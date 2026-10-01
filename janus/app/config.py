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

    # Async URL (asyncpg). Alembic rewrites the driver to psycopg for sync migrations.
    database_url: str = "postgresql+asyncpg://janus:janus@localhost:5432/janus"
    log_level: str = "INFO"

    # Public base URL used to build the manifest redirect/webhook URLs. Unset locally
    # (falls back to the request base URL); set it to the tunnel URL (smee.io/ngrok)
    # when installing the App against a real GitHub account.
    public_url: str | None = None
    github_app_name: str = "Janus"

    github_app_id: int | None = None
    github_private_key_path: str | None = None
    github_webhook_secret: str | None = None
    github_client_id: str | None = None
    github_client_secret: str | None = None

    # --- Orchestrator guardrails ---
    # Hard stop for all workers, independent of the DB kill switch.
    kill_switch: bool = False
    # Retries before a job is dead-lettered.
    max_job_attempts: int = 5
    # Cost caps: exceeding any of these routes a job to skipped_budget.
    max_sandbox_minutes: float = 30.0
    max_llm_tokens: int = 200_000
    max_fix_retries: int = 3
    # Per-repo quota: at most N jobs enqueued per repo in a rolling window.
    repo_quota_max_jobs: int = 50
    repo_quota_window_minutes: int = 60
    # Worker loop cadence and retry backoff (seconds).
    worker_poll_interval_seconds: float = 5.0
    worker_backoff_base_seconds: float = 30.0
    worker_backoff_cap_seconds: float = 900.0
    # Lease/heartbeat: a claim is valid for lease_seconds and is extended every
    # heartbeat_interval_seconds. A reaper requeues jobs whose lease expired.
    worker_lease_seconds: float = 60.0
    worker_heartbeat_interval_seconds: float = 10.0
    # Ingest backpressure: return 503 when the queued depth exceeds this.
    queue_depth_limit: int = 10_000
    # Phase-2 stub processor latency (seconds); replaced by the real brain in S1/S2.
    stub_processing_seconds: float = 0.0


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance.

    Caching keeps a single source of truth per process and makes the object cheap to
    use as a FastAPI dependency. Tests clear the cache when they patch the environment.
    """
    return Settings()
