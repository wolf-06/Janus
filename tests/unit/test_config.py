"""Settings parsing: defaults, env prefix and type coercion."""

from __future__ import annotations

import pytest

from janus.app.config import Settings, get_settings


def test_defaults_when_unconfigured() -> None:
    settings = Settings(_env_file=None)
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.log_level == "INFO"
    assert settings.github_app_id is None
    assert settings.github_webhook_secret is None


def test_reads_prefixed_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JANUS_DATABASE_URL", "postgresql+psycopg://u:p@db:5432/x")
    monkeypatch.setenv("JANUS_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("JANUS_GITHUB_APP_ID", "12345")
    monkeypatch.setenv("JANUS_GITHUB_WEBHOOK_SECRET", "s3cret")

    settings = Settings(_env_file=None)

    assert settings.database_url == "postgresql+psycopg://u:p@db:5432/x"
    assert settings.log_level == "DEBUG"
    assert settings.github_app_id == 12345
    assert settings.github_webhook_secret == "s3cret"


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JANUS_LOG_LEVEL", "WARNING")
    assert get_settings().log_level == "WARNING"

    monkeypatch.setenv("JANUS_LOG_LEVEL", "ERROR")
    assert get_settings().log_level == "WARNING"
