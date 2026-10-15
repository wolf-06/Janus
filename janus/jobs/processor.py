"""The real job processor: reproduce, triage, persist and post a verdict.

Replaces ``stub_processor`` for claimed jobs. For one job it resolves the repository and
run, fetches the failing run's CI log and parses the failing tests, checks out the exact
revision, reruns the failing targets K times plus the whole suite once (the reproducer),
resolves the last-green → first-red change, collects the seven triage signals, scores them
into a route, persists the evidence and the diagnosis, and posts the verdict as a Check run
(and, when the run belongs to a pull request, an evidence comment).

Scope: this is **S2**. A ``CODE_BUG`` decision runs the fix loop — per-test coverage, SBFL
localization, ``git log`` retrieval, LLM diff generation, the static policy gate and the four
measured proof gates — inside the same source checkout as the reproduction. A proven fix in
``pr`` mode is committed to a normal branch and opened as a draft PR; every other outcome
posts the suggest surface. A failing test suite is the *expected* input, so it is a
successful job that gathered evidence; only an infrastructure failure (no daemon, image build
failure, timeout, or no test-phase result) is a retryable failure.

The whole blocking sequence — acquire the source, run the reproducer, resolve the change,
collect the signals, score, and (for ``CODE_BUG``) run the fix loop — happens inside one
``asyncio.to_thread`` call, and the source context manager is entered and exited inside that
thread, so a checkout can never leak. The
two steps that cannot run in that thread are the async database reads: the flake history is
read first, in a short-lived session, and the log fetch that yields the failing targets is
done just before the thread so those targets are known when the history is read. An
unexpected error anywhere is turned into a retryable failure; the pipeline never raises.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from janus.app.config import Settings
from janus.config.janus_yml import RepoConfig, RepoConfigError, load_repo_config
from janus.core.adapter import Adapter, Command, MutationResult, TestTarget
from janus.core.evidence import ChangeSummary, CommitSummary, Reproduction
from janus.core.generate.llm import LLMClient, build_llm
from janus.core.generate.prompt import MAX_PROMPT_CHARS, build_fix_prompt, extract_diff
from janus.core.localize.sbfl import SuspectLine, localize
from janus.core.plan import build_plan, phase_result
from janus.core.prove.gates import (
    GATE_MUTATION,
    GateResult,
    all_passed,
    coverage_percent,
    evaluate,
    test_file_hashes,
)
from janus.core.prove.policy import PolicyViolation, check_policy, diff_files
from janus.core.reproducer import Reproducer, ReproduceRequest
from janus.core.retrieve.git_grep import (
    PriorFix,
    build_terms,
    git_log_argv,
    parse_git_log,
    rank,
)
from janus.core.signals import collect_all, default_collectors
from janus.core.signals.base import Collector, SignalContext, SignalEvidence
from janus.core.triage.routes import Route, TriageDecision
from janus.core.triage.scorer import score
from janus.db.models import GateResult as GateRow
from janus.db.models import Job, Repository, Run, Signal, Suspect
from janus.db.models import Reproduction as ReproductionRow
from janus.db.repository import (
    add_fix,
    add_gate_results,
    add_reproductions,
    add_signals,
    add_suspects,
    flake_history,
    set_diagnosis,
)
from janus.github.client import GitHubClient
from janus.github.logs import extract_log_text
from janus.github.push import commit_and_push
from janus.jobs.state import JobStatus
from janus.jobs.worker import JobProcessor, JobResult, stub_processor
from janus.report.comment import check_conclusion, render_check_output, render_evidence
from janus.report.pr import branch_name, render_pr_body, render_pr_title, render_suggest_comment
from janus.sandbox.profiles import Profile, RunPhase, RunPlan
from janus.sandbox.runner import SandboxRunner
from janus.sandbox.spec import SandboxPhase, SandboxResult
from janus.source import SourceProvider, SourceSpec

log = structlog.get_logger(__name__)

# ``Callable[[], AsyncSession]`` matches ``Worker``'s ``session_factory`` and
# ``janus.db.session.AsyncSessionLocal``.
SessionFactory = Callable[[], AsyncSession]

# ``installation_id -> GitHubClient``. A callable so the GitHub App auth module can mint a
# token lazily (and so tests can inject a fake client).
GitHubClientFactory = Callable[[int], GitHubClient]

# ``installation_id -> installation token``. Only the push path needs it.
TokenProvider = Callable[[int], str]

_MAX_DETAIL_CHARS = 200

# Bounds for the S2 fix loop. Source files are read in full up to this many characters so a
# generated file cannot blow up the prompt; the prompt itself clips further.
_MAX_SOURCE_FILE_CHARS = 8000
_MAX_GIT_OUTPUT_CHARS = 200_000
_GIT_TIMEOUT_SECONDS = 30.0
_GIT_APPLY_TIMEOUT_SECONDS = 60.0
_MAX_PRIOR_FIXES = 5


def _missing_token(installation_id: int) -> str:
    """The default token provider: fail loudly rather than silently pushing unauthenticated."""
    raise RuntimeError(f"no installation token provider configured for {installation_id}")


def _git_env() -> dict[str, str]:
    """A minimal git environment with no token and no user/system config."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": tempfile.gettempdir(),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "LC_ALL": "C",
    }


