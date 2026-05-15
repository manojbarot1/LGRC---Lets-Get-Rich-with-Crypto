"""FastAPI app — LGRC: Let's Get Rich with Crypto."""
import asyncio
import structlog
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import state
from app.config import get_settings
from app.database import get_session, get_session_factory, init_db
from app.models import AISettings, AnalysisLog, Portfolio, Trade
from app.portfolio import (
    deposit_cash, get_or_create_portfolio, get_positions,
    record_snapshot, withdraw_cash,
)
from app.prices import get_prices_for_symbols
from app.scheduler import run_scheduler

log = structlog.get_logger()

structlog.configure(processors=[
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.processors.JSONRenderer(),
])


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    asyncio.create_task(run_scheduler())
    log.info("lgrc.started")
    yield
    log.info("lgrc.stopped")


app = FastAPI(title="LGRC — Let's Get Rich with Crypto", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


# ─── Dashboard ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    settings = get_settings()
    portfolio = await get_or_create_portfolio(
        session, settings.starting_capital, settings.target_weekly_pct
    )
    positions = await get_positions(session)
    prices = await get_prices_for_symbols([p.symbol for p in positions])

    invested = sum(p.quantity * prices.get(p.symbol, p.current_price) for p in positions)
    total = portfolio.current_cash + invested
    basis = portfolio.total_deposited if portfolio.total_deposited > 0 else portfolio.initial_capital
    pnl = total - basis
    pnl_pct = pnl / basis * 100 if basis > 0 else 0

    recent_trades = (await session.execute(
        select(Trade).order_by(desc(Trade.executed_at)).limit(30)
    )).scalars().all()

    last_log = (await session.execute(
        select(AnalysisLog).order_by(desc(AnalysisLog.created_at)).limit(1)
    )).scalar_one_or_none()

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "settings": settings,
        "portfolio": portfolio,
        "positions": [
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "avg_cost": p.avg_cost,
                "current_price": prices.get(p.symbol, p.current_price),
                "pnl_usd": round((prices.get(p.symbol, p.current_price) - p.avg_cost) * p.quantity, 2),
                "pnl_pct": round(
                    (prices.get(p.symbol, p.current_price) - p.avg_cost) / p.avg_cost * 100, 2
                ),
                "value_usd": round(prices.get(p.symbol, p.current_price) * p.quantity, 2),
                "opened_at": p.opened_at.isoformat() if p.opened_at else "",
            }
            for p in positions
        ],
        "started_at": portfolio.started_at.isoformat() if portfolio.started_at else "",
        "total_value": round(total, 2),
        "invested": round(invested, 2),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "recent_trades": recent_trades,
        "last_analysis": last_log.market_view if last_log else state.last_analysis,
        "last_cash_advice": (last_log.cash_advice if last_log and last_log.cash_advice
                             else state.last_cash_advice),
        "next_cycle_in": state.next_cycle_in,
        "next_ai_in": state.next_ai_in,
    })


# ─── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await state.ws_connect(websocket)
    try:
        while True:
            await asyncio.sleep(1)
            await websocket.send_json({
                "type": "ping",
                "next_cycle_in": state.next_cycle_in,
                "next_ai_in": state.next_ai_in,
            })
    except WebSocketDisconnect:
        state.ws_disconnect(websocket)
    except Exception:
        state.ws_disconnect(websocket)


# ─── Cash management ──────────────────────────────────────────────────────────

class CashRequest(BaseModel):
    amount: float
    note: str = ""


@app.post("/api/deposit")
async def api_deposit(body: CashRequest, session: AsyncSession = Depends(get_session)):
    if body.amount <= 0:
        return JSONResponse({"error": "Amount must be positive"}, status_code=400)
    settings = get_settings()
    portfolio = await get_or_create_portfolio(
        session, settings.starting_capital, settings.target_weekly_pct
    )
    await deposit_cash(session, portfolio, body.amount, body.note)
    return {
        "status": "ok",
        "cash": round(portfolio.current_cash, 2),
        "total_deposited": round(portfolio.total_deposited, 2),
    }


