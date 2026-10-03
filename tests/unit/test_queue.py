"""Queue enqueue idempotency and claim/lease semantics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from janus.db.models import Job
from janus.jobs.queue import claim_next, enqueue
from janus.jobs.state import JobStatus


async def test_enqueue_creates_queued_job(session: AsyncSession, seeded_run: object) -> None:
    job = await enqueue(session, delivery_id="d1", repo_id=2, run_id=3)
    await session.commit()

    assert job.state == JobStatus.QUEUED.value
    assert job.delivery_id == "d1"
    assert job.attempts == 0


async def test_enqueue_is_idempotent_on_delivery(session: AsyncSession, seeded_run: object) -> None:
    first = await enqueue(session, delivery_id="d1", repo_id=2, run_id=3)
    second = await enqueue(session, delivery_id="d1", repo_id=2, run_id=3)
    await session.commit()

    assert first.id == second.id
    assert await session.scalar(select(func.count()).select_from(Job)) == 1


async def test_claim_next_stamps_lease(session: AsyncSession, seeded_run: object) -> None:
    await enqueue(session, delivery_id="d1", repo_id=2, run_id=3)
    await session.commit()

    now = datetime.now(UTC)
    claimed = await claim_next(session, worker_id="w1", now=now, lease_seconds=30.0)
    await session.commit()

    assert claimed is not None
    assert claimed.state == JobStatus.RUNNING.value
    assert claimed.attempts == 1
    assert claimed.locked_by == "w1"
    assert claimed.lease_expires_at is not None
    assert claimed.lease_expires_at > now


async def test_claim_next_returns_none_when_empty(
    session: AsyncSession, seeded_run: object
) -> None:
    assert await claim_next(session, worker_id="w1") is None


async def test_claim_next_respects_run_after(session: AsyncSession, seeded_run: object) -> None:
    job = await enqueue(session, delivery_id="d1", repo_id=2, run_id=3)
    job.run_after = datetime.now(UTC) + timedelta(minutes=5)
    await session.commit()

    assert await claim_next(session, worker_id="w1") is None
    later = datetime.now(UTC) + timedelta(minutes=10)
    assert await claim_next(session, worker_id="w1", now=later) is not None
