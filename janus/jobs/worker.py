"""The orchestrator: a polling loop that claims and processes one job at a time.

The loop is intentionally small; the intelligence lives behind ``JobProcessor``. In
Phase 2 the default processor is a no-op, so the worker's job is to prove the mechanics:
claiming, retries with backoff, cost caps, per-repo quotas, and the kill switch.

Order of checks on each tick (fail fast, cheapest first): kill switch → claim → quota →
process → caps/retry → commit. Any exception from the processor is treated as a
retryable failure, so a buggy processor cannot wedge the queue.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from janus.app.config import Settings, get_settings
from janus.app.logging import configure_logging
from janus.db.models import Job
from janus.db.session import SessionLocal
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
from janus.jobs.queue import claim_next
from janus.jobs.state import JobStatus, assert_transition


@dataclass
class JobResult:
    """What a processor reports back about a job."""

    status: JobStatus
    cost_minutes: float = 0.0
    llm_tokens: int = 0
    retryable: bool = False
    detail: str | None = None


JobProcessor = Callable[[Session, Job], JobResult]


def default_processor(session: Session, job: Job) -> JobResult:
    """Placeholder processor until the reproduce→triage→fix pipeline exists (S1/S2)."""
    return JobResult(status=JobStatus.SUCCEEDED, detail="no-op processor (Phase 2)")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Worker:
    """Claims and processes jobs, enforcing guardrails."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        processor: JobProcessor = default_processor,
        *,
        settings: Settings | None = None,
        caps: Caps | None = None,
        quota: RepoQuota | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._session_factory = session_factory
        self._processor = processor
        self._settings = settings or get_settings()
        self._caps = caps or caps_from_settings(self._settings)
        self._quota = quota or quota_from_settings(self._settings)
        self._clock = clock

    def _now(self) -> datetime:
        return self._clock()

    def _set_status(self, job: Job, target: JobStatus, *, error: str | None = None) -> None:
        assert_transition(JobStatus(job.status), target)
        job.status = target.value
        if error is not None:
            job.last_error = error

    def run_once(self) -> bool:
        """Process at most one job. Returns whether a job was handled."""
        session = self._session_factory()
        try:
            if self._settings.kill_switch or is_kill_switch_engaged(session):
                return False

            job = claim_next(session, now=self._now())
            if job is None:
                return False
            self._set_status(job, JobStatus.RUNNING)

            if repo_quota_exceeded(session, job.run.repo_id, self._quota, now=self._now()):
                self._set_status(job, JobStatus.COMMENT_ONLY, error="per-repo quota exceeded")
                session.commit()
                return True

            result = self._process(session, job)
            self._apply(session, job, result)
            session.commit()
            return True
        finally:
            session.close()

    def run(self, *, max_iterations: int | None = None) -> None:
        """Poll forever (or ``max_iterations`` times, for tests)."""
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            handled = self.run_once()
            iterations += 1
            if not handled:
                time.sleep(self._settings.worker_poll_interval_seconds)

    def _process(self, session: Session, job: Job) -> JobResult:
        try:
            return self._processor(session, job)
        except Exception as exc:  # noqa: BLE001 - any processor failure is retryable
            return JobResult(status=JobStatus.FAILED, retryable=True, detail=str(exc))

    def _apply(self, session: Session, job: Job, result: JobResult) -> None:
        job.cost_minutes += result.cost_minutes
        job.llm_tokens += result.llm_tokens

        reason = cap_exceeded(job, self._caps)
        if reason is not None:
            self._set_status(job, JobStatus.COMMENT_ONLY, error=reason)
            return

        if result.retryable and job.attempts < self._caps.max_attempts:
            self._set_status(job, JobStatus.QUEUED, error=result.detail)
            delay = backoff_seconds(job.attempts, self._caps.backoff_base_seconds)
            job.run_after = self._now() + timedelta(seconds=delay)
            return

        if result.status == JobStatus.SUCCEEDED:
            self._set_status(job, JobStatus.SUCCEEDED)
            return

        self._set_status(job, result.status, error=result.detail)


def main() -> None:
    """Entrypoint for ``python -m janus.jobs.worker``."""
    settings = get_settings()
    configure_logging(settings.log_level)
    Worker(SessionLocal, settings=settings).run()


if __name__ == "__main__":
    main()
