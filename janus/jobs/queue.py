"""Postgres-backed job queue.

Two operations, both idempotent/concurrency-safe by construction:

- ``enqueue`` dedupes on ``idempotency_key`` (default ``run:{run_id}``), so a redelivered
  webhook or a re-enqueue returns the existing job instead of creating a second one.
- ``claim_next`` uses ``SELECT ... FOR UPDATE SKIP LOCKED``: each worker locks the oldest
  eligible row and skips rows already locked by another worker. This is what makes N
  workers safe without a separate lock service — the database *is* the queue.

``claim_next`` flushes but does not commit; the caller owns the transaction, so a crash
mid-processing rolls the claim back and the job returns to ``queued``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from janus.db.models import Job
from janus.jobs.state import JobStatus


def enqueue(session: Session, *, run_id: int, idempotency_key: str | None = None) -> Job:
    """Return the queued job for ``idempotency_key``, creating it if absent."""
    key = idempotency_key or f"run:{run_id}"
    existing = session.scalar(select(Job).where(Job.idempotency_key == key))
    if existing is not None:
        return existing
    job = Job(run_id=run_id, status=JobStatus.QUEUED.value, idempotency_key=key)
    session.add(job)
    session.flush()
    return job


def claim_next(session: Session, *, now: datetime | None = None) -> Job | None:
    """Claim the oldest eligible queued job, or ``None`` if none is available.

    Eligible = ``status == queued`` and ``run_after`` is null or in the past. The row is
    locked ``FOR UPDATE SKIP LOCKED`` so concurrent workers never claim the same job.
    """
    now = now or datetime.now(UTC)
    statement = (
        select(Job)
        .where(Job.status == JobStatus.QUEUED.value)
        .where(or_(Job.run_after.is_(None), Job.run_after <= now))
        .order_by(Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = session.scalar(statement)
    if job is None:
        return None
    job.status = JobStatus.CLAIMED.value
    job.attempts += 1
    session.flush()
    return job
