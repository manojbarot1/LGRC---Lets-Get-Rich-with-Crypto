"""FastAPI app — LGRC: Let's Get Rich with Crypto."""
import asyncio
import structlog
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import state
from app.auth import COOKIE, create_token, get_current_user, hash_password, redirect_to_login, verify_password
from app.config import get_settings
from app.database import get_session, get_session_factory, init_db
from app.models import AISettings, AnalysisLog, Portfolio, PortfolioSnapshot, Trade, User
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


# ─── Auth routes ──────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = "", mode: str = ""):
    return templates.TemplateResponse("login.html", {
        "request": request, "error": error, "mode": mode,
    })


@app.post("/login")
async def login_submit(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    mode = form.get("mode", "login")
    username = str(form.get("username", "")).strip().lower()
    password = str(form.get("password", ""))

    if mode == "register":
        password2 = str(form.get("password2", ""))
        if len(username) < 3:
            return templates.TemplateResponse("login.html", {
                "request": request, "error": "Username must be at least 3 characters.", "mode": "register"
            })
        if len(password) < 6:
            return templates.TemplateResponse("login.html", {
                "request": request, "error": "Password must be at least 6 characters.", "mode": "register"
            })
        if password != password2:
            return templates.TemplateResponse("login.html", {
                "request": request, "error": "Passwords do not match.", "mode": "register"
            })
        existing = (await session.execute(
            select(User).where(User.username == username)
        )).scalar_one_or_none()
        if existing:
            return templates.TemplateResponse("login.html", {
                "request": request, "error": "Username already taken.", "mode": "register"
            })
        user = User(username=username, password_hash=hash_password(password))
        session.add(user)
        await session.commit()
        log.info("user.registered", username=username)

    else:
        user = (await session.execute(
            select(User).where(User.username == username, User.is_active == True)
        )).scalar_one_or_none()
        if not user or not verify_password(password, user.password_hash):
            return templates.TemplateResponse("login.html", {
                "request": request, "error": "Invalid username or password.", "mode": "login"
            })

    token = create_token(user.id, user.username)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(COOKIE, token, httponly=True, max_age=86400 * 30, samesite="lax")
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response


# ─── Dashboard ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    user = await get_current_user(request, session)
    if not user:
        return redirect_to_login()

    settings = get_settings()
    portfolio = await get_or_create_portfolio(
        session, user.id, settings.starting_capital, settings.target_weekly_pct
    )
    positions = await get_positions(session, portfolio.id)
    prices = await get_prices_for_symbols([p.symbol for p in positions])

    invested = sum(p.quantity * prices.get(p.symbol, p.current_price) for p in positions)
    total = portfolio.current_cash + invested
    basis = portfolio.total_deposited if portfolio.total_deposited > 0 else portfolio.initial_capital
    pnl = total - basis
    pnl_pct = pnl / basis * 100 if basis > 0 else 0

    recent_trades = (await session.execute(
        select(Trade)
        .where(Trade.portfolio_id == portfolio.id)
        .order_by(desc(Trade.executed_at))
        .limit(30)
    )).scalars().all()

    last_log = (await session.execute(
        select(AnalysisLog)
        .where(AnalysisLog.portfolio_id == portfolio.id)
        .order_by(desc(AnalysisLog.created_at))
        .limit(1)
    )).scalar_one_or_none()

    snapshots = (await session.execute(
        select(PortfolioSnapshot)
        .where(PortfolioSnapshot.portfolio_id == portfolio.id)
        .order_by(desc(PortfolioSnapshot.recorded_at))
        .limit(200)
    )).scalars().all()
    chart_history = [
        {"t": s.recorded_at.isoformat(), "v": s.total_value}
        for s in reversed(snapshots)
    ]

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
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
        "total_value": round(total, 2),
        "invested": round(invested, 2),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "recent_trades": recent_trades,
        "last_analysis": last_log.market_view if last_log else state.last_analysis.get(portfolio.id, "Waiting for first analysis cycle…"),
        "started_at": portfolio.started_at.isoformat() if portfolio.started_at else "",
        "chart_history": chart_history,
    })


# ─── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, session: AsyncSession = Depends(get_session)):
    user = await get_current_user(websocket, session)
    if not user:
        await websocket.close(code=4001)
        return

    settings = get_settings()
    portfolio = await get_or_create_portfolio(
        session, user.id, settings.starting_capital, settings.target_weekly_pct
    )
    await state.ws_connect(websocket, portfolio.id)
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


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _require_portfolio(request: Request, session: AsyncSession):
    """Returns (user, portfolio) or raises a JSON 401."""
    user = await get_current_user(request, session)
    if not user:
        raise _Unauthorized()
    settings = get_settings()
    portfolio = await get_or_create_portfolio(
        session, user.id, settings.starting_capital, settings.target_weekly_pct
    )
    return user, portfolio


class _Unauthorized(Exception):
    pass


async def _get_portfolio(request: Request, session: AsyncSession = Depends(get_session)):
    user = await get_current_user(request, session)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401), None, None
    settings = get_settings()
    portfolio = await get_or_create_portfolio(
        session, user.id, settings.starting_capital, settings.target_weekly_pct
    )
    return None, user, portfolio


# ─── Cash management ──────────────────────────────────────────────────────────

