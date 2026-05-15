"""Portfolio execution engine — applies AI decisions to a user's sim portfolio."""
from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CashTransaction, Portfolio, Position, Trade, PortfolioSnapshot

log = structlog.get_logger()


async def get_or_create_portfolio(
    session: AsyncSession, user_id: int, initial_capital: float, target_weekly_pct: float
) -> Portfolio:
    result = await session.execute(select(Portfolio).where(Portfolio.user_id == user_id).limit(1))
    p = result.scalar_one_or_none()
    if p is None:
        p = Portfolio(
            user_id=user_id,
            initial_capital=initial_capital,
            total_deposited=initial_capital,
            current_cash=initial_capital,
            target_weekly_pct=target_weekly_pct,
        )
        session.add(p)
        await session.commit()
        log.info("portfolio.created", user_id=user_id, capital=initial_capital)
    return p


async def get_positions(session: AsyncSession, portfolio_id: int) -> list[Position]:
    result = await session.execute(
        select(Position).where(Position.portfolio_id == portfolio_id)
    )
    return list(result.scalars().all())


async def update_prices(session: AsyncSession, portfolio_id: int, prices: dict[str, float]) -> None:
    for pos in await get_positions(session, portfolio_id):
        if pos.symbol in prices:
            pos.current_price = prices[pos.symbol]
    await session.commit()


async def deposit_cash(
    session: AsyncSession, portfolio: Portfolio, amount: float, note: str = ""
) -> CashTransaction:
    amount = round(abs(amount), 2)
    portfolio.current_cash = round(portfolio.current_cash + amount, 4)
    portfolio.total_deposited = round(portfolio.total_deposited + amount, 4)
    tx = CashTransaction(portfolio_id=portfolio.id, type="DEPOSIT", amount=amount, note=note)
    session.add(tx)
    await session.commit()
    log.info("cash.deposit", portfolio_id=portfolio.id, amount=amount)
    return tx


async def withdraw_cash(
    session: AsyncSession, portfolio: Portfolio, amount: float, note: str = ""
) -> CashTransaction | None:
    amount = round(abs(amount), 2)
    if amount > portfolio.current_cash:
        return None
    portfolio.current_cash = round(portfolio.current_cash - amount, 4)
    portfolio.total_deposited = round(portfolio.total_deposited - amount, 4)
    tx = CashTransaction(portfolio_id=portfolio.id, type="WITHDRAW", amount=amount, note=note)
    session.add(tx)
    await session.commit()
    log.info("cash.withdraw", portfolio_id=portfolio.id, amount=amount)
    return tx


async def execute_buy(
    session: AsyncSession, portfolio: Portfolio,
    symbol: str, amount_usd: float, price: float, reason: str,
) -> Trade | None:
    if price <= 0:
        return None
    if amount_usd > portfolio.current_cash:
        amount_usd = portfolio.current_cash
    if amount_usd < 1.0:
        return None

    quantity = round(amount_usd / price, 6)

    pos = (await session.execute(
        select(Position).where(Position.portfolio_id == portfolio.id, Position.symbol == symbol)
    )).scalar_one_or_none()

    if pos is None:
        pos = Position(portfolio_id=portfolio.id, symbol=symbol,
                       quantity=quantity, avg_cost=price, current_price=price)
        session.add(pos)
    else:
        total_cost = pos.avg_cost * pos.quantity + price * quantity
        pos.quantity = round(pos.quantity + quantity, 6)
        pos.avg_cost = total_cost / pos.quantity
        pos.current_price = price

    portfolio.current_cash = round(portfolio.current_cash - amount_usd, 4)
    trade = Trade(portfolio_id=portfolio.id, symbol=symbol, side="BUY",
                  quantity=quantity, price=price, amount_usd=amount_usd, reason=reason)
    session.add(trade)
    await session.commit()
    log.info("trade.buy", portfolio_id=portfolio.id, symbol=symbol, qty=quantity, price=price)
    return trade


