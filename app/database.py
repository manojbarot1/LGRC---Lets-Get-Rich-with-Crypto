from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from app.config import get_settings

_engine = None
_session_factory = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(settings.database_url, echo=False)
    return _engine


def get_session_factory() -> async_sessionmaker:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_session() -> AsyncSession:
    async with get_session_factory()() as session:
        yield session


async def init_db():
    import os
    from sqlalchemy import select
    from app.models import (
        AISettings, AnalysisLog, Base, CashTransaction,
        Portfolio, PortfolioSnapshot, Position, Trade, User,
    )

    os.makedirs("data", exist_ok=True)
    engine = get_engine()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Migration: if portfolios exist without user_id, assign them to a default admin
    async with get_session_factory()() as session:
        orphaned = (await session.execute(
            select(Portfolio).where(Portfolio.user_id == None)
        )).scalars().all()

        if orphaned:
            from app.auth import hash_password
            admin = (await session.execute(
                select(User).where(User.username == "admin")
            )).scalar_one_or_none()

            if not admin:
                admin = User(username="admin", password_hash=hash_password("admin123"))
                session.add(admin)
                await session.flush()

            first_pid = orphaned[0].id
            for p in orphaned:
                p.user_id = admin.id

            for Model in (Position, Trade, CashTransaction, PortfolioSnapshot, AnalysisLog):
                rows = (await session.execute(
                    select(Model).where(Model.portfolio_id == None)
                )).scalars().all()
                for row in rows:
                    row.portfolio_id = first_pid

            for ai in (await session.execute(
                select(AISettings).where(AISettings.user_id == None)
            )).scalars().all():
                ai.user_id = admin.id

            await session.commit()
            print("DB migration: existing data assigned to 'admin' (password: admin123) — please change it.")
