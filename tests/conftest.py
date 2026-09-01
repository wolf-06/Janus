"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from janus.app.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Reset the settings cache around every test.

    ``get_settings`` is ``lru_cache``d; without this, environment changes in one test
    would leak into the next.
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
