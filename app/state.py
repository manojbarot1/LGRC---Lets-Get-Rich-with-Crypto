"""Shared in-process state: WebSocket connections + live snapshot."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fastapi import WebSocket
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.models import Portfolio, Position, PortfolioSnapshot

_connections: list[WebSocket] = []
next_cycle_in: int = 0       # seconds until next fast price cycle
next_ai_in: int = 0          # seconds until next Claude analysis
last_analysis: str = "Waiting for first analysis cycle…"
last_actions: list[dict] = []
last_cash_advice: dict = {"action": "NONE"}


async def ws_connect(ws: WebSocket) -> None:
    await ws.accept()
    _connections.append(ws)


def ws_disconnect(ws: WebSocket) -> None:
    if ws in _connections:
        _connections.remove(ws)


async def _safe_send(ws: WebSocket, data: dict) -> bool:
    try:
        await ws.send_json(data)
        return True
    except Exception:
        ws_disconnect(ws)
        return False


async def broadcast_update(
    session: AsyncSession,
    portfolio: "Portfolio",
    positions: list["Position"],
    prices: dict[str, float],
    snap: "PortfolioSnapshot",
) -> None:
    if not _connections:
        return

    from app.models import Trade, PortfolioSnapshot

    trades_q = await session.execute(
        select(Trade).order_by(desc(Trade.executed_at)).limit(20)
    )
    recent_trades = [
        {
            "id": t.id,
            "symbol": t.symbol,
            "side": t.side,
            "quantity": t.quantity,
            "price": t.price,
            "amount_usd": t.amount_usd,
            "realized_pnl": t.realized_pnl,
            "reason": t.reason,
            "executed_at": t.executed_at.isoformat() if t.executed_at else None,
        }
        for t in trades_q.scalars().all()
    ]

    hist_q = await session.execute(
        select(PortfolioSnapshot).order_by(desc(PortfolioSnapshot.recorded_at)).limit(200)
    )
    history = [
        {"t": s.recorded_at.isoformat(), "v": s.total_value, "pnl_pct": s.pnl_pct}
        for s in reversed(hist_q.scalars().all())
    ]

    pos_list = [
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
    ]

    payload = {
        "portfolio": {
            "initial_capital": portfolio.initial_capital,
            "total_deposited": round(portfolio.total_deposited, 2),
            "cash": round(portfolio.current_cash, 2),
            "total_value": snap.total_value,
            "invested": snap.invested,
            "pnl": snap.pnl,
            "pnl_pct": snap.pnl_pct,
            "realized_pnl": round(portfolio.realized_pnl, 2),
            "is_running": portfolio.is_running,
            "started_at": portfolio.started_at.isoformat() if portfolio.started_at else None,
        },
        "positions": pos_list,
        "recent_trades": recent_trades,
        "history": history,
        "analysis": last_analysis,
        "last_actions": last_actions,
        "cash_advice": last_cash_advice,
        "next_cycle_in": next_cycle_in,
        "next_ai_in": next_ai_in,
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    dead = []
    for ws in list(_connections):
        ok = await _safe_send(ws, payload)
        if not ok:
            dead.append(ws)
    for ws in dead:
        ws_disconnect(ws)
