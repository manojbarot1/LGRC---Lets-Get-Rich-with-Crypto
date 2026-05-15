"""Two-speed trading loop — runs concurrently for every active user portfolio.

  Fast cycle (every 60s)  — price update, stop-loss/take-profit, broadcast
  Claude cycle (every 5m) — full AI analysis, trade execution, broadcast
"""
from __future__ import annotations

import asyncio
import structlog
from sqlalchemy import select, desc

from app.analyst import analyze_and_decide
from app.config import get_settings
from app.database import get_session_factory
from app.models import AISettings, AnalysisLog, Portfolio
from app.portfolio import (
    apply_claude_actions, apply_stop_loss_and_take_profit,
    get_or_create_portfolio, get_positions, record_snapshot, update_prices,
)
from app.prices import get_market_snapshot, get_prices_for_symbols, get_top_movers
from app import state

log = structlog.get_logger()


async def _fetch_prices(session, portfolio_id: int) -> tuple[dict, list]:
    positions = await get_positions(session, portfolio_id)
    held = [p.symbol for p in positions]
    top = [m["symbol"] for m in (await get_top_movers(20))]
    prices = await get_prices_for_symbols(list(set(held + top)))
    return prices, positions


async def _run_price_cycle(portfolio_id: int) -> None:
    settings = get_settings()
    sf = get_session_factory()
    async with sf() as session:
        portfolio = (await session.execute(
            select(Portfolio).where(Portfolio.id == portfolio_id)
        )).scalar_one_or_none()
        if not portfolio or not portfolio.is_running:
            return

        prices, positions = await _fetch_prices(session, portfolio_id)
        await update_prices(session, portfolio_id, prices)
        triggered = await apply_stop_loss_and_take_profit(
            session, portfolio, prices, settings.stop_loss_pct, settings.take_profit_pct
        )
        positions = await get_positions(session, portfolio_id)
        snap = await record_snapshot(session, portfolio, prices)
        await state.broadcast_update(session, portfolio, positions, prices, snap)
        if triggered:
            log.info("price_cycle.auto_trades", portfolio_id=portfolio_id, trades=len(triggered))


async def _run_claude_cycle(portfolio_id: int) -> None:
    settings = get_settings()
    sf = get_session_factory()
    async with sf() as session:
        portfolio = (await session.execute(
            select(Portfolio).where(Portfolio.id == portfolio_id)
        )).scalar_one_or_none()
        if not portfolio or not portfolio.is_running:
            return

        market = await get_market_snapshot()
        prices, _ = await _fetch_prices(session, portfolio_id)
        await update_prices(session, portfolio_id, prices)

        triggered = await apply_stop_loss_and_take_profit(
            session, portfolio, prices, settings.stop_loss_pct, settings.take_profit_pct
        )

        positions = await get_positions(session, portfolio_id)
        pos_dicts = [
            {
                "symbol": p.symbol, "quantity": p.quantity,
                "avg_cost": p.avg_cost,
                "current_price": prices.get(p.symbol, p.current_price),
            }
            for p in positions
        ]

        settings_dict = {
            "initial_capital": settings.starting_capital,
            "total_deposited": portfolio.total_deposited,
            "target_weekly_pct": settings.target_weekly_pct,
            "max_positions": settings.max_positions,
            "max_position_pct": settings.max_position_pct,
            "min_cash_reserve_pct": settings.min_cash_reserve_pct,
            "stop_loss_pct": settings.stop_loss_pct,
            "take_profit_pct": settings.take_profit_pct,
        }

        ai_row = (await session.execute(
            select(AISettings).where(AISettings.user_id == portfolio.user_id).limit(1)
        )).scalar_one_or_none()
        ai_config = (
            {"provider": ai_row.provider, "api_key": ai_row.api_key,
             "base_url": ai_row.base_url, "model_name": ai_row.model_name}
            if ai_row else None
        )

        decision = await analyze_and_decide(
            portfolio.current_cash, pos_dicts, market, settings_dict, ai_config
        )

        claude_trades = await apply_claude_actions(
            session, portfolio, decision.get("actions", []),
            prices, settings.max_positions,
            settings.max_position_pct, settings.min_cash_reserve_pct,
        )

        positions = await get_positions(session, portfolio_id)
        snap = await record_snapshot(session, portfolio, prices)

        session.add(AnalysisLog(
            portfolio_id=portfolio_id,
            market_view=decision.get("market_view", ""),
            actions=decision.get("actions", []),
            cash_advice=decision.get("cash_advice"),
            trades_executed=len(triggered) + len(claude_trades),
            error=decision.get("error"),
        ))
        await session.commit()

        state.last_analysis[portfolio_id] = decision.get("market_view", "")
        state.last_actions[portfolio_id] = decision.get("actions", [])
        state.last_cash_advice[portfolio_id] = decision.get("cash_advice", {"action": "NONE"})

        await state.broadcast_update(session, portfolio, positions, prices, snap)
        log.info("claude_cycle.done", portfolio_id=portfolio_id,
                 value=snap.total_value, trades=len(triggered) + len(claude_trades))


async def _run_all(cycle_fn) -> None:
    """Run one cycle function for every active portfolio, concurrently."""
    sf = get_session_factory()
    async with sf() as session:
        ids = [
            p.id for p in (await session.execute(
                select(Portfolio).where(Portfolio.is_running == True)
            )).scalars().all()
        ]
    if not ids:
        return
    results = await asyncio.gather(*[cycle_fn(pid) for pid in ids], return_exceptions=True)
    for pid, r in zip(ids, results):
        if isinstance(r, Exception):
            log.exception("cycle.failed", portfolio_id=pid, error=str(r))


async def run_scheduler() -> None:
    settings = get_settings()
    fast_interval = settings.fast_interval_seconds
    claude_every_n = max(1, settings.claude_interval_seconds // fast_interval)

    log.info("scheduler.start", fast_interval=fast_interval, claude_every_n=claude_every_n)
    tick = 0

    while True:
        try:
            if tick % claude_every_n == 0:
                await _run_all(_run_claude_cycle)
            else:
                await _run_all(_run_price_cycle)
        except Exception as e:
            log.exception("scheduler.error", error=str(e))

        tick += 1
        ticks_until_claude = claude_every_n - (tick % claude_every_n)
        state.next_ai_in = ticks_until_claude * fast_interval

        for i in range(fast_interval, 0, -1):
            state.next_cycle_in = i
            await asyncio.sleep(1)
        state.next_cycle_in = 0
