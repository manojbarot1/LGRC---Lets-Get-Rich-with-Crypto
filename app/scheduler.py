"""Two-speed trading loop:
  Fast cycle (every 60s)  — price update, stop-loss/take-profit, broadcast
  Claude cycle (every 5m) — full AI analysis, trade execution, broadcast
"""
from __future__ import annotations

import asyncio
import structlog
from sqlalchemy import select, desc

from app.analyst import analyze_and_decide
from app.models import AISettings
from app.config import get_settings
from app.database import get_session_factory
from app.models import AnalysisLog, Portfolio
from app.portfolio import (
    apply_claude_actions, apply_stop_loss_and_take_profit,
    get_or_create_portfolio, get_positions, record_snapshot, update_prices,
)
from app.prices import get_market_snapshot, get_prices_for_symbols
from app import state

log = structlog.get_logger()


async def _fetch_prices(session, portfolio) -> tuple[dict, list]:
    """Fetch current prices for held positions + top market movers."""
    from app.prices import get_top_movers
    positions = await get_positions(session)
    held = [p.symbol for p in positions]
    top = [m["symbol"] for m in (await get_top_movers(20))]
    all_syms = list(set(held + top))
    prices = await get_prices_for_symbols(all_syms)
    return prices, positions


async def run_price_cycle() -> None:
    """Fast cycle: update prices, enforce stop-loss/take-profit, broadcast."""
    settings = get_settings()
    sf = get_session_factory()

    async with sf() as session:
        portfolio = await get_or_create_portfolio(
            session, settings.starting_capital, settings.target_weekly_pct
        )
        if not portfolio.is_running:
            return

        prices, positions = await _fetch_prices(session, portfolio)
        await update_prices(session, prices)

        triggered = await apply_stop_loss_and_take_profit(
            session, portfolio, prices,
            settings.stop_loss_pct, settings.take_profit_pct,
        )

        positions = await get_positions(session)
        snap = await record_snapshot(session, portfolio, prices)
        await state.broadcast_update(session, portfolio, positions, prices, snap)

        if triggered:
            log.info("price_cycle.auto_trades", trades=len(triggered), value=snap.total_value)


async def run_claude_cycle() -> None:
    """Full cycle: market snapshot, Claude analysis, execute trades, broadcast."""
    settings = get_settings()
    sf = get_session_factory()

    async with sf() as session:
        portfolio = await get_or_create_portfolio(
            session, settings.starting_capital, settings.target_weekly_pct
        )
        if not portfolio.is_running:
            return

        market = await get_market_snapshot()

        positions = await get_positions(session)
        held = [p.symbol for p in positions]
        top = [m["symbol"] for m in market.get("top_movers", [])[:20]]
        all_syms = list(set(held + top))
        prices = await get_prices_for_symbols(all_syms)

        await update_prices(session, prices)

        triggered = await apply_stop_loss_and_take_profit(
            session, portfolio, prices,
            settings.stop_loss_pct, settings.take_profit_pct,
        )

        positions = await get_positions(session)
        pos_dicts = [
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
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

        ai_row = (await session.execute(select(AISettings).limit(1))).scalar_one_or_none()
        ai_config = (
            {"provider": ai_row.provider, "api_key": ai_row.api_key,
             "base_url": ai_row.base_url, "model_name": ai_row.model_name}
            if ai_row else None
        )

        decision = await analyze_and_decide(portfolio.current_cash, pos_dicts, market, settings_dict, ai_config)

        claude_trades = await apply_claude_actions(
            session, portfolio, decision.get("actions", []),
            prices, settings.max_positions,
            settings.max_position_pct, settings.min_cash_reserve_pct,
        )

        all_trades = triggered + claude_trades

        positions = await get_positions(session)
        snap = await record_snapshot(session, portfolio, prices)

        log_entry = AnalysisLog(
            market_view=decision.get("market_view", ""),
            actions=decision.get("actions", []),
            cash_advice=decision.get("cash_advice"),
            trades_executed=len(all_trades),
            error=decision.get("error"),
        )
        session.add(log_entry)
        await session.commit()

        state.last_analysis = decision.get("market_view", "")
        state.last_actions = decision.get("actions", [])
        state.last_cash_advice = decision.get("cash_advice", {"action": "NONE"})

        await state.broadcast_update(session, portfolio, positions, prices, snap)

        log.info(
            "claude_cycle.done",
            value=snap.total_value,
            pnl=snap.pnl,
            pnl_pct=snap.pnl_pct,
            trades=len(all_trades),
        )


async def run_scheduler() -> None:
    settings = get_settings()
    fast_interval = settings.fast_interval_seconds
    claude_interval = settings.claude_interval_seconds
    claude_every_n = max(1, claude_interval // fast_interval)

    log.info("scheduler.start",
             fast_interval=fast_interval,
             claude_interval=claude_interval,
             claude_every_n_ticks=claude_every_n)

    tick = 0  # tick=0 runs Claude immediately on startup

    while True:
        try:
            if tick % claude_every_n == 0:
                log.info("scheduler.tick", type="claude+price", tick=tick)
                await run_claude_cycle()
            else:
                log.info("scheduler.tick", type="price_only", tick=tick)
                await run_price_cycle()
        except Exception as e:
            log.exception("cycle.failed", error=str(e))

        tick += 1
        ticks_until_claude = claude_every_n - (tick % claude_every_n)
        state.next_ai_in = ticks_until_claude * fast_interval

        for i in range(fast_interval, 0, -1):
            state.next_cycle_in = i
            await asyncio.sleep(1)
        state.next_cycle_in = 0
