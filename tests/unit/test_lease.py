"""Leases: heartbeat and reaper."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from janus.jobs.lease import heartbeat, reap_expired
from janus.jobs.queue import claim_next, enqueue
from janus.jobs.state import JobStatus


async def _claimed(session: AsyncSession, now: datetime, *, lease_seconds: float = 10.0):
    await enqueue(session, delivery_id="d1", repo_id=2, run_id=3)
    await session.commit()
    job = await claim_next(session, worker_id="w1", now=now, lease_seconds=lease_seconds)
    await session.commit()
    assert job is not None
    return job


async def test_heartbeat_extends_lease(session: AsyncSession, seeded_run: object) -> None:
    now = datetime.now(UTC)
    job = await _claimed(session, now)

    extended = await heartbeat(
        session, job_id=job.id, worker_id="w1", now=now + timedelta(seconds=5), lease_seconds=10
    )
    await session.commit()

    assert extended is True
    assert job.lease_expires_at == now + timedelta(seconds=15)


async def test_heartbeat_ignored_for_other_worker(
    session: AsyncSession, seeded_run: object
) -> None:
    now = datetime.now(UTC)
    job = await _claimed(session, now)

    assert (
        await heartbeat(session, job_id=job.id, worker_id="other", now=now, lease_seconds=10)
        is False
    )


async def test_reaper_requeues_expired_lease(session: AsyncSession, seeded_run: object) -> None:
    now = datetime.now(UTC)
    job = await _claimed(session, now, lease_seconds=10)

    assert await reap_expired(session, now=now + timedelta(seconds=5)) == 0
    assert await reap_expired(session, now=now + timedelta(seconds=20)) == 1
    await session.commit()

    assert job.state == JobStatus.QUEUED.value
    assert job.locked_by is None
    assert job.lease_expires_at is None
    assert job.attempts == 1  # a crash does not consume a retry
