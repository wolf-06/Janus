"""Leases: heartbeat and reaper.

A claim stamps ``lease_expires_at``. While a worker is processing, a heartbeat extends the
lease every few seconds; if the worker dies (``kill -9``), the lease stops being extended
and a **reaper** returns the job to ``queued`` so another worker can pick it up. This is
what makes the pipeline crash-safe without holding a database transaction open across the
(long) processing step.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from janus.db.models import Job
from janus.jobs.state import JobStatus

LEASE_EXPIRED = "lease expired (worker crashed?)"


async def heartbeat(
    session: AsyncSession,
    *,
    job_id: int,
    worker_id: str,
    now: datetime | None = None,
    lease_seconds: float = 60.0,
) -> bool:
    """Extend the lease if this worker still owns the job. Returns whether it did."""
    now = now or datetime.now(UTC)
    job = await session.get(Job, job_id)
    if job is None or job.state != JobStatus.RUNNING.value or job.locked_by != worker_id:
        return False
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    await session.flush()
    return True


async def reap_expired(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Requeue running jobs whose lease has expired; return how many were reaped.

    ``SKIP LOCKED`` keeps two reapers from double-requeuing the same row. ``attempts`` is
    not incremented here — it was counted at claim time — so a crash does not silently
    consume a retry.
    """
    now = now or datetime.now(UTC)
    statement = (
        select(Job)
        .where(Job.state == JobStatus.RUNNING.value)
        .where(Job.lease_expires_at.is_not(None))
        .where(Job.lease_expires_at < now)
        .with_for_update(skip_locked=True)
    )
    jobs = list((await session.scalars(statement)).all())
    for job in jobs:
        job.state = JobStatus.QUEUED.value
        job.locked_by = None
        job.lease_expires_at = None
        job.last_error = LEASE_EXPIRED
    await session.flush()
    return len(jobs)
