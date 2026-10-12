"""The real job processor: reproduce, triage, persist and post a verdict.

Replaces ``stub_processor`` for claimed jobs. For one job it resolves the repository and
run, fetches the failing run's CI log and parses the failing tests, checks out the exact
revision, reruns the failing targets K times plus the whole suite once (the reproducer),
resolves the last-green → first-red change, collects the seven triage signals, scores them
into a route, persists the evidence and the diagnosis, and posts the verdict as a Check run
(and, when the run belongs to a pull request, an evidence comment).

Scope: this is **S1**. There is no fix loop here — a ``CODE_BUG`` decision only reaches a
documented seam (:meth:`SandboxProcessor._maybe_fix`) that S2 fills in. A failing test suite
is the *expected* input, so it is a successful job that gathered evidence; only an
infrastructure failure (no daemon, image build failure, timeout, or no test-phase result) is
a retryable failure.

The whole blocking sequence — acquire the source, run the reproducer, resolve the change,
collect the signals and score — happens inside one ``asyncio.to_thread`` call, and the source
context manager is entered and exited inside that thread, so a checkout can never leak. The
two steps that cannot run in that thread are the async database reads: the flake history is
read first, in a short-lived session, and the log fetch that yields the failing targets is
done just before the thread so those targets are known when the history is read. An
unexpected error anywhere is turned into a retryable failure; the pipeline never raises.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from janus.app.config import Settings
from janus.config.janus_yml import RepoConfig, RepoConfigError, load_repo_config
from janus.core.adapter import Adapter, TestTarget
from janus.core.evidence import ChangeSummary, CommitSummary, Reproduction
from janus.core.reproducer import Reproducer, ReproduceRequest
from janus.core.signals import collect_all, default_collectors
from janus.core.signals.base import Collector, SignalContext, SignalEvidence
from janus.core.triage.routes import Route, TriageDecision
from janus.core.triage.scorer import score
from janus.db.models import Job, Repository, Run, Signal
from janus.db.models import Reproduction as ReproductionRow
from janus.db.repository import add_reproductions, add_signals, flake_history, set_diagnosis
from janus.github.client import GitHubClient
from janus.github.logs import extract_log_text
from janus.jobs.state import JobStatus
from janus.jobs.worker import JobProcessor, JobResult, stub_processor
from janus.report.comment import check_conclusion, render_check_output, render_evidence
from janus.sandbox.profiles import Profile
from janus.sandbox.runner import SandboxRunner
from janus.source import SourceProvider, SourceSpec

log = structlog.get_logger(__name__)

# ``Callable[[], AsyncSession]`` matches ``Worker``'s ``session_factory`` and
# ``janus.db.session.AsyncSessionLocal``.
SessionFactory = Callable[[], AsyncSession]

# ``installation_id -> GitHubClient``. A callable so the GitHub App auth module can mint a
# token lazily (and so tests can inject a fake client).
GitHubClientFactory = Callable[[int], GitHubClient]

_MAX_DETAIL_CHARS = 200


@dataclass(frozen=True)
class _PipelineResult:
    """The plain object the blocking thread hands back to the async half.

    It carries the measured evidence (reproduction, change), the signal context and evidence
    the collectors produced, the triage decision, the failing targets and the per-repo
    config, so the async half can persist and post without touching the sandbox again.
    """

    reproduction: Reproduction
    change: ChangeSummary
    targets: tuple[TestTarget, ...]
    ctx: SignalContext
    evidence: tuple[SignalEvidence, ...]
    decision: TriageDecision
    repo_config: RepoConfig

    @property
    def infra_failure(self) -> bool:
        """True when the sandbox itself failed (no test result, an error, or a timeout).

        The full-suite run is the last test-phase result, so a ``sandbox_error`` on it means
        the container never produced a usable outcome. A normal red suite has no error, so it
        is *not* an infrastructure failure.
        """
        full_suite = self.reproduction.full_suite
        return full_suite is None or full_suite.sandbox_error is not None


def _reproduction_rows(job_id: int, reproduction: Reproduction) -> list[ReproductionRow]:
    """One ``reproductions`` row per attempt, plus the full-suite attempt (``target_id`` NULL)."""
    rows = [
        _row_from_attempt(job_id, reproduction.profile.value, attempt)
        for attempt in reproduction.attempts
    ]
    if reproduction.full_suite is not None:
        rows.append(_row_from_attempt(job_id, reproduction.profile.value, reproduction.full_suite))
    return rows


def _row_from_attempt(job_id: int, profile: str, attempt: Any) -> ReproductionRow:
    return ReproductionRow(
        job_id=job_id,
        profile=profile,
        attempt_index=attempt.index,
        target_id=attempt.target_id,
        passed=attempt.passed,
        duration_s=attempt.duration_s,
        exit_code=attempt.exit_code,
        timed_out=attempt.timed_out,
        oom_killed=attempt.oom_killed,
        cpu_seconds=attempt.cpu_seconds,
        peak_mem_mb=attempt.peak_mem_mb,
        stdout_excerpt=attempt.stdout_excerpt,
        stderr_excerpt=attempt.stderr_excerpt,
    )


def _bound_details(details: Mapping[str, Any], limit: int) -> dict[str, Any]:
    """Bound every string in a signal's ``details`` so the stored JSON stays small."""
    return {key: _bound_value(value, limit) for key, value in details.items()}