class CashRequest(BaseModel):
    amount: float
    note: str = ""


@app.post("/api/deposit")
async def api_deposit(body: CashRequest, request: Request, session: AsyncSession = Depends(get_session)):
    user = await get_current_user(request, session)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if body.amount <= 0:
        return JSONResponse({"error": "Amount must be positive"}, status_code=400)
    settings = get_settings()
    portfolio = await get_or_create_portfolio(session, user.id, settings.starting_capital, settings.target_weekly_pct)
    await deposit_cash(session, portfolio, body.amount, body.note)
    return {"status": "ok", "cash": round(portfolio.current_cash, 2), "total_deposited": round(portfolio.total_deposited, 2)}


@app.post("/api/withdraw")
async def api_withdraw(body: CashRequest, request: Request, session: AsyncSession = Depends(get_session)):
    user = await get_current_user(request, session)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if body.amount <= 0:
        return JSONResponse({"error": "Amount must be positive"}, status_code=400)
    settings = get_settings()
    portfolio = await get_or_create_portfolio(session, user.id, settings.starting_capital, settings.target_weekly_pct)
    tx = await withdraw_cash(session, portfolio, body.amount, body.note)
    if tx is None:
        return JSONResponse({"error": f"Insufficient cash. Available: ${portfolio.current_cash:,.2f}"}, status_code=400)
    return {"status": "ok", "cash": round(portfolio.current_cash, 2), "total_deposited": round(portfolio.total_deposited, 2)}


# ─── Control API ──────────────────────────────────────────────────────────────

@app.post("/api/pause")
async def pause(request: Request, session: AsyncSession = Depends(get_session)):
    user = await get_current_user(request, session)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    portfolio = (await session.execute(select(Portfolio).where(Portfolio.user_id == user.id).limit(1))).scalar_one_or_none()
    if portfolio:
        portfolio.is_running = False
        await session.commit()
    return {"status": "paused"}


@app.post("/api/resume")
async def resume(request: Request, session: AsyncSession = Depends(get_session)):
    user = await get_current_user(request, session)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    portfolio = (await session.execute(select(Portfolio).where(Portfolio.user_id == user.id).limit(1))).scalar_one_or_none()
    if portfolio:
        portfolio.is_running = True
        await session.commit()
    return {"status": "running"}


@app.post("/api/reset")
async def reset(request: Request, session: AsyncSession = Depends(get_session)):
    user = await get_current_user(request, session)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    from app.models import Position
    settings = get_settings()
    portfolio = (await session.execute(select(Portfolio).where(Portfolio.user_id == user.id).limit(1))).scalar_one_or_none()
    if portfolio:
        for pos in (await session.execute(select(Position).where(Position.portfolio_id == portfolio.id))).scalars().all():
            await session.delete(pos)
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
async def get_ai_settings(request: Request, session: AsyncSession = Depends(get_session)):
    user = await get_current_user(request, session)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    ai = (await session.execute(
        select(AISettings).where(AISettings.user_id == user.id).limit(1)
    )).scalar_one_or_none()
    settings = get_settings()
    if not ai:
        return {"provider": "anthropic", "api_key_set": bool(settings.anthropic_api_key),
                "base_url": "", "model_name": settings.claude_model}
    return {"provider": ai.provider, "api_key_set": bool(ai.api_key),
            "base_url": ai.base_url, "model_name": ai.model_name}


@app.post("/api/settings/ai")
async def update_ai_settings(body: AISettingsRequest, request: Request, session: AsyncSession = Depends(get_session)):
    user = await get_current_user(request, session)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    ai = (await session.execute(
        select(AISettings).where(AISettings.user_id == user.id).limit(1)
    )).scalar_one_or_none()
    if not ai:
        ai = AISettings(user_id=user.id)
        session.add(ai)
    ai.provider = body.provider
    ai.base_url = body.base_url
    ai.model_name = body.model_name
    if body.api_key:
        ai.api_key = body.api_key
    await session.commit()
    return {"status": "ok"}


@app.post("/api/settings/ai/test")
async def test_ai_connection(body: AISettingsRequest, request: Request, session: AsyncSession = Depends(get_session)):
    user = await get_current_user(request, session)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    import anthropic as _anthropic
    import httpx as _httpx

    if body.provider == "anthropic":
        try:
            key = body.api_key or get_settings().anthropic_api_key
            model = body.model_name or get_settings().claude_model
            client = _anthropic.AsyncAnthropic(api_key=key)
            await client.messages.create(model=model, max_tokens=10,
                                          messages=[{"role": "user", "content": "ping"}])
            return {"status": "ok", "message": f"Connected · model: {model}"}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    else:
        try:
            base = (body.base_url or "http://localhost:11434/v1").rstrip("/")
            model = body.model_name or "llama3.2"
            headers = {"Content-Type": "application/json"}
            if body.api_key:
                headers["Authorization"] = f"Bearer {body.api_key}"
            async with _httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(base + "/chat/completions", headers=headers, json={
                    "model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5,
                })
                resp.raise_for_status()
            return {"status": "ok", "message": f"Connected · {base} · model: {model}"}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "next_cycle_in": state.next_cycle_in, "next_ai_in": state.next_ai_in}
