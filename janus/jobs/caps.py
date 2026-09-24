"""Guardrails: cost caps, per-repo quotas, the kill switch, and retry backoff.

These are the mechanisms that keep an automated agent from running away. They are
grouped because they answer the same question from different angles — *"should this job
be allowed to keep going?"*:

- **Cost caps** bound a single job (sandbox minutes, LLM tokens). Exceeding one is a
  terminal ``COMMENT_ONLY``: report what we have, spend no more.
- **Per-repo quotas** bound a repository (jobs per rolling window), so one noisy repo
  cannot starve the others.
- **Kill switch** is the global emergency stop, stored in the database so it takes effect
  on the next claim without a redeploy.
- **Backoff** spaces out retries so a transient failure is not hammered.

``max_fix_retries`` is carried here for the S2 fix loop; Phase 2 does not consume it yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from janus.app.config import Settings
from janus.db.models import Job, Run, SystemFlag

KILL_SWITCH_FLAG = "kill_switch"


@dataclass(frozen=True)
class Caps:
    """Hard per-job limits."""

    max_attempts: int = 3
    max_sandbox_minutes: float = 30.0
    max_llm_tokens: int = 200_000
    max_fix_retries: int = 3
    backoff_base_seconds: float = 30.0


@dataclass(frozen=True)
class RepoQuota:
    """Per-repository rate limit over a rolling window."""

    max_jobs: int = 50
    window_minutes: int = 60


def caps_from_settings(settings: Settings) -> Caps:
    return Caps(
        max_attempts=settings.max_job_attempts,
        max_sandbox_minutes=settings.max_sandbox_minutes,
        max_llm_tokens=settings.max_llm_tokens,
        max_fix_retries=settings.max_fix_retries,
        backoff_base_seconds=settings.worker_backoff_base_seconds,
    )


def quota_from_settings(settings: Settings) -> RepoQuota:
    return RepoQuota(
        max_jobs=settings.repo_quota_max_jobs,
        window_minutes=settings.repo_quota_window_minutes,
    )


def cap_exceeded(job: Job, caps: Caps) -> str | None:
    """Return a human-readable reason if ``job`` is over a cap, else ``None``."""
    if job.cost_minutes > caps.max_sandbox_minutes:
        return f"sandbox minutes {job.cost_minutes:.1f} > {caps.max_sandbox_minutes:.1f}"
    if job.llm_tokens > caps.max_llm_tokens:
        return f"llm tokens {job.llm_tokens} > {caps.max_llm_tokens}"
    return None


def backoff_seconds(attempts: int, base_seconds: float) -> float:
    """Exponential backoff: ``base * 2**(attempts-1)`` (attempts is 1-based here)."""
    return base_seconds * float(2 ** max(attempts - 1, 0))


def repo_quota_exceeded(session: Session, repo_id: int, quota: RepoQuota, *, now: datetime) -> bool:
    """Whether ``repo_id`` has reached its job quota within the window ending at ``now``."""
    window_start = now - timedelta(minutes=quota.window_minutes)
    count = session.scalar(
        select(func.count())
        .select_from(Job)
        .join(Run, Job.run_id == Run.run_id)
        .where(Run.repo_id == repo_id, Job.created_at >= window_start)
    )
    # ``count`` includes the job being claimed, so allow exactly ``max_jobs`` and reject
    # the next one.
    return (count or 0) > quota.max_jobs


def is_kill_switch_engaged(session: Session) -> bool:
    """Whether the global kill switch is currently on."""
    flag = session.get(SystemFlag, KILL_SWITCH_FLAG)
    return bool(flag and flag.enabled)


def set_kill_switch(session: Session, *, enabled: bool) -> SystemFlag:
    """Turn the global kill switch on or off."""
    flag = session.get(SystemFlag, KILL_SWITCH_FLAG)
    if flag is None:
        flag = SystemFlag(name=KILL_SWITCH_FLAG, enabled=enabled)
        session.add(flag)
    else:
        flag.enabled = enabled
    session.flush()
    return flag
