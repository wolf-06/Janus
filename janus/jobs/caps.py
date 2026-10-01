"""Guardrails: cost caps, per-repo quotas, the kill switch, and retry backoff.

These answer the same question from different angles — *"should this job be allowed to
keep going?"*:

- **Cost caps** bound a single job (sandbox minutes, LLM tokens). Exceeding one is a
  terminal ``skipped_budget``: report what we have, spend no more.
- **Per-repo quotas** bound a repository (jobs per rolling window), so one noisy repo
  cannot starve the others. (Phase 2F adds the fairness/concurrency-cap form.)
- **Kill switch** is the global emergency stop, stored in the database so it takes effect
  on the next claim without a redeploy.
- **Backoff** spaces retries with **full jitter** so a thundering herd does not form.

``max_fix_retries`` is carried for the S2 fix loop; it is not consumed yet.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from janus.app.config import Settings
from janus.db.models import Job, SystemFlag

KILL_SWITCH_FLAG = "kill_switch"


@dataclass(frozen=True)
class Caps:
    """Hard per-job cost limits and backoff shape."""

    max_sandbox_minutes: float = 30.0
    max_llm_tokens: int = 200_000
    max_fix_retries: int = 3
    backoff_base_seconds: float = 30.0
    backoff_cap_seconds: float = 900.0


@dataclass(frozen=True)
class RepoQuota:
    """Per-repository rate limit over a rolling window."""

    max_jobs: int = 50
    window_minutes: int = 60


def caps_from_settings(settings: Settings) -> Caps:
    return Caps(
        max_sandbox_minutes=settings.max_sandbox_minutes,
        max_llm_tokens=settings.max_llm_tokens,
        max_fix_retries=settings.max_fix_retries,
        backoff_base_seconds=settings.worker_backoff_base_seconds,
        backoff_cap_seconds=settings.worker_backoff_cap_seconds,
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


def backoff_seconds(
    attempts: int,
    base_seconds: float,
    cap_seconds: float,
    *,
    rng: Callable[[], float] = random.random,
) -> float:
    """Full-jitter backoff: ``rand(0, min(cap, base * 2**attempts))``.

    ``attempts`` is the already-incremented attempt count (1 on the first failure).
    """
    ceiling = min(cap_seconds, base_seconds * (2.0**attempts))
    return rng() * ceiling


async def repo_quota_exceeded(
    session: AsyncSession, repo_id: int, quota: RepoQuota, *, now: datetime
) -> bool:
    """Whether ``repo_id`` has reached its job quota within the window ending at ``now``."""
    window_start = now - timedelta(minutes=quota.window_minutes)
    count = await session.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.repo_id == repo_id, Job.created_at >= window_start)
    )
    # ``count`` includes the job being claimed, so allow exactly ``max_jobs``.
    return (count or 0) > quota.max_jobs


async def is_kill_switch_engaged(session: AsyncSession) -> bool:
    """Whether the global kill switch is currently on."""
    flag = await session.get(SystemFlag, KILL_SWITCH_FLAG)
    return bool(flag and flag.enabled)


async def set_kill_switch(session: AsyncSession, *, enabled: bool) -> SystemFlag:
    """Turn the global kill switch on or off."""
    flag = await session.get(SystemFlag, KILL_SWITCH_FLAG)
    if flag is None:
        flag = SystemFlag(name=KILL_SWITCH_FLAG, enabled=enabled)
        session.add(flag)
    else:
        flag.enabled = enabled
    await session.flush()
    return flag
