"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from janus.app.config import get_settings
from janus.db.models import Base, Run
from janus.db.repository import get_or_create_run, upsert_installation, upsert_repository
from janus.github.app_auth import clear_token_cache


@pytest.fixture(autouse=True)
def _clear_caches() -> Iterator[None]:
    """Reset process-global caches around every test.

    ``get_settings`` is ``lru_cache``d and installation tokens are cached in a module
    global; without this, environment changes and tokens would leak between tests.
    """
    get_settings.cache_clear()
    clear_token_cache()
    yield
    get_settings.cache_clear()
    clear_token_cache()


@pytest.fixture
def engine() -> Iterator[Engine]:
    """A fresh in-memory SQLite database with the schema created.

    ``StaticPool`` keeps a single connection so the in-memory database persists across
    the session(s) used within one test.
    """
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    """A factory that hands out fresh sessions bound to the test engine."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session


@pytest.fixture
def seeded_run(session: Session) -> Run:
    """An installation + repository + failed run, committed, ready to enqueue against."""
    upsert_installation(session, installation_id=1, account="acme", app_id=1)
    upsert_repository(
        session,
        repo_id=2,
        owner="acme",
        name="widgets",
        default_branch="main",
        installation_id=1,
    )
    run, _ = get_or_create_run(
        session,
        run_id=3,
        repo_id=2,
        head_sha="sha",
        head_branch="main",
        conclusion="failure",
        event="push",
    )
    session.commit()
    return run