async def execute_sell(
    session: AsyncSession, portfolio: Portfolio,
    symbol: str, quantity: float | None, price: float, reason: str,
) -> Trade | None:
    if price <= 0:
        return None

    pos = (await session.execute(
        select(Position).where(Position.portfolio_id == portfolio.id, Position.symbol == symbol)
    )).scalar_one_or_none()

    if pos is None or pos.quantity <= 0:
        return None

    quantity = round(min(quantity or pos.quantity, pos.quantity), 6)
    if quantity <= 0:
        return None

    proceeds = round(quantity * price, 4)
    cost_basis = round(quantity * pos.avg_cost, 4)
    realized_pnl = round(proceeds - cost_basis, 4)

    pos.quantity = round(pos.quantity - quantity, 6)
    pos.current_price = price
    portfolio.current_cash = round(portfolio.current_cash + proceeds, 4)
    portfolio.realized_pnl = round(portfolio.realized_pnl + realized_pnl, 4)

    if pos.quantity < 0.000001:
        await session.delete(pos)

    trade = Trade(portfolio_id=portfolio.id, symbol=symbol, side="SELL",
                  quantity=quantity, price=price, amount_usd=proceeds,
                  realized_pnl=realized_pnl, reason=reason)
    session.add(trade)
    await session.commit()
    log.info("trade.sell", portfolio_id=portfolio.id, symbol=symbol, qty=quantity, pnl=realized_pnl)
    return trade


async def apply_stop_loss_and_take_profit(
    session: AsyncSession, portfolio: Portfolio, prices: dict[str, float],
    stop_loss_pct: float, take_profit_pct: float,
) -> list[Trade]:
    triggered = []
    for pos in await get_positions(session, portfolio.id):
        price = prices.get(pos.symbol)
        if not price:
            continue
        change = (price - pos.avg_cost) / pos.avg_cost
        if change <= -stop_loss_pct:
            t = await execute_sell(session, portfolio, pos.symbol, None, price,
                                   f"stop-loss triggered ({change*100:.1f}%)")
            if t:
                triggered.append(t)
        elif change >= take_profit_pct:
            t = await execute_sell(session, portfolio, pos.symbol, None, price,
                                   f"take-profit triggered ({change*100:.1f}%)")
            if t:
                triggered.append(t)
    return triggered


async def apply_claude_actions(
    session: AsyncSession, portfolio: Portfolio, actions: list[dict],
    prices: dict[str, float], max_positions: int,
    max_position_pct: float, min_cash_reserve_pct: float,
) -> list[Trade]:
    positions = await get_positions(session, portfolio.id)
    pos_count = len(positions)
    pos_symbols = {p.symbol for p in positions}
    total_value = portfolio.current_cash + sum(
        p.quantity * prices.get(p.symbol, p.avg_cost) for p in positions
    )
    trades = []

    for action in actions:
        act = action.get("action", "").upper()
        symbol = action.get("symbol", "").upper()
        reason = action.get("reason", "AI decision")

        if act == "BUY":
            if pos_count >= max_positions and symbol not in pos_symbols:
                log.info("trade.skipped", reason="max_positions", symbol=symbol)
                continue
            max_allowed = total_value * max_position_pct
            available = portfolio.current_cash - total_value * min_cash_reserve_pct
            if available < 1.0:
                log.info("trade.skipped", reason="cash_reserve", symbol=symbol, available=round(available, 2))
                continue
            price = prices.get(symbol, 0.0)
            if price <= 0:
                log.info("trade.skipped", reason="no_price", symbol=symbol)
                continue
            amount = min(float(action.get("amount_usd", 0) or 0), max_allowed, available)
            if amount < 1.0:
                log.info("trade.skipped", reason="amount_zero", symbol=symbol, amount_usd_from_ai=action.get("amount_usd"))
                continue
            t = await execute_buy(session, portfolio, symbol, amount, price, reason)
            if t:
                trades.append(t)
                if symbol not in pos_symbols:
                    pos_count += 1
                    pos_symbols.add(symbol)

        elif act == "SELL":
            price = prices.get(symbol, 0.0)
            if price <= 0:
                log.info("trade.skipped", reason="no_price", symbol=symbol)
                continue
            t = await execute_sell(session, portfolio, symbol,
                                   action.get("quantity"), price, reason)
            if t:
                trades.append(t)

    return trades


async def record_snapshot(
    session: AsyncSession, portfolio: Portfolio, prices: dict[str, float]
) -> PortfolioSnapshot:
    positions = await get_positions(session, portfolio.id)
    invested = sum(p.quantity * prices.get(p.symbol, p.avg_cost) for p in positions)
    total = portfolio.current_cash + invested
    basis = portfolio.total_deposited if portfolio.total_deposited > 0 else portfolio.initial_capital
    pnl = total - basis
    pnl_pct = pnl / basis * 100 if basis > 0 else 0
    snap = PortfolioSnapshot(
        portfolio_id=portfolio.id,
        total_value=round(total, 2),
        cash=round(portfolio.current_cash, 2),
        invested=round(invested, 2),
        pnl=round(pnl, 2),
        pnl_pct=round(pnl_pct, 4),
    )
    session.add(snap)
    await session.commit()
    return snap
