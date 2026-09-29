"""Queue enqueue idempotency and claim semantics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from janus.db.models import Job
from janus.jobs.queue import claim_next, enqueue
from janus.jobs.state import JobStatus


def test_enqueue_creates_queued_job(session: Session, seeded_run: object) -> None:
    job = enqueue(session, run_id=3)
    session.commit()

    assert job.status == JobStatus.QUEUED.value
    assert job.idempotency_key == "run:3"
    assert job.attempts == 0


def test_enqueue_is_idempotent(session: Session, seeded_run: object) -> None:
    first = enqueue(session, run_id=3)
    second = enqueue(session, run_id=3)
    session.commit()

    assert first.id == second.id
    assert session.scalar(select(func.count()).select_from(Job)) == 1


def test_claim_next_marks_claimed(session: Session, seeded_run: object) -> None:
    enqueue(session, run_id=3)
    session.commit()

    claimed = claim_next(session)
    session.commit()

    assert claimed is not None
    assert claimed.status == JobStatus.CLAIMED.value
    assert claimed.attempts == 1


def test_claim_next_returns_none_when_empty(session: Session, seeded_run: object) -> None:
    assert claim_next(session) is None


def test_claim_next_respects_run_after(session: Session, seeded_run: object) -> None:
    job = enqueue(session, run_id=3)
    job.run_after = datetime.now(UTC) + timedelta(minutes=5)
    session.commit()

    assert claim_next(session) is None
    later = datetime.now(UTC) + timedelta(minutes=10)
    assert claim_next(session, now=later) is not None
