"""Cost caps, per-repo quotas, kill switch and backoff."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from janus.db.models import Job
from janus.jobs.caps import (
    Caps,
    RepoQuota,
    backoff_seconds,
    cap_exceeded,
    is_kill_switch_engaged,
    repo_quota_exceeded,
    set_kill_switch,
)
from janus.jobs.queue import enqueue

CAPS = Caps(max_sandbox_minutes=10.0, max_llm_tokens=1000)


def _job(*, cost_minutes: float = 0.0, llm_tokens: int = 0) -> Job:
    return Job(
        delivery_id="d",
        repo_id=1,
        run_id=1,
        cost_minutes=cost_minutes,
        llm_tokens=llm_tokens,
    )


def test_under_caps_is_allowed() -> None:
    assert cap_exceeded(_job(cost_minutes=5.0, llm_tokens=100), CAPS) is None


def test_sandbox_minutes_cap() -> None:
    reason = cap_exceeded(_job(cost_minutes=11.0), CAPS)
    assert reason is not None and "sandbox minutes" in reason


def test_llm_tokens_cap() -> None:
    reason = cap_exceeded(_job(llm_tokens=1001), CAPS)
    assert reason is not None and "llm tokens" in reason


def test_backoff_is_full_jitter() -> None:
    # Ceiling is base * 2**attempts, capped; rng=1.0 returns the ceiling, 0.0 returns 0.
    assert backoff_seconds(1, 30.0, 900.0, rng=lambda: 1.0) == 60.0
    assert backoff_seconds(2, 30.0, 900.0, rng=lambda: 1.0) == 120.0
    assert backoff_seconds(10, 30.0, 900.0, rng=lambda: 1.0) == 900.0  # capped
    assert backoff_seconds(3, 30.0, 900.0, rng=lambda: 0.0) == 0.0


async def test_repo_quota_exceeded(session: AsyncSession, seeded_run: object) -> None:
    for i in range(2):
        await enqueue(session, delivery_id=f"d{i}", repo_id=2, run_id=3)
    await session.commit()

    quota = RepoQuota(max_jobs=1, window_minutes=60)
    assert await repo_quota_exceeded(session, 2, quota, now=datetime.now(UTC)) is True

    generous = RepoQuota(max_jobs=10, window_minutes=60)
    assert await repo_quota_exceeded(session, 2, generous, now=datetime.now(UTC)) is False


async def test_repo_quota_ignores_old_jobs(session: AsyncSession, seeded_run: object) -> None:
    job = await enqueue(session, delivery_id="d0", repo_id=2, run_id=3)
    job.created_at = datetime.now(UTC) - timedelta(hours=5)
    await session.commit()

    quota = RepoQuota(max_jobs=0, window_minutes=60)
    assert await repo_quota_exceeded(session, 2, quota, now=datetime.now(UTC)) is False


async def test_kill_switch_toggles(session: AsyncSession) -> None:
    assert await is_kill_switch_engaged(session) is False
    await set_kill_switch(session, enabled=True)
    await session.commit()
    assert await is_kill_switch_engaged(session) is True
    await set_kill_switch(session, enabled=False)
    await session.commit()
    assert await is_kill_switch_engaged(session) is False
