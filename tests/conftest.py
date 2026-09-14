"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from janus.app.config import get_settings
from janus.db.models import Base
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
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session
