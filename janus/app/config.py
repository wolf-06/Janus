"""Application settings.

Process-wide configuration is assembled from three layers, highest priority first:
constructor arguments, environment variables (prefix ``JANUS_``), and the TOML config file
(``janus.toml``, or ``[tool.janus]`` in ``pyproject.toml``). A local ``.env`` file is also
read. Per-repository behaviour (``.janus.yml``) arrives in S1/S2; this module only covers
process-wide settings.
"""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from janus.sandbox.settings import SandboxSettings

CONFIG_FILE_ENV = "JANUS_CONFIG_FILE"


class ConfigError(RuntimeError):
    """Raised when a config file is present but cannot be read or parsed."""


def _has_janus_section(path: Path) -> bool:
    """True when ``path`` is a ``pyproject.toml`` carrying a ``[tool.janus]`` table."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    tool = data.get("tool")
    return isinstance(tool, dict) and isinstance(tool.get("janus"), dict)


def config_file_path() -> Path | None:
    """Resolve the TOML config file.

    ``JANUS_CONFIG_FILE`` wins when set; pointing it at a missing file is an error rather
    than a silent fallback. Otherwise the working directory and each of its parents are
    searched for ``janus.toml``, then for a ``pyproject.toml`` carrying ``[tool.janus]``.
    Searching upward keeps the effective configuration stable when the app is launched from
    a subdirectory. Returns ``None`` only when no config file exists at all.
    """
    override = os.environ.get(CONFIG_FILE_ENV)
    if override:
        path = Path(override)
        if not path.is_file():
            raise ConfigError(f"{CONFIG_FILE_ENV} points at a missing file: {path}")
        return path

    for directory in (Path.cwd(), *Path.cwd().parents):
        janus_toml = directory / "janus.toml"
        if janus_toml.is_file():
            return janus_toml
        pyproject = directory / "pyproject.toml"
        if pyproject.is_file() and _has_janus_section(pyproject):
            return pyproject
    return None


def load_toml_config() -> dict[str, Any]:
    """Read Janus settings from the TOML config file.

    ``janus.toml`` keeps settings at the top level; ``pyproject.toml`` keeps them under
    ``[tool.janus]``. A file that exists but cannot be read or parsed raises
    :class:`ConfigError` so a typo fails startup instead of silently reverting every
    setting to its default. Only a genuinely absent file yields an empty mapping.
    """
    path = config_file_path()
    if path is None:
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    if path.name == "pyproject.toml":
        tool = data.get("tool")
        if not isinstance(tool, dict):
            return {}
        janus = tool.get("janus")
        return janus if isinstance(janus, dict) else {}
    return data


class TomlSettingsSource(PydanticBaseSettingsSource):
    """A settings source backed by a mapping already parsed from TOML."""

    def __init__(self, settings_cls: type[BaseSettings], data: dict[str, Any]) -> None:
        super().__init__(settings_cls)
        self._data = data

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._data


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
        env_nested_delimiter="__",
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

    # --- Sandbox ---
    # Loaded from the TOML config file; see ``janus/sandbox/settings.py``.
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)

    # Which job processor the worker runs: ``stub`` (no-op) or ``sandbox`` (real work in a
    # container). The sandbox processor is still used only when ``sandbox.enabled`` is true.
    processor_mode: Literal["stub", "sandbox"] = "stub"

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

    # --- Triage (Phase 4 / S1) ---
    # Rerun each failing target this many times, then run the full suite once at the end
    # (PLAN.md §1.2).
    k_reruns: int = 5
    # The argmax route must score at least tau; if the top two are within epsilon the
    # decision is UNSURE (PLAN.md §1.2).
    tau: float = 0.80
    epsilon: float = 0.05
    # Name of the Check run Janus posts with the triage verdict.
    triage_check_name: str = "Janus triage"
    # Post the evidence comment on the PR when the failing run belongs to one.
    comment_on_pr: bool = True
    # Cap each signal's ``details`` payload so stored JSON stays bounded.
    max_signal_detail_chars: int = 2000

    # --- Fix loop (Phase 5/6 / S2) ---
    # ``suggest`` comments only; ``pr`` opens a PR with the verification table.
    fix_mode: Literal["suggest", "pr"] = "suggest"
    # Ranked suspects considered per job, and fix attempts per suspect.
    max_suspects: int = 5
    max_fix_attempts: int = 3
    # Run ``git bisect`` in the sandbox to find the introducing commit.
    bisect_enabled: bool = False
    # Documented escape hatch: when false the mutation gate is reported as passed with
    # ``measured_value="disabled"`` (and the PR body/comment shows that), instead of running
    # mutmut twice per attempt.
    mutation_gate_enabled: bool = True

    # --- LLM (used by S2 diff generation) ---
    # OpenAI-compatible endpoint; unset uses the client default. ``llm_api_key`` is a
    # secret and should come from the environment, never the TOML file.
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 60.0
    llm_max_tokens: int = 4096

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Layer the TOML file below the environment (so env still overrides it)."""
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlSettingsSource(settings_cls, load_toml_config()),
            file_secret_settings,
        )


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance.

    Caching keeps a single source of truth per process and makes the object cheap to
    use as a FastAPI dependency. Tests clear the cache when they patch the environment.
    """
    return Settings()