@app.post("/api/withdraw")
async def api_withdraw(body: CashRequest, session: AsyncSession = Depends(get_session)):
    if body.amount <= 0:
        return JSONResponse({"error": "Amount must be positive"}, status_code=400)
    settings = get_settings()
    portfolio = await get_or_create_portfolio(
        session, settings.starting_capital, settings.target_weekly_pct
    )
    tx = await withdraw_cash(session, portfolio, body.amount, body.note)
    if tx is None:
        return JSONResponse(
            {"error": f"Insufficient cash. Available: ${portfolio.current_cash:,.2f}"},
            status_code=400,
        )
    return {
        "status": "ok",
        "cash": round(portfolio.current_cash, 2),
        "total_deposited": round(portfolio.total_deposited, 2),
    }


# ─── Control API ──────────────────────────────────────────────────────────────

@app.post("/api/pause")
async def pause(session: AsyncSession = Depends(get_session)):
    portfolio = (await session.execute(select(Portfolio).limit(1))).scalar_one_or_none()
    if portfolio:
        portfolio.is_running = False
        await session.commit()
    return {"status": "paused"}


@app.post("/api/resume")
async def resume(session: AsyncSession = Depends(get_session)):
    portfolio = (await session.execute(select(Portfolio).limit(1))).scalar_one_or_none()
    if portfolio:
        portfolio.is_running = True
        await session.commit()
    return {"status": "running"}


@app.post("/api/reset")
async def reset(session: AsyncSession = Depends(get_session)):
    from app.models import Position
    for pos in (await session.execute(select(Position))).scalars().all():
        await session.delete(pos)
    settings = get_settings()
    portfolio = (await session.execute(select(Portfolio).limit(1))).scalar_one_or_none()
    if portfolio:
        portfolio.current_cash = settings.starting_capital
        portfolio.total_deposited = settings.starting_capital
        portfolio.realized_pnl = 0.0
        portfolio.is_running = True
    await session.commit()
    return {"status": "reset"}


# ─── AI Settings ──────────────────────────────────────────────────────────────

class AISettingsRequest(BaseModel):
    provider: str = "anthropic"
    api_key: str = ""
    base_url: str = ""
    model_name: str = ""


@app.get("/api/settings/ai")
async def get_ai_settings(session: AsyncSession = Depends(get_session)):
    ai = (await session.execute(select(AISettings).limit(1))).scalar_one_or_none()
    settings = get_settings()
    if not ai:
        return {
            "provider": "anthropic",
            "api_key_set": bool(settings.anthropic_api_key),
            "base_url": "",
            "model_name": settings.claude_model,
        }
    return {
        "provider": ai.provider,
        "api_key_set": bool(ai.api_key),
        "base_url": ai.base_url,
        "model_name": ai.model_name,
    }


@app.post("/api/settings/ai")
async def update_ai_settings(body: AISettingsRequest, session: AsyncSession = Depends(get_session)):
    ai = (await session.execute(select(AISettings).limit(1))).scalar_one_or_none()
    if not ai:
        ai = AISettings()
        session.add(ai)
    ai.provider = body.provider
    ai.base_url = body.base_url
    ai.model_name = body.model_name
    if body.api_key:  # blank = keep existing key
        ai.api_key = body.api_key
    await session.commit()
    return {"status": "ok"}


@app.post("/api/settings/ai/test")
async def test_ai_connection(body: AISettingsRequest):
    import anthropic as _anthropic
    import httpx as _httpx

    if body.provider == "anthropic":
        try:
            key = body.api_key or get_settings().anthropic_api_key
            model = body.model_name or get_settings().claude_model
            client = _anthropic.AsyncAnthropic(api_key=key)
            await client.messages.create(
                model=model, max_tokens=10,
                messages=[{"role": "user", "content": "ping"}],
            )
            return {"status": "ok", "message": f"Connected · model: {model}"}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    else:
        try:
            base = (body.base_url or "http://localhost:11434/v1").rstrip("/")
            model = body.model_name or "llama3.2"
            url = base + "/chat/completions"
            headers = {"Content-Type": "application/json"}
            if body.api_key:
                headers["Authorization"] = f"Bearer {body.api_key}"
            async with _httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, headers=headers, json={
                    "model": model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 5,
                })
                resp.raise_for_status()
            return {"status": "ok", "message": f"Connected · {base} · model: {model}"}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "next_cycle_in": state.next_cycle_in,
        "next_ai_in": state.next_ai_in,
    }
