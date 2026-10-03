"""Worker loop: retries, caps, quotas, kill switch and leases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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


async def _one_job(session: AsyncSession) -> Job:
    return (await session.scalars(select(Job))).one()


async def _seed_job(
    session_factory: async_sessionmaker[AsyncSession], *, max_attempts: int = 5, **kw: object
) -> None:
    async with session_factory() as session:
        await enqueue(session, delivery_id="d1", repo_id=2, run_id=3, max_attempts=max_attempts)
        await session.commit()


async def test_successful_job_is_marked_succeeded(
    session_factory: async_sessionmaker[AsyncSession], seeded_run: object
) -> None:
    await _seed_job(session_factory)

    async def ok(job: Job) -> JobResult:
        return JobResult(status=JobStatus.SUCCEEDED)

    worker = Worker(session_factory, ok, settings=_settings())
    assert await worker.run_once() is True

    async with session_factory() as session:
        assert (await _one_job(session)).state == JobStatus.SUCCEEDED.value


async def test_retryable_failure_is_bounded_then_dead(
    session_factory: async_sessionmaker[AsyncSession], seeded_run: object
) -> None:
    await _seed_job(session_factory, max_attempts=2)
    clock = FakeClock()

    async def flaky(job: Job) -> JobResult:
        return JobResult(status=JobStatus.FAILED, retryable=True, detail="boom")

    worker = Worker(
        session_factory,
        flaky,
        settings=_settings(),
        caps=Caps(backoff_base_seconds=1.0, backoff_cap_seconds=1.0),
        clock=clock,
    )

    assert await worker.run_once() is True
    async with session_factory() as session:
        job = await _one_job(session)
        assert job.state == JobStatus.QUEUED.value
        assert job.attempts == 1
        assert job.last_error == "boom"

    clock.advance(60)
    assert await worker.run_once() is True
    async with session_factory() as session:
        job = await _one_job(session)
        assert job.state == JobStatus.DEAD.value
        assert job.attempts == 2


async def test_cost_cap_routes_to_skipped_budget(
    session_factory: async_sessionmaker[AsyncSession], seeded_run: object
) -> None:
    await _seed_job(session_factory)

    async def spendy(job: Job) -> JobResult:
        return JobResult(status=JobStatus.SUCCEEDED, cost_minutes=5.0)

    worker = Worker(
        session_factory, spendy, settings=_settings(), caps=Caps(max_sandbox_minutes=1.0)
    )
    await worker.run_once()

    async with session_factory() as session:
        job = await _one_job(session)
        assert job.state == JobStatus.SKIPPED_BUDGET.value
        assert "sandbox minutes" in (job.last_error or "")


async def test_processor_exception_is_retryable_then_dead(
    session_factory: async_sessionmaker[AsyncSession], seeded_run: object
) -> None:
    await _seed_job(session_factory, max_attempts=1)

    async def boom(job: Job) -> JobResult:
        raise RuntimeError("kaboom")

    worker = Worker(session_factory, boom, settings=_settings())
    await worker.run_once()

    async with session_factory() as session:
        job = await _one_job(session)
        assert job.state == JobStatus.DEAD.value
        assert "kaboom" in (job.last_error or "")


async def test_db_kill_switch_stops_claiming(
    session_factory: async_sessionmaker[AsyncSession], seeded_run: object
) -> None:
    async with session_factory() as session:
        await enqueue(session, delivery_id="d1", repo_id=2, run_id=3)
        await set_kill_switch(session, enabled=True)
        await session.commit()

    worker = Worker(session_factory, settings=_settings())
    assert await worker.run_once() is False

    async with session_factory() as session:
        assert (await _one_job(session)).state == JobStatus.QUEUED.value


async def test_settings_kill_switch_stops_claiming(
    session_factory: async_sessionmaker[AsyncSession], seeded_run: object
) -> None:
    await _seed_job(session_factory)
    worker = Worker(session_factory, settings=_settings(kill_switch=True))
    assert await worker.run_once() is False


async def test_repo_quota_routes_to_skipped_budget(
    session_factory: async_sessionmaker[AsyncSession], seeded_run: object
) -> None:
    async with session_factory() as session:
        await enqueue(session, delivery_id="d1", repo_id=2, run_id=3)
        await enqueue(session, delivery_id="d2", repo_id=2, run_id=3)
        await session.commit()

    worker = Worker(
        session_factory,
        settings=_settings(),
        quota=RepoQuota(max_jobs=1, window_minutes=60),
    )
    assert await worker.run_once() is True

    async with session_factory() as session:
        jobs = (await session.scalars(select(Job))).all()
        assert sorted(job.state for job in jobs) == [
            JobStatus.QUEUED.value,
            JobStatus.SKIPPED_BUDGET.value,
        ]
