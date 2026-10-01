"""Async Postgres-backed job queue.

Two operations, both concurrency-safe by construction:

- ``enqueue`` dedupes on ``delivery_id`` (one job per delivery), so a redelivered webhook
  or a re-enqueue returns the existing job.
- ``claim_next`` runs ``SELECT ... FOR UPDATE SKIP LOCKED`` and, in the same statement,
  moves the row to ``running`` and stamps a lease. N workers never claim the same job.

``claim_next`` flushes but does not commit; the caller commits the claim in its own short
transaction and then processes outside any transaction (see ``worker.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from janus.db.models import Job
from janus.jobs.state import JobStatus


async def enqueue(
    session: AsyncSession,
    *,
    delivery_id: str,
    repo_id: int,
    run_id: int,
    fingerprint: str | None = None,
    max_attempts: int = 5,
) -> Job:
    """Return the job for ``delivery_id``, creating it if absent."""
    existing = await session.scalar(select(Job).where(Job.delivery_id == delivery_id))
    if existing is not None:
        return existing
    job = Job(
        delivery_id=delivery_id,
        repo_id=repo_id,
        run_id=run_id,
        fingerprint=fingerprint,
        state=JobStatus.QUEUED.value,
        max_attempts=max_attempts,
    )
    session.add(job)
    await session.flush()
    return job


async def claim_next(
    session: AsyncSession,
    *,
    worker_id: str,
    now: datetime | None = None,
    lease_seconds: float = 60.0,
) -> Job | None:
    """Claim the oldest eligible queued job, or ``None`` if none is available.

    Eligible = ``state='queued'`` and ``run_after`` null/past. The row is locked
    ``FOR UPDATE SKIP LOCKED`` and moved to ``running`` with a lease in the same
    transaction.
    """
    now = now or datetime.now(UTC)
    statement = (
        select(Job)
        .where(Job.state == JobStatus.QUEUED.value)
        .where(or_(Job.run_after.is_(None), Job.run_after <= now))
        .order_by(Job.run_after.asc().nullsfirst(), Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = await session.scalar(statement)
    if job is None:
        return None
    job.state = JobStatus.RUNNING.value
    job.attempts += 1
    job.locked_by = worker_id
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    await session.flush()
    return job


async def queued_depth(session: AsyncSession) -> int:
    """Number of queued jobs (used for ingest backpressure)."""
    count = await session.scalar(
        select(func.count()).select_from(Job).where(Job.state == JobStatus.QUEUED.value)
    )
    return int(count or 0)
