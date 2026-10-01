"""Async database engine and session management.

The engine is created lazily so importing this module never opens a connection.
``pool_pre_ping`` drops dead connections. Sessions are async (asyncpg); Alembic keeps a
separate synchronous engine (see ``migrations/env.py``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from janus.app.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


def _factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(), autoflush=False, expire_on_commit=False
        )
    return _session_factory


def AsyncSessionLocal() -> AsyncSession:
    """Return a new async session bound to the engine."""
    return _factory()()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an async session that is always closed."""
    session = AsyncSessionLocal()
    try:
        yield session
    finally:
        await session.close()
