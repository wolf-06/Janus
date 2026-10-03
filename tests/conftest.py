"""Shared pytest fixtures (async).

Integration/DB tests run against in-memory async SQLite by default; Postgres-only tests
(SKIP LOCKED) are gated on ``JANUS_TEST_DATABASE_URL``. ``asyncio_mode = auto`` makes the
async fixtures/tests run without explicit decorators.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from janus.app.config import get_settings
from janus.db.models import Base, Run
from janus.db.repository import get_or_create_run, upsert_installation, upsert_repository
from janus.github.app_auth import clear_token_cache


@pytest.fixture(autouse=True)
def _clear_caches() -> Iterator[None]:
    """Reset process-global caches around every test."""
    get_settings.cache_clear()
    clear_token_cache()
    yield
    get_settings.cache_clear()
    clear_token_cache()


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """A fresh in-memory async SQLite database with the schema created."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A factory that hands out fresh async sessions bound to the test engine."""
    return async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
async def session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest.fixture
async def seeded_run(session: AsyncSession) -> Run:
    """An installation + repository + failed run, committed, ready to enqueue against."""
    await upsert_installation(session, installation_id=1, account="acme", app_id=1)
    await upsert_repository(
        session,
        repo_id=2,
        owner="acme",
        name="widgets",
        default_branch="main",
        installation_id=1,
    )
    run, _ = await get_or_create_run(
        session,
        run_id=3,
        repo_id=2,
        head_sha="sha",
        head_branch="main",
        conclusion="failure",
        event="push",
    )
    await session.commit()
    return run