def _bound_value(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, Mapping):
        return {key: _bound_value(item, limit) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_bound_value(item, limit) for item in value]
    return value


def _commit_summaries(raw: Any) -> tuple[CommitSummary, ...]:
    """Turn the commits API's list into ``CommitSummary`` values (total, never raises)."""
    if not isinstance(raw, list):
        return ()
    summaries: list[CommitSummary] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        sha = item.get("sha")
        if not isinstance(sha, str) or not sha:
            continue
        message = ""
        commit = item.get("commit")
        if isinstance(commit, Mapping):
            raw_message = commit.get("message")
            if isinstance(raw_message, str):
                message = raw_message
        files = item.get("files")
        paths: tuple[str, ...] = ()
        if isinstance(files, list):
            paths = tuple(
                entry["filename"]
                for entry in files
                if isinstance(entry, Mapping) and isinstance(entry.get("filename"), str)
            )
        summaries.append(CommitSummary(sha=sha, message=message, files=paths))
    return tuple(summaries)


def _changed_files(raw: Any) -> tuple[str, ...]:
    """The unique filenames in the compare API's ``files`` list, sorted."""
    if not isinstance(raw, list):
        return ()
    return tuple(
        sorted(
            {
                entry["filename"]
                for entry in raw
                if isinstance(entry, Mapping) and isinstance(entry.get("filename"), str)
            }
        )
    )


def _diff_excerpt(raw: Any, limit: int) -> str:
    """A bounded concatenation of the compare API's per-file patches."""
    if not isinstance(raw, list):
        return ""
    parts = [
        entry["patch"]
        for entry in raw
        if isinstance(entry, Mapping) and isinstance(entry.get("patch"), str) and entry["patch"]
    ]
    return "\n".join(parts)[:limit]


def _infra_detail(reproduction: Reproduction) -> str:
    """A short reason an infrastructure failure occurred, for the ``JobResult`` detail."""
    full_suite = reproduction.full_suite
    if full_suite is None:
        return "no test phase result"
    return full_suite.sandbox_error or "sandbox failure"


def _success_detail(result: _PipelineResult) -> str:
    """The bounded route/confidence/counts summary reported on a successful job."""
    decision = result.decision
    detail = (
        f"{decision.route.value} confidence={decision.confidence:.2f} "
        f"gap={decision.top2_gap:.2f} attempts={len(result.reproduction.attempts)} "
        f"signals={len(result.evidence)}"
    )
    return detail[:_MAX_DETAIL_CHARS]


