"""The orchestrator: an async polling loop that claims and processes jobs.

The loop is intentionally small; the intelligence lives behind ``JobProcessor``. In this
phase the default processor is a stub, so the worker's job is to prove the *mechanics*:
claiming with a lease, heartbeating while processing, retries with full jitter,
dead-lettering, cost caps, quotas, and the kill switch.

Transaction discipline (this is the crash-safety story): a claim is committed in its own
short transaction, processing runs **outside** any transaction, a background task
heartbeats the lease, and finalization is another short transaction. So a ``kill -9``
mid-processing leaves a ``running`` row whose lease expires and is reaped — never a
half-open transaction or a lost job.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from janus.app.config import Settings, get_settings
from janus.app.logging import configure_logging
from janus.db.models import Job
from janus.db.session import AsyncSessionLocal
from janus.jobs.caps import (
    Caps,
    RepoQuota,
    backoff_seconds,
    cap_exceeded,
    caps_from_settings,
    is_kill_switch_engaged,
    quota_from_settings,
    repo_quota_exceeded,
)
from janus.jobs.lease import heartbeat, reap_expired
from janus.jobs.queue import claim_next
from janus.jobs.state import JobStatus, assert_transition, is_terminal


@dataclass
class JobResult:
    """What a processor reports back about a job."""

    status: JobStatus = JobStatus.SUCCEEDED
    retryable: bool = False
    cost_minutes: float = 0.0
    llm_tokens: int = 0
    detail: str | None = None


JobProcessor = Callable[[Job], Awaitable[JobResult]]


async def stub_processor(job: Job) -> JobResult:
    """Placeholder processor until the reproduce→triage→fix brain exists (S1/S2)."""
    return JobResult(status=JobStatus.SUCCEEDED, detail="stub processor")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Worker:
    """Claims and processes jobs, enforcing leases and guardrails."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        processor: JobProcessor = stub_processor,
        *,
        settings: Settings | None = None,
        caps: Caps | None = None,
        quota: RepoQuota | None = None,
        clock: Callable[[], datetime] = _utcnow,
        worker_id: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._session_factory = session_factory
        self._processor = processor
        self._settings = settings or get_settings()
        self._caps = caps or caps_from_settings(self._settings)
        self._quota = quota or quota_from_settings(self._settings)
        self._clock = clock
        self._sleep = sleep
        self.worker_id = worker_id or f"worker-{os.getpid()}-{uuid4().hex[:8]}"

    def _now(self) -> datetime:
        return self._clock()

    async def _claim(self) -> Job | None:
        async with self._session_factory() as session:
            if self._settings.kill_switch or await is_kill_switch_engaged(session):
                return None
            job = await claim_next(
                session,
                worker_id=self.worker_id,
                now=self._now(),
                lease_seconds=self._settings.worker_lease_seconds,
            )
            await session.commit()
            return job

    async def _over_quota(self, repo_id: int) -> bool:
        async with self._session_factory() as session:
            return await repo_quota_exceeded(session, repo_id, self._quota, now=self._now())

    async def _heartbeat_loop(self, job_id: int) -> None:
        interval = self._settings.worker_heartbeat_interval_seconds
        while True:
            await self._sleep(interval)
            async with self._session_factory() as session:
                await heartbeat(
                    session,
                    job_id=job_id,
                    worker_id=self.worker_id,
                    now=self._now(),
                    lease_seconds=self._settings.worker_lease_seconds,
                )
                await session.commit()

    async def _process(self, job: Job) -> JobResult:
        try:
            return await self._processor(job)
        except Exception as exc:  # noqa: BLE001 - any processor failure is retryable
            return JobResult(status=JobStatus.FAILED, retryable=True, detail=str(exc))

    def _transition(self, job: Job, target: JobStatus, error: str | None = None) -> None:
        assert_transition(JobStatus(job.state), target)
        job.state = target.value
        job.locked_by = None
        job.lease_expires_at = None
        if error is not None:
            job.last_error = error
        if is_terminal(target):
            job.finished_at = self._now()

    async def _finalize(self, job_id: int, result: JobResult) -> None:
        async with self._session_factory() as session:
            job = await session.get(Job, job_id)
            if job is None:
                return
            job.cost_minutes += result.cost_minutes
            job.llm_tokens += result.llm_tokens

            reason = cap_exceeded(job, self._caps)
            if reason is not None:
                self._transition(job, JobStatus.SKIPPED_BUDGET, reason)
            elif result.status == JobStatus.SUCCEEDED:
                self._transition(job, JobStatus.SUCCEEDED)
            elif result.status == JobStatus.SKIPPED_BUDGET:
                self._transition(job, JobStatus.SKIPPED_BUDGET, result.detail)
            elif result.retryable and job.attempts < job.max_attempts:
                self._transition(job, JobStatus.QUEUED, result.detail)
                delay = backoff_seconds(
                    job.attempts,
                    self._caps.backoff_base_seconds,
                    self._caps.backoff_cap_seconds,
                )
                job.run_after = self._now() + timedelta(seconds=delay)
            else:
                self._transition(job, JobStatus.DEAD, result.detail)
            await session.commit()

    async def reap_once(self) -> int:
        """Requeue running jobs whose lease expired (crashed workers)."""
        async with self._session_factory() as session:
            count = await reap_expired(session, now=self._now())
            await session.commit()
            return count

    async def run_once(self) -> bool:
        """Process at most one job. Returns whether a job was handled."""
        job = await self._claim()
        if job is None:
            return False
        job_id = job.id

        if await self._over_quota(job.repo_id):
            await self._finalize(
                job_id,
                JobResult(status=JobStatus.SKIPPED_BUDGET, detail="per-repo quota exceeded"),
            )
            return True

        heartbeat_task = asyncio.create_task(self._heartbeat_loop(job_id))
        try:
            result = await self._process(job)
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

        await self._finalize(job_id, result)
        return True

    async def run(self, *, max_iterations: int | None = None) -> None:
        """Poll forever (or ``max_iterations`` times, for tests)."""
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            await self.reap_once()
            handled = await self.run_once()
            iterations += 1
            if not handled:
                await self._sleep(self._settings.worker_poll_interval_seconds)


def main() -> None:
    """Entrypoint for ``python -m janus.jobs.worker``."""
    settings = get_settings()
    configure_logging(settings.log_level)
    asyncio.run(_serve(settings))


async def _serve(settings: Settings) -> None:
    worker = Worker(AsyncSessionLocal, settings=settings)
    await worker.run()


if __name__ == "__main__":
    main()