@dataclass(frozen=True)
class FixOutcome:
    """The result of one ``CODE_BUG`` fix attempt run inside the source checkout.

    ``status`` is one of ``proven``, ``no_localization``, ``no_llm``, ``no_diff``,
    ``rejected`` (the static policy gate refused every diff), ``gates_failed`` (a diff
    applied but the measured proof did not pass), or ``error``. ``patched_checkout`` is the
    temporary, diff-applied copy retained only for a proven fix so the async half can push
    it; every other outcome cleans its own temporary directory before returning.
    """

    status: str
    suspects: tuple[SuspectLine, ...] = ()
    diff: str | None = None
    gates: tuple[GateResult, ...] = ()
    violations: tuple[PolicyViolation, ...] = ()
    prior_fixes: tuple[PriorFix, ...] = ()
    attempts: int = 0
    detail: str = ""
    patched_checkout: Path | None = None


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
    fix: FixOutcome | None = None

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
    if result.fix is not None:
        detail += f"; fix={result.fix.status}"
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
        token_provider: TokenProvider | None = None,
        llm: LLMClient | None = None,
        reproducer: Reproducer | None = None,
        collectors: Sequence[Collector] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._adapter = adapter
        self._source = source
        self._runner = runner
        self._settings = settings
        self._github_client = github_client
        self._token_provider: TokenProvider = (
            token_provider if token_provider is not None else _missing_token
        )
        # ``None`` means "build from settings"; tests inject a scripted client.
        self._llm = llm
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
        try:
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
                await self._post(job.id, repo, run, result)
            except Exception as exc:  # noqa: BLE001 - posting is best-effort
                log.warning("processor.post_failed", job_id=job.id, error=str(exc))

            log.info(
                "processor.run_complete",
                job_id=job.id,
                repo_id=job.repo_id,
                run_id=job.run_id,
                route=result.decision.route.value,
                confidence=result.decision.confidence,
                attempts=len(result.reproduction.attempts),
                signals=len(result.evidence),
                fix=result.fix.status if result.fix is not None else None,
            )
            return JobResult(
                status=JobStatus.SUCCEEDED,
                cost_minutes=cost_minutes,
                detail=_success_detail(result),
            )
        finally:
            self._cleanup_fix_workdir(result)

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
                # The fix loop needs the checkout, so it runs in the same thread and the same
                # ``with`` block as the reproduction. Its outcome travels back on the result;
                # the source context is still exited exactly once.
                fix = (
                    self._run_fix(
                        checkout=checkout,
                        targets=targets,
                        reproduction=reproduction,
                        decision=decision,
                        repo_config=repo_config,
                    )
                    if decision.route is Route.CODE_BUG
                    else None
                )
            return _PipelineResult(
                reproduction=reproduction,
                change=change,
                targets=targets,
                ctx=ctx,
                evidence=evidence,
                decision=decision,
                repo_config=repo_config,
                fix=fix,
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
        """Write the reproductions, signals, diagnosis and fix evidence in one transaction."""
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
            if result.fix is not None:
                await self._persist_fix(session, job_id, result.fix)
            await session.commit()

    async def _persist_fix(self, session: AsyncSession, job_id: int, fix: FixOutcome) -> None:
        """Persist the suspects, the final generated diff and its measured gates.

        A diff is recorded even when it was rejected (policy) or its gates failed, because the
        evidence of *what was tried* is part of the audit trail. No diff means no ``fixes``
        row; suspects are recorded whenever localization produced any.
        """
        suspect_rows = [
            Suspect(
                job_id=job_id,
                file=suspect.file,
                line=suspect.line,
                score=suspect.score,
                rank=suspect.rank,
            )
            for suspect in fix.suspects
        ]
        if suspect_rows:
            await add_suspects(session, job_id, suspect_rows)

        if not fix.diff:
            return

        suspect_id = getattr(suspect_rows[0], "id", None) if suspect_rows else None
        fix_row = await add_fix(
            session,
            job_id=job_id,
            suspect_id=suspect_id,
            attempt=max(fix.attempts, 0),
            diff=fix.diff,
            status=fix.status,
        )
        gate_rows = [
            GateRow(
                gate=gate.gate,
                passed=gate.passed,
                measured_value=gate.measured_value,
                baseline_value=gate.baseline_value,
            )
            for gate in fix.gates
        ]
        if gate_rows:
            await add_gate_results(session, fix_row.id, gate_rows)

    async def _post(self, job_id: int, repo: Repository, run: Run, result: _PipelineResult) -> None:
        """Post the verdict as a Check run, the evidence comment, and the fix surface."""
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
            await self._post_fix(client, job_id, repo, run, result)
        finally:
            client.close()

    def _effective_mode(self, repo_config: RepoConfig) -> str:
        """The repo's ``mode`` when it set one, otherwise the process-wide ``fix_mode``."""
        raw_mode = repo_config.raw.get("mode")
        if raw_mode in ("suggest", "pr"):
            return str(raw_mode)
        return self._settings.fix_mode

    async def _post_fix(
        self,
        client: GitHubClient,
        job_id: int,
        repo: Repository,
        run: Run,
        result: _PipelineResult,
    ) -> None:
        """Open a draft PR for a proven fix in ``pr`` mode, otherwise post the suggestion.

        ``no_localization`` has no fix surface to post: there is no suspect and no diff, and
        the triage evidence already carries the diagnosis. Posting failures are logged and
        degrade to the comment surface; they never fail the job.
        """
        fix = result.fix
        if fix is None or fix.status == "no_localization":
            return

        mode = self._effective_mode(result.repo_config)
        if (
            fix.status == "proven"
            and mode == "pr"
            and fix.diff is not None
            and fix.patched_checkout is not None
        ):
            try:
                await asyncio.to_thread(self._open_pr, client, job_id, repo, result, fix)
                return
            except Exception as exc:  # noqa: BLE001 - push/PR failures fall back to a comment
                log.warning("processor.fix_pr_failed", job_id=job_id, error=str(exc))

        self._post_suggest(client, repo, run, result, fix)

    def _open_pr(
        self,
        client: GitHubClient,
        job_id: int,
        repo: Repository,
        result: _PipelineResult,
        fix: FixOutcome,
    ) -> None:
        """Commit the proven patch to ``janus/fix-<job>`` and open a draft PR.

        This is deliberately a normal branch push: there is no force-push and no merge path.
        The installation token is minted here and handed only to ``commit_and_push``.
        """
        assert fix.diff is not None
        assert fix.patched_checkout is not None
        token = self._token_provider(repo.installation_id)
        branch = branch_name(job_id)
        title = render_pr_title(decision=result.decision, suspects=fix.suspects)
        commit_and_push(
            checkout=fix.patched_checkout,
            branch=branch,
            message=title,
            token=token,
        )
        pull_request = client.create_pull_request(
            repo.owner,
            repo.name,
            title=title,
            head=branch,
            base=getattr(repo, "default_branch", "main"),
            body=render_pr_body(
                decision=result.decision,
                reproduction=result.reproduction,
                change=result.change,
                suspects=fix.suspects,
                diff=fix.diff,
                gates=fix.gates,
                violations=fix.violations,
            ),
            draft=True,
        )
        url = pull_request.get("html_url") if isinstance(pull_request, Mapping) else None
        log.info("processor.fix_pr_opened", job_id=job_id, branch=branch, url=url)

    def _post_suggest(
        self,
        client: GitHubClient,
        repo: Repository,
        run: Run,
        result: _PipelineResult,
        fix: FixOutcome,
    ) -> None:
        """Post the suggest surface as a PR comment, or as a Check run without a PR."""
        body = render_suggest_comment(
            decision=result.decision,
            suspects=fix.suspects,
            diff=fix.diff,
            gates=fix.gates,
            violations=fix.violations,
        )
        if run.pr_number is not None:
            client.create_issue_comment(repo.owner, repo.name, run.pr_number, body)
            return
        client.create_check_run(
            repo.owner,
            repo.name,
            name=f"{self._settings.triage_check_name}: fix",
            head_sha=run.head_sha,
            status="completed",
            conclusion="neutral",
            output={"title": "Janus fix proposal", "summary": body},
        )

    def _cleanup_fix_workdir(self, result: _PipelineResult) -> None:
        """Remove the retained patched copy of a proven fix (the source checkout is gone)."""
        fix = result.fix
        if fix is None or fix.patched_checkout is None:
            return
        shutil.rmtree(fix.patched_checkout.parent, ignore_errors=True)

    # --- S2 fix loop (blocking, inside the checkout's source context) ---------------------

    def _run_fix(
        self,
        *,
        checkout: Path,
        targets: tuple[TestTarget, ...],
        reproduction: Reproduction,
        decision: TriageDecision,
        repo_config: RepoConfig,
    ) -> FixOutcome:
        """Localize, retrieve, generate and prove a fix for one ``CODE_BUG``.

        Blocking, and called inside the source context so the checkout is still available.
        It never raises: an unexpected failure is an ``error`` outcome.
        """
        del repo_config  # the effective mode is applied by the async posting half
        try:
            coverage_result = self._run_adapter_command(
                checkout, self._adapter.coverage_command(targets)
            )
            contexts = (
                self._adapter.parse_coverage(coverage_result) if coverage_result is not None else []
            )
            if not contexts:
                return FixOutcome(status="no_localization")

            failing_ids = self._failing_test_ids(targets, reproduction)
            suspects = tuple(
                localize(
                    contexts,
                    failing_ids,
                    max_suspects=self._settings.max_suspects,
                )
            )
            if not suspects:
                return FixOutcome(status="no_localization")

            prior_fixes = self._retrieve(checkout, failing_ids, suspects, reproduction)
            sources = self._read_sources(checkout, suspects)
            llm = self._llm if self._llm is not None else build_llm(self._settings)
            if not llm.available:
                return FixOutcome(
                    status="no_llm",
                    suspects=suspects,
                    prior_fixes=prior_fixes,
                )
            return self._generate_and_prove(
                checkout=checkout,
                decision=decision,
                reproduction=reproduction,
                suspects=suspects,
                sources=sources,
                prior_fixes=prior_fixes,
                llm=llm,
            )
        except Exception as exc:  # noqa: BLE001 - the fix loop never raises
            log.warning("processor.fix_failed", error=str(exc))
            return FixOutcome(status="error", detail=str(exc))

    def _run_adapter_command(self, checkout: Path, command: Command) -> SandboxResult | None:
        """Run one adapter command with the shared setup/test phase rules.

        The setup phases come from :func:`janus.core.plan.build_plan`; the requested command
        replaces the test phase. Returns the test-phase result, or ``None`` when the sandbox
        is disabled or produced no test phase.
        """
        if not self._settings.sandbox.enabled:
            return None
        plan = build_plan(self._adapter, self._settings.sandbox, targets=())
        phases = [phase for phase in plan.phases if phase.phase is SandboxPhase.SETUP]
        phases.append(
            RunPhase(
                phase=SandboxPhase.TEST,
                command=command.argv,
                network=False,
                artifacts=command.artifacts,
            )
        )
        run_plan = RunPlan(profile=Profile.CLEAN, phases=tuple(phases))
        results: list[SandboxResult] = []
        for spec in run_plan.specs(self._settings.sandbox):
            try:
                results.append(self._runner.run(spec, source_dir=checkout))
            except Exception as exc:  # noqa: BLE001 - a bad run degrades to an error result
                results.append(
                    SandboxResult(
                        image=spec.image,
                        phase=spec.phase,
                        error=f"runner_error: {exc}",
                        command=spec.command,
                    )
                )
        return phase_result(results, SandboxPhase.TEST)

    def _failing_test_ids(
        self, targets: Sequence[TestTarget], reproduction: Reproduction
    ) -> tuple[str, ...]:
        """Every failing test id, from the CI targets and the rerun attempts."""
        ids = [target.id for target in targets]
        ids.extend(
            attempt.target_id for attempt in reproduction.attempts if attempt.target_id is not None
        )
        return tuple(dict.fromkeys(ids))

    def _retrieve(
        self,
        checkout: Path,
        failing_ids: Sequence[str],
        suspects: Sequence[SuspectLine],
        reproduction: Reproduction,
    ) -> tuple[PriorFix, ...]:
        """Grep ``git log -p`` for similar past commits, best match first (top five)."""
        error_lines: list[str] = []
        attempts = list(reproduction.attempts)
        if reproduction.full_suite is not None:
            attempts.append(reproduction.full_suite)
        for attempt in attempts:
            excerpt = attempt.stderr_excerpt.strip()
            if excerpt:
                error_lines.extend(excerpt.splitlines()[:2])

        terms = build_terms(
            failing_tests=failing_ids,
            suspect_files=[suspect.file for suspect in suspects],
            error_lines=error_lines,
        )
        argv = git_log_argv(terms)
        text = self._run_git(checkout, argv)
        if not text:
            return ()
        return tuple(rank(parse_git_log(text), terms)[:_MAX_PRIOR_FIXES])

    def _run_git(self, checkout: Path, argv: Sequence[str]) -> str:
        """Run a shell-free, token-free ``git`` command with bounded output."""
        if not argv:
            return ""
        try:
            completed = subprocess.run(
                list(argv),
                cwd=checkout,
                env=_git_env(),
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("processor.retrieve_failed", error=str(exc))
            return ""
        if completed.returncode != 0:
            return ""
        return completed.stdout[:_MAX_GIT_OUTPUT_CHARS]

    def _read_sources(self, checkout: Path, suspects: Sequence[SuspectLine]) -> dict[str, str]:
        """Read each suspect file, bounded, rejecting any path that escapes the checkout."""
        root = checkout.resolve()
        sources: dict[str, str] = {}
        seen: set[Path] = set()
        for suspect in suspects:
            path = self._safe_source_path(root, suspect.file)
            if path is None or path in seen:
                continue
            seen.add(path)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            key = path.relative_to(root).as_posix()
            sources[key] = text[:_MAX_SOURCE_FILE_CHARS]
        return sources

    def _safe_source_path(self, root: Path, file: str) -> Path | None:
        """Resolve ``file`` inside ``root``; ``None`` for an escape or a non-file."""
        candidate = Path(file)
        resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            return None
        return resolved

    def _generate_and_prove(
        self,
        *,
        checkout: Path,
        decision: TriageDecision,
        reproduction: Reproduction,
        suspects: tuple[SuspectLine, ...],
        sources: Mapping[str, str],
        prior_fixes: tuple[PriorFix, ...],
        llm: LLMClient,
    ) -> FixOutcome:
        """Ask the model for a diff and prove it, retrying with feedback."""
        attempts = 0
        feedback: list[str] = []
        last_diff: str | None = None
        last_violations: tuple[PolicyViolation, ...] = ()
        best_diff: str | None = None
        best_gates: tuple[GateResult, ...] = ()
        best_violations: tuple[PolicyViolation, ...] = ()
        best_score = -1

        for index in range(max(0, self._settings.max_fix_attempts)):
            attempts = index + 1
            prompt = build_fix_prompt(
                decision=decision,
                reproduction=reproduction,
                suspects=suspects,
                sources=sources,
                prior_fixes=prior_fixes,
            )
            if feedback:
                prompt = _append_feedback(prompt, "\n".join(feedback[-3:]))

            try:
                reply = llm.complete(prompt, max_tokens=self._settings.llm_max_tokens)
            except Exception as exc:  # noqa: BLE001 - a failed call is verifier feedback
                feedback.append(f"The model call failed ({exc}). Reply with a unified diff only.")
                continue

            diff = extract_diff(reply)
            if diff is None:
                feedback.append("The reply contained no unified diff. Reply with a diff only.")
                continue
            last_diff = diff

            test_files = tuple(test_file_hashes(checkout))
            violations = tuple(check_policy(diff, test_files=test_files))
            if violations:
                last_violations = violations
                feedback.append(
                    "The diff violated the policy gate: "
                    + "; ".join(f"{item.rule}: {item.detail}" for item in violations)
                )
                continue

            patched_root = Path(tempfile.mkdtemp(prefix="janus-fix-"))
            patched = patched_root / "repo"
            keep = False
            try:
                shutil.copytree(checkout, patched, symlinks=True)
                apply_error = self._apply_diff(patched, diff)
                if apply_error is not None:
                    feedback.append(f"The diff did not apply: {apply_error}")
                    continue

                gates = tuple(self._measure_gates(checkout, patched, diff))
                score = sum(1 for gate in gates if gate.passed)
                if score > best_score:
                    best_score = score
                    best_diff = diff
                    best_gates = gates
                    best_violations = ()

                if all_passed(gates):
                    keep = True
                    return FixOutcome(
                        status="proven",
                        suspects=suspects,
                        diff=diff,
                        gates=gates,
                        prior_fixes=prior_fixes,
                        attempts=attempts,
                        patched_checkout=patched,
                    )

                failed = ", ".join(gate.gate for gate in gates if not gate.passed)
                feedback.append(
                    f"The proof gates failed ({failed}). Change only source so all pass."
                )
            finally:
                if not keep:
                    shutil.rmtree(patched_root, ignore_errors=True)

        if best_gates:
            return FixOutcome(
                status="gates_failed",
                suspects=suspects,
                diff=best_diff,
                gates=best_gates,
                violations=best_violations,
                prior_fixes=prior_fixes,
                attempts=attempts,
                detail="proof gates did not all pass",
            )
        if last_violations:
            return FixOutcome(
                status="rejected",
                suspects=suspects,
                diff=last_diff,
                violations=last_violations,
                prior_fixes=prior_fixes,
                attempts=attempts,
                detail="static policy gate rejected every diff",
            )
        if last_diff is not None:
            return FixOutcome(
                status="error",
                suspects=suspects,
                diff=last_diff,
                prior_fixes=prior_fixes,
                attempts=attempts,
                detail="no generated diff could be applied",
            )
        return FixOutcome(
            status="no_diff",
            suspects=suspects,
            prior_fixes=prior_fixes,
            attempts=attempts,
        )

    def _measure_gates(self, checkout: Path, patched: Path, diff: str) -> list[GateResult]:
        """Run the four measured gates: suite, mutation, coverage, test-file hashes."""
        hashes_before = test_file_hashes(checkout)
        hashes_after = test_file_hashes(patched)

        suite_result = self._run_adapter_command(patched, self._adapter.test_command(()))
        suite_passed = suite_result is not None and suite_result.ok

        coverage_before = self._coverage_percent(checkout)
        coverage_after = self._coverage_percent(patched)

        if self._settings.mutation_gate_enabled:
            changed_files = diff_files(diff)
            mutation_before = self._mutation_result(checkout, changed_files)
            mutation_after = self._mutation_result(patched, changed_files)
        else:
            mutation_before = None
            mutation_after = None

        results = evaluate(
            suite_passed=suite_passed,
            mutation_before=mutation_before,
            mutation_after=mutation_after,
            coverage_before=coverage_before,
            coverage_after=coverage_after,
            test_hashes_before=hashes_before,
            test_hashes_after=hashes_after,
        )
        if self._settings.mutation_gate_enabled:
            return results
        return [
            replace(
                gate,
                passed=True,
                measured_value="disabled",
                baseline_value=None,
            )
            if gate.gate == GATE_MUTATION
            else gate
            for gate in results
        ]

    def _coverage_percent(self, checkout: Path) -> float | None:
        """Run the full-suite coverage command and return its covered-pair count."""
        result = self._run_adapter_command(checkout, self._adapter.coverage_command(()))
        if result is None:
            return None
        return coverage_percent(self._adapter.parse_coverage(result))

    def _mutation_result(self, checkout: Path, files: Sequence[str]) -> MutationResult | None:
        """Run mutation testing for ``files``; missing measurements fail closed (``None``)."""
        command = self._adapter.mutation_command(files)
        result = self._run_adapter_command(checkout, command)
        if result is None or result.error is not None:
            return None
        if any(path not in result.artifacts for path in command.artifacts):
            return None
        return self._adapter.parse_mutation(result)

    def _apply_diff(self, patched: Path, diff: str) -> str | None:
        """Apply ``diff`` in ``patched`` with ``git apply``; return an error or ``None``."""
        payload = diff if diff.endswith("\n") else diff + "\n"
        for argv in (("git", "apply", "--check", "-"), ("git", "apply", "-")):
            try:
                completed = subprocess.run(
                    list(argv),
                    cwd=patched,
                    env=_git_env(),
                    input=payload,
                    capture_output=True,
                    text=True,
                    timeout=_GIT_APPLY_TIMEOUT_SECONDS,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return str(exc)
            if completed.returncode != 0:
                message = (completed.stderr or completed.stdout or "git apply failed").strip()
                return message[:500]
        return None


def _append_feedback(prompt: str, feedback: str) -> str:
    """Append verifier feedback without letting it push the prompt past its hard cap."""
    header = "\n\nFEEDBACK FROM THE PREVIOUS ATTEMPT\n"
    room = MAX_PROMPT_CHARS - len(prompt) - len(header)
    if room <= 0:
        return prompt
    return prompt + header + feedback[:room]


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
        token_provider=token_provider,
    )
