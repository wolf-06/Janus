"""Application settings.

Infrastructure configuration is read from the environment (prefix ``JANUS_``) or a
local ``.env`` file. Per-repository behaviour (``.janus.yml``) arrives in S1/S2; this
module only covers process-wide settings.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide configuration.

    Only ``database_url`` and ``log_level`` have defaults so the app can boot locally
    without a ``.env``. The GitHub App fields are populated by the manifest flow (S0)
    and are therefore optional at import time.
    """

    model_config = SettingsConfigDict(
        env_prefix="JANUS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://janus:janus@localhost:5432/janus"
    log_level: str = "INFO"

    github_app_id: int | None = None
    github_private_key_path: str | None = None
    github_webhook_secret: str | None = None
    github_client_id: str | None = None
    github_client_secret: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance.

    Caching keeps a single source of truth per process and makes the object cheap to
    use as a FastAPI dependency. Tests clear the cache when they patch the environment.
    """
    return Settings()
