"""Shared in-process state: WebSocket connections (per portfolio) + live snapshot."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fastapi import WebSocket
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.models import Portfolio, Position, PortfolioSnapshot

# portfolio_id -> list of active WebSocket connections
_connections: dict[int, list[WebSocket]] = {}
_ws_to_portfolio: dict[int, int] = {}  # id(ws) -> portfolio_id

next_cycle_in: int = 0
next_ai_in: int = 0

# per-portfolio state (portfolio_id -> value)
last_analysis: dict[int, str] = {}
last_actions: dict[int, list] = {}
last_cash_advice: dict[int, dict] = {}


async def ws_connect(ws: WebSocket, portfolio_id: int) -> None:
    await ws.accept()
    _connections.setdefault(portfolio_id, []).append(ws)
    _ws_to_portfolio[id(ws)] = portfolio_id


def ws_disconnect(ws: WebSocket) -> None:
    pid = _ws_to_portfolio.pop(id(ws), None)
    if pid is not None and pid in _connections:
        try:
            _connections[pid].remove(ws)
        except ValueError:
            pass


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
    connections = list(_connections.get(portfolio.id, []))
    if not connections:
        return

    from app.models import Trade, PortfolioSnapshot as PS

    trades = (await session.execute(
        select(Trade)
        .where(Trade.portfolio_id == portfolio.id)
        .order_by(desc(Trade.executed_at))
        .limit(20)
    )).scalars().all()

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
        for t in trades
    ]

    hist = (await session.execute(
        select(PS)
        .where(PS.portfolio_id == portfolio.id)
        .order_by(desc(PS.recorded_at))
        .limit(200)
    )).scalars().all()

    history = [
        {"t": s.recorded_at.isoformat(), "v": s.total_value, "pnl_pct": s.pnl_pct}
        for s in reversed(hist)
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
        "analysis": last_analysis.get(portfolio.id, "Waiting for first analysis cycle…"),
        "last_actions": last_actions.get(portfolio.id, []),
        "cash_advice": last_cash_advice.get(portfolio.id, {"action": "NONE"}),
        "next_cycle_in": next_cycle_in,
        "next_ai_in": next_ai_in,
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    for ws in connections:
        await _safe_send(ws, payload)
