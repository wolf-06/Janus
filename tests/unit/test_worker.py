"""Worker loop: retries, caps, quotas and the kill switch."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from janus.app.config import Settings
from janus.db.models import Job
from janus.jobs.caps import Caps, RepoQuota, set_kill_switch
from janus.jobs.queue import enqueue
from janus.jobs.state import JobStatus
from janus.jobs.worker import JobResult, Worker


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)


def _job(session: Session) -> Job:
    return session.scalars(select(Job)).one()


def test_successful_job_is_marked_succeeded(
    session_factory: sessionmaker[Session], seeded_run: object
) -> None:
    with session_factory() as session:
        enqueue(session, run_id=3)
        session.commit()

    worker = Worker(
        session_factory,
        lambda s, j: JobResult(status=JobStatus.SUCCEEDED),
        settings=_settings(),
    )

    assert worker.run_once() is True
    with session_factory() as session:
        assert _job(session).status == JobStatus.SUCCEEDED.value


def test_retryable_failure_is_bounded(
    session_factory: sessionmaker[Session], seeded_run: object
) -> None:
    with session_factory() as session:
        enqueue(session, run_id=3)
        session.commit()

    clock = FakeClock()
    caps = Caps(max_attempts=2, backoff_base_seconds=1.0)
    worker = Worker(
        session_factory,
        lambda s, j: JobResult(status=JobStatus.FAILED, retryable=True, detail="boom"),
        settings=_settings(),
        caps=caps,
        clock=clock,
    )

    assert worker.run_once() is True
    with session_factory() as session:
        job = _job(session)
        assert job.status == JobStatus.QUEUED.value
        assert job.attempts == 1
        assert job.last_error == "boom"

    clock.advance(60)
    assert worker.run_once() is True
    with session_factory() as session:
        job = _job(session)
        assert job.status == JobStatus.FAILED.value
        assert job.attempts == 2


def test_cost_cap_routes_to_comment_only(
    session_factory: sessionmaker[Session], seeded_run: object
) -> None:
    with session_factory() as session:
        enqueue(session, run_id=3)
        session.commit()

    worker = Worker(
        session_factory,
        lambda s, j: JobResult(status=JobStatus.SUCCEEDED, cost_minutes=5.0),
        settings=_settings(),
        caps=Caps(max_sandbox_minutes=1.0),
    )

    worker.run_once()
    with session_factory() as session:
        job = _job(session)
        assert job.status == JobStatus.COMMENT_ONLY.value
        assert "sandbox minutes" in (job.last_error or "")


def test_processor_exception_is_retryable_then_failed(
    session_factory: sessionmaker[Session], seeded_run: object
) -> None:
    with session_factory() as session:
        enqueue(session, run_id=3)
        session.commit()

    def boom(session: Session, job: Job) -> JobResult:
        raise RuntimeError("kaboom")

    worker = Worker(session_factory, boom, settings=_settings(), caps=Caps(max_attempts=1))

    worker.run_once()
    with session_factory() as session:
        job = _job(session)
        assert job.status == JobStatus.FAILED.value
        assert "kaboom" in (job.last_error or "")


def test_db_kill_switch_stops_claiming(
    session_factory: sessionmaker[Session], seeded_run: object
) -> None:
    with session_factory() as session:
        enqueue(session, run_id=3)
        set_kill_switch(session, enabled=True)
        session.commit()

    worker = Worker(session_factory, settings=_settings())

    assert worker.run_once() is False
    with session_factory() as session:
        assert _job(session).status == JobStatus.QUEUED.value


def test_settings_kill_switch_stops_claiming(
    session_factory: sessionmaker[Session], seeded_run: object
) -> None:
    with session_factory() as session:
        enqueue(session, run_id=3)
        session.commit()

    worker = Worker(session_factory, settings=_settings(kill_switch=True))

    assert worker.run_once() is False


def test_repo_quota_routes_to_comment_only(
    session_factory: sessionmaker[Session], seeded_run: object
) -> None:
    with session_factory() as session:
        enqueue(session, run_id=3, idempotency_key="k1")
        enqueue(session, run_id=3, idempotency_key="k2")
        session.commit()

    worker = Worker(
        session_factory,
        settings=_settings(),
        quota=RepoQuota(max_jobs=1, window_minutes=60),
    )

    assert worker.run_once() is True
    with session_factory() as session:
        statuses = sorted(job.status for job in session.scalars(select(Job)))
        assert statuses == [JobStatus.COMMENT_ONLY.value, JobStatus.QUEUED.value]