class SandboxProcessor:
    """Reproduce a job's failure, triage it, persist the evidence and post the verdict.

    Every collaborator is injected so the class can be driven with fakes: the session
    factory loads the repo/run rows and reads/writes evidence, the adapter translates CI logs
    and sandbox results, the source provider yields a checkout, the runner executes the
    sandbox specs, the GitHub client factory mints per-installation clients, and the
    reproducer and collectors can be overridden for tests.
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        adapter: Adapter,
        source: SourceProvider,
        runner: SandboxRunner,
        settings: Settings,
        github_client: GitHubClientFactory,
        reproducer: Reproducer | None = None,
        collectors: Sequence[Collector] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._adapter = adapter
        self._source = source
        self._runner = runner
        self._settings = settings
        self._github_client = github_client
        self._reproducer = (
            reproducer
            if reproducer is not None
            else Reproducer(adapter=adapter, runner=runner, settings=settings)
        )
        self._collectors: tuple[Collector, ...] = (
            tuple(collectors) if collectors is not None else default_collectors()
        )

    async def __call__(self, job: Job) -> JobResult:
        loaded = await self._load(job)
        if loaded is None:
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
        repo, run = loaded

        client = self._github_client(repo.installation_id)
        try:
            targets = self._fetch_targets(client, repo, run)
            flake = await self._read_flake_history(repo.repo_id, [target.id for target in targets])
            result = await asyncio.to_thread(self._run_pipeline, client, repo, run, targets, flake)
        except Exception as exc:  # noqa: BLE001 - the pipeline never raises
            log.warning("processor.pipeline_failed", job_id=job.id, error=str(exc))
            client.close()
            return JobResult(status=JobStatus.FAILED, retryable=True, detail=str(exc))

        cost_minutes = result.reproduction.cost_minutes

        if result.infra_failure:
            detail = _infra_detail(result.reproduction)
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

        try:
            await self._persist(job.id, result)
        except Exception as exc:  # noqa: BLE001 - a persistence failure is retryable
            log.warning("processor.persist_failed", job_id=job.id, error=str(exc))
            return JobResult(
                status=JobStatus.FAILED,
                retryable=True,
                cost_minutes=cost_minutes,
                detail=f"persist failed: {exc}",
            )

        try:
            await self._post(repo, run, result)
        except Exception as exc:  # noqa: BLE001 - posting is best-effort
            log.warning("processor.post_failed", job_id=job.id, error=str(exc))

        if result.decision.route is Route.CODE_BUG:
            await self._maybe_fix(result.ctx, result.decision)

        log.info(
            "processor.run_complete",
            job_id=job.id,
            repo_id=job.repo_id,
            run_id=job.run_id,
            route=result.decision.route.value,
            confidence=result.decision.confidence,
            attempts=len(result.reproduction.attempts),
            signals=len(result.evidence),
        )
        return JobResult(
            status=JobStatus.SUCCEEDED,
            cost_minutes=cost_minutes,
            detail=_success_detail(result),
        )

    async def _load(self, job: Job) -> tuple[Repository, Run] | None:
        """Load the job's repository and run; ``None`` when either row is missing."""
        async with self._session_factory() as session:
            repo = await session.get(Repository, job.repo_id)
            run = await session.get(Run, job.run_id)
            if repo is None or run is None:
                return None
            return repo, run

    def _fetch_targets(
        self, client: GitHubClient, repo: Repository, run: Run
    ) -> tuple[TestTarget, ...]:
        """Download and parse the failing run's log; a fetch/parse failure yields ``()``.

        The log is untrusted input and the fetch is a network call, so any failure degrades to
        an empty target list — the reproducer then runs only the full suite — rather than
        failing the job.
        """
        try:
            archive = client.download_run_logs(repo.owner, repo.name, run.run_id)
            return tuple(self._adapter.parse_failing_tests(extract_log_text(archive)))
        except Exception as exc:  # noqa: BLE001 - a log fetch must never fail the job
            log.warning(
                "processor.log_fetch_failed",
                repo_id=repo.repo_id,
                run_id=run.run_id,
                error=str(exc),
            )
            return ()

    async def _read_flake_history(
        self, repo_id: int, target_ids: Sequence[str]
    ) -> dict[str, float]:
        """Read each failing target's historical failure rate in a short-lived session."""
        async with self._session_factory() as session:
            return await flake_history(session, repo_id, list(target_ids))

    def _run_pipeline(
        self,
        client: GitHubClient,
        repo: Repository,
        run: Run,
        targets: tuple[TestTarget, ...],
        flake: Mapping[str, float],
    ) -> _PipelineResult:
        """Blocking: acquire the source, reproduce, resolve the change, collect and score.

        The ``with`` guarantees the checkout is removed even when a run raises, so the source
        context is always exited, and the client is closed in ``finally``.
        """
        try:
            source_spec = SourceSpec(
                owner=repo.owner,
                name=repo.name,
                ref=run.head_sha,
                installation_id=repo.installation_id,
            )
            with self._source.acquire(source_spec) as checkout:
                repo_config = self._load_repo_config(checkout)
                reproduction = self._reproducer.reproduce(
                    checkout,
                    ReproduceRequest(
                        targets=targets,
                        k=repo_config.k_reruns or self._settings.k_reruns,
                        profile=Profile.CLEAN,
                    ),
                )
                change = self._resolve_change(client, repo, run)
                ctx = SignalContext(
                    reproduction=reproduction,
                    change=change,
                    failing_targets=tuple(target.id for target in targets),
                    flake_history=flake,
                    # Empty for now: richer CI correlation facts (runner, matrix leg, cache
                    # hit) are not fetched yet and arrive with the correlation signal work.
                    metadata={},
                )
                evidence = tuple(collect_all(self._collectors, ctx))
                decision = score(
                    evidence,
                    tau=repo_config.tau or self._settings.tau,
                    epsilon=repo_config.epsilon or self._settings.epsilon,
                )
            return _PipelineResult(
                reproduction=reproduction,
                change=change,
                targets=targets,
                ctx=ctx,
                evidence=evidence,
                decision=decision,
                repo_config=repo_config,
            )
        finally:
            client.close()

    def _load_repo_config(self, checkout: Path) -> RepoConfig:
        """Load the checkout's ``.janus.yml``; a malformed file logs and uses the defaults."""
        try:
            return load_repo_config(checkout / ".janus.yml")
        except RepoConfigError as exc:
            log.warning("processor.repo_config_invalid", error=str(exc))
            return RepoConfig()

    def _resolve_change(self, client: GitHubClient, repo: Repository, run: Run) -> ChangeSummary:
        """Build the last-green → first-red change; any failure makes it unavailable.

        With no ``baseline_sha`` the range is unknown, so ``available`` is false. Otherwise the
        compare API supplies the commits and files, ``list_commits`` backfills the commit list
        when the compare payload omits it, and the diff is bounded to the evidence size cap.
        Every failure degrades to a ``base_sha=None`` summary rather than raising.
        """
        head_sha = run.head_sha
        base_sha = run.baseline_sha
        if not base_sha:
            return ChangeSummary(base_sha=None, head_sha=head_sha)
        try:
            comparison = client.compare_commits(repo.owner, repo.name, base_sha, head_sha)
            files_raw = comparison.get("files") if isinstance(comparison, Mapping) else None
            commits = _commit_summaries(
                comparison.get("commits") if isinstance(comparison, Mapping) else None
            )
            files_changed = _changed_files(files_raw)
            if not commits:
                commits = _commit_summaries(
                    client.list_commits(repo.owner, repo.name, sha=head_sha)
                )
            if not files_changed:
                files_changed = tuple(sorted({path for commit in commits for path in commit.files}))
            return ChangeSummary(
                base_sha=base_sha,
                head_sha=head_sha,
                commits=commits,
                files_changed=files_changed,
                diff_excerpt=_diff_excerpt(files_raw, self._settings.max_signal_detail_chars),
            )
        except Exception as exc:  # noqa: BLE001 - change attribution is best-effort
            log.warning(
                "processor.change_unavailable",
                repo_id=repo.repo_id,
                run_id=run.run_id,
                error=str(exc),
            )
            return ChangeSummary(base_sha=None, head_sha=head_sha)

    async def _persist(self, job_id: int, result: _PipelineResult) -> None:
        """Write the reproductions, signals and diagnosis in one transaction."""
        async with self._session_factory() as session:
            await add_reproductions(
                session, job_id, _reproduction_rows(job_id, result.reproduction)
            )
            signals = [
                Signal(
                    job_id=job_id,
                    name=evidence.name,
                    summary=evidence.summary,
                    details=_bound_details(
                        evidence.details, self._settings.max_signal_detail_chars
                    ),
                )
                for evidence in result.evidence
            ]
            await add_signals(session, job_id, signals)
            await set_diagnosis(
                session,
                job_id=job_id,
                route=result.decision.route.value,
                confidence=result.decision.confidence,
                top2_gap=result.decision.top2_gap,
                evidence={
                    "scores": {
                        route.value: value for route, value in result.decision.scores.items()
                    },
                    "reasons": list(result.decision.reasons),
                },
            )
            await session.commit()

    async def _post(self, repo: Repository, run: Run, result: _PipelineResult) -> None:
        """Post the verdict as a Check run and, for a PR run, an evidence comment."""
        client = self._github_client(repo.installation_id)
        try:
            output = render_check_output(
                decision=result.decision,
                reproduction=result.reproduction,
                evidence=result.evidence,
            )
            client.create_check_run(
                repo.owner,
                repo.name,
                name=self._settings.triage_check_name,
                head_sha=run.head_sha,
                status="completed",
                conclusion=check_conclusion(result.decision.route),
                output=output,
            )
            if self._settings.comment_on_pr and run.pr_number is not None:
                body = render_evidence(
                    decision=result.decision,
                    reproduction=result.reproduction,
                    change=result.change,
                    evidence=result.evidence,
                    failing_targets=tuple(target.id for target in result.targets),
                )
                client.create_issue_comment(repo.owner, repo.name, run.pr_number, body)
        finally:
            client.close()

    async def _maybe_fix(self, ctx: SignalContext, decision: TriageDecision) -> None:
        """Seam for the S2 ``CODE_BUG`` fix loop (localize → retrieve → generate → prove → PR).

        S2 fills this in. For now a confident ``CODE_BUG`` reaches this seam and nothing
        happens but a log line; the fix loop is deliberately out of scope for S1.
        """
        log.info(
            "processor.fix_loop_not_implemented",
            route=decision.route.value,
            confidence=decision.confidence,
        )


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

    def github_client(installation_id: int) -> GitHubClient:
        return GitHubClient(token_provider(installation_id))

    return SandboxProcessor(
        session_factory=session_factory,
        adapter=PythonAdapter(),
        source=GitSourceProvider(token_provider=token_provider),
        runner=SandboxRunner(settings=settings.sandbox),
        settings=settings,
        github_client=github_client,
    )
