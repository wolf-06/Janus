"""The real job processor: gather reproduction evidence, not a verdict.

Replaces ``stub_processor`` for claimed jobs. For one job it resolves the repository and
run, checks out the exact revision, materialises the adapter's commands into sandbox phase
specs, runs them, parses the outcomes, and reports back through ``JobResult``.

Scope: this is the **evidence-gathering** step only. There is no triage and no scoring
(Phase 4 owns those), and no new persistence (Phase 4 adds the reproducer's storage). A
failing test suite is the *expected* input, so it is a successful job that gathered
evidence — only an infrastructure failure (no daemon, image build failure, timeout) is a
retryable failure.

The whole blocking sequence — acquire the source, run the specs, parse — happens inside one
``asyncio.to_thread`` call, and the source context manager is exited even when a run fails,
so a checkout can never leak.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from janus.app.config import Settings
from janus.core.adapter import Adapter, TestOutcome
from janus.db.models import Job, Repository, Run
from janus.jobs.state import JobStatus
from janus.jobs.worker import JobProcessor, JobResult, stub_processor
from janus.sandbox.profiles import Profile, RunPhase, RunPlan
from janus.sandbox.runner import SandboxRunner
from janus.sandbox.spec import SandboxPhase, SandboxResult, SandboxSpec
from janus.source import SourceProvider, SourceSpec

log = structlog.get_logger(__name__)

# ``Callable[[], AsyncSession]`` matches ``Worker``'s ``session_factory`` and
# ``janus.db.session.AsyncSessionLocal``.
SessionFactory = Callable[[], AsyncSession]

_MAX_FAILING_IDS = 3
_MAX_DETAIL_CHARS = 200


def _phase_result(results: Sequence[SandboxResult], phase: SandboxPhase) -> SandboxResult | None:
    """The result for ``phase``, or ``None`` when that phase did not run."""
    return next((result for result in results if result.phase is phase), None)


def _summary(passed: int, failed: Sequence[TestOutcome], total: int) -> str:
    """A short, bounded report of the run: counts plus a few failing target ids."""
    detail = f"{passed} passed, {len(failed)} failed, {total} outcomes"
    if failed:
        ids = ", ".join(outcome.target_id for outcome in failed[:_MAX_FAILING_IDS])
        detail = f"{detail}; failing: {ids}"
    return detail[:_MAX_DETAIL_CHARS]


class SandboxProcessor:
    """Gather evidence for a job by running the adapter's commands in the sandbox.

    Every collaborator is injected so the class can be driven with fakes: the session
    factory loads the repo/run rows, the adapter translates commands and results, the source
    provider yields a checkout, and the runner executes the specs.
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        adapter: Adapter,
        source: SourceProvider,
        runner: SandboxRunner,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._adapter = adapter
        self._source = source
        self._runner = runner
        self._settings = settings

    async def __call__(self, job: Job) -> JobResult:
        source_spec = await self._source_spec(job)
        if source_spec is None:
            log.warning(
                "processor.source_missing",
                job_id=job.id,
                repo_id=job.repo_id,
                run_id=job.run_id,
            )
            return JobResult(
                status=JobStatus.FAILED,
                retryable=False,
                detail="repo or run missing",
            )

        plan = self._plan()
        specs = plan.specs(self._settings.sandbox)
        results, outcomes = await asyncio.to_thread(self._run, source_spec, specs)

        cost_minutes = sum(result.duration_s for result in results) / 60.0
        passed = sum(1 for outcome in outcomes if outcome.passed)
        failed = [outcome for outcome in outcomes if not outcome.passed]
        test_result = _phase_result(results, SandboxPhase.TEST)

        log.info(
            "processor.run_complete",
            job_id=job.id,
            repo_id=job.repo_id,
            run_id=job.run_id,
            passed=passed,
            failed=len(failed),
            outcomes=len(outcomes),
        )

        if test_result is None:
            log.warning(
                "processor.no_test_result",
                job_id=job.id,
                repo_id=job.repo_id,
                run_id=job.run_id,
            )
            return JobResult(
                status=JobStatus.FAILED,
                retryable=True,
                cost_minutes=cost_minutes,
                detail="no test phase result",
            )

        if test_result.error is not None or test_result.timed_out:
            detail = test_result.error or "timed_out"
            log.warning(
                "processor.sandbox_failure",
                job_id=job.id,
                repo_id=job.repo_id,
                run_id=job.run_id,
                detail=detail,
            )
            return JobResult(
                status=JobStatus.FAILED,
                retryable=True,
                cost_minutes=cost_minutes,
                detail=detail,
            )

        return JobResult(
            status=JobStatus.SUCCEEDED,
            cost_minutes=cost_minutes,
            detail=_summary(passed, failed, len(outcomes)),
        )

    async def _source_spec(self, job: Job) -> SourceSpec | None:
        """Load the job's repository and run; ``None`` when either row is missing."""
        async with self._session_factory() as session:
            repo = await session.get(Repository, job.repo_id)
            run = await session.get(Run, job.run_id)
            if repo is None or run is None:
                return None
            return SourceSpec(
                owner=repo.owner,
                name=repo.name,
                ref=run.head_sha,
                installation_id=repo.installation_id,
            )

    def _plan(self) -> RunPlan:
        """Build the plan from the adapter's commands, carrying argv and artifacts.

        Setup commands run only when the sandbox config allows the network (otherwise the
        setup phase is skipped entirely). The test phase runs the whole suite offline;
        failing-test targeting arrives with the reproducer in Phase 4.
        """
        sandbox = self._settings.sandbox
        phases: list[RunPhase] = []
        if sandbox.network:
            phases.extend(
                RunPhase(
                    phase=SandboxPhase.SETUP,
                    command=command.argv,
                    network=True,
                    artifacts=command.artifacts,
                )
                for command in self._adapter.setup_commands()
            )
        test = self._adapter.test_command(())
        phases.append(
            RunPhase(
                phase=SandboxPhase.TEST,
                command=test.argv,
                network=False,
                artifacts=test.artifacts,
            )
        )
        return RunPlan(profile=Profile.CLEAN, phases=tuple(phases))

    def _run(
        self, source_spec: SourceSpec, specs: Sequence[SandboxSpec]
    ) -> tuple[list[SandboxResult], list[TestOutcome]]:
        """Blocking: acquire the source, run every spec, and parse the test outcome.

        The ``with`` guarantees the checkout is removed even when a run raises, so the
        source context is always exited.
        """
        results: list[SandboxResult] = []
        with self._source.acquire(source_spec) as checkout:
            for spec in specs:
                results.append(self._runner.run(spec, source_dir=checkout))
        test_result = _phase_result(results, SandboxPhase.TEST)
        outcomes = self._adapter.parse_results(test_result) if test_result is not None else []
        return results, outcomes


def build_processor(settings: Settings, session_factory: SessionFactory) -> JobProcessor:
    """The production factory: a real sandbox processor, or the stub when disabled.

    The GitHub App auth module is imported lazily so this module does not pull it in at
    import time (it, in turn, imports the app config).
    """
    if not settings.sandbox.enabled:
        return stub_processor

    from janus.adapters.python.adapter import PythonAdapter
    from janus.github.app_auth import installation_token
    from janus.source import GitSourceProvider

    def token_provider(installation_id: int) -> str:
        return installation_token(settings, installation_id)

    return SandboxProcessor(
        session_factory=session_factory,
        adapter=PythonAdapter(),
        source=GitSourceProvider(token_provider=token_provider),
        runner=SandboxRunner(settings=settings.sandbox),
        settings=settings,
    )
