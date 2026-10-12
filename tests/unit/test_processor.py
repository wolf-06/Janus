"""The Phase 4 processor: log → reproduce → triage → persist → post.

No Docker and no network: the adapter is the canned ``FakeAdapter``, the source provider is a
context manager over a temp dir, the runner returns canned ``SandboxResult``s, the GitHub
client is a fake that records its calls, and the session factory is a fake that records the
rows it is asked to persist.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from janus.app.config import Settings
from janus.core.signals.base import SignalContext, SignalEvidence
from janus.core.triage.routes import Route
from janus.db.models import Diagnosis, Job, Repository, Run, Signal
from janus.db.models import Reproduction as ReproductionRow
from janus.jobs.processor import SandboxProcessor, build_processor
from janus.jobs.state import JobStatus
from janus.jobs.worker import stub_processor
from janus.sandbox.settings import SandboxSettings
from janus.sandbox.spec import SandboxResult, SandboxSpec
from janus.source import SourceSpec
from tests.conformance.fake_adapter import (
    FAILING_LOG,
    FAILING_REPORT,
    REPORT_PATH,
    FakeAdapter,
)

_RUN_ID = 3
_UNSET = object()
_COMPARE = {
    "commits": [
        {
            "sha": "c1",
            "commit": {"message": "break the maths"},
            "files": [{"filename": "tests/test_math.py"}],
        }
    ],
    "files": [
        {
            "filename": "tests/test_math.py",
            "patch": "@@ -1 +1 @@\n-assert 3 == 3\n+assert 3 == 4\n",
        }
    ],
}


# --- collaborators ------------------------------------------------------------------------


class _StubSource:
    """A source provider that yields a fixed path and records enter/exit."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.entered = 0
        self.exited = 0
        self.spec: SourceSpec | None = None

    @contextmanager
    def acquire(self, spec: SourceSpec) -> Iterator[Path]:
        self.entered += 1
        self.spec = spec
        try:
            yield self._path
        finally:
            self.exited += 1


class _StubRunner:
    """A runner returning factory-produced results and recording the specs it was given."""

    def __init__(self, factory: Any) -> None:
        self._factory = factory
        self.specs: list[SandboxSpec] = []
        self.source_dirs: list[Path | None] = []

    def run(self, spec: SandboxSpec, *, source_dir: Path | None = None) -> SandboxResult:
        self.specs.append(spec)
        self.source_dirs.append(source_dir)
        return self._factory(spec)


class _FakeGitHub:
    """A GitHub client that records every call and returns canned payloads."""

    def __init__(
        self,
        *,
        log: bytes = b"",
        compare: Mapping[str, Any] | None = None,
        fail_logs: bool = False,
        fail_compare: bool = False,
    ) -> None:
        self._log = log
        self._compare = compare
        self._fail_logs = fail_logs
        self._fail_compare = fail_compare
        self.downloaded: list[tuple[str, str, int]] = []
        self.check_runs: list[dict[str, Any]] = []
        self.comments: list[tuple[int, str]] = []
        self.closed = 0

    def download_run_logs(self, owner: str, repo: str, run_id: int) -> bytes:
        self.downloaded.append((owner, repo, run_id))
        if self._fail_logs:
            raise RuntimeError("log fetch exploded")
        return self._log

    def compare_commits(self, owner: str, repo: str, base: str, head: str) -> dict[str, Any]:
        if self._fail_compare or self._compare is None:
            raise RuntimeError("compare failed")
        return dict(self._compare)

    def list_commits(
        self, owner: str, repo: str, *, sha: str | None = None, per_page: int = 100
    ) -> list[dict[str, Any]]:
        return []

    def create_check_run(
        self,
        owner: str,
        repo: str,
        *,
        name: str,
        head_sha: str,
        status: str = "in_progress",
        conclusion: str | None = None,
        details_url: str | None = None,
        output: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.check_runs.append(
            {
                "owner": owner,
                "repo": repo,
                "name": name,
                "head_sha": head_sha,
                "status": status,
                "conclusion": conclusion,
                "output": output,
            }
        )
        return {}

    def create_issue_comment(
        self, owner: str, repo: str, issue_number: int, body: str
    ) -> dict[str, Any]:
        self.comments.append((issue_number, body))
        return {}

    def close(self) -> None:
        self.closed += 1


class _FakeResult:
    def all(self) -> list[Any]:
        return []


class _FakeSession:
    """A minimal async session: returns canned repo/run rows and records added rows."""

    def __init__(self, repo: object | None, run: object | None) -> None:
        self._repo = repo
        self._run = run
        self.added: list[Any] = []

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, model: type, pk: int) -> object | None:
        if model is Repository:
            return self._repo
        if model is Run:
            return self._run
        return None

    def add(self, row: object) -> None:
        self.added.append(row)

    def add_all(self, rows: Sequence[object]) -> None:
        self.added.extend(rows)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def scalar(self, statement: object) -> object | None:
        return None

    async def execute(self, statement: object) -> _FakeResult:
        return _FakeResult()


class _FakeSessionFactory:
    def __init__(self, repo: object | None, run: object | None) -> None:
        self.session = _FakeSession(repo, run)

    def __call__(self) -> _FakeSession:
        return self.session


class _FixedCollector:
    """A collector that always emits one fixed vote (for forcing a route in tests)."""

    def __init__(self, route: Route, *, vote: float = 0.9) -> None:
        self.name = "determinism"
        self._route = route
        self._vote = vote

    def collect(self, ctx: SignalContext) -> SignalEvidence | None:
        return SignalEvidence(
            name=self.name,
            summary=f"forced {self._route.value}",
            details={"forced": True},
            votes={self._route: self._vote},
        )


class _CaptureCollector:
    """A collector that abstains but records the context it was handed."""

    name = "determinism"

    def __init__(self) -> None:
        self.ctx: SignalContext | None = None

    def collect(self, ctx: SignalContext) -> SignalEvidence | None:
        self.ctx = ctx
        return None


# --- helpers ------------------------------------------------------------------------------


def _settings(*, k_reruns: int = 2, enabled: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        k_reruns=k_reruns,
        sandbox=SandboxSettings(enabled=enabled),
    )


def _job() -> Job:
    return Job(id=7, delivery_id="d1", repo_id=2, run_id=_RUN_ID)


def _failing_result(spec: SandboxSpec) -> SandboxResult:
    return SandboxResult(
        image="fake:latest",
        phase=spec.phase,
        exit_code=1,
        duration_s=1.0,
        command=spec.command,
        artifacts={REPORT_PATH: FAILING_REPORT},
    )


def _error_result(spec: SandboxSpec) -> SandboxResult:
    return SandboxResult(
        image="fake:latest",
        phase=spec.phase,
        error="daemon_unreachable",
        command=spec.command,
    )


def _is_rerun(spec: SandboxSpec) -> bool:
    return "::" in " ".join(spec.command)


def _make(
    tmp_path: Path,
    *,
    k_reruns: int = 2,
    pr_number: int | None = None,
    baseline_sha: str | None = "basesha",
    repo: object | None = _UNSET,
    run: object | None = _UNSET,
    fail_logs: bool = False,
    fail_compare: bool = False,
    runner_error: bool = False,
    collectors: Sequence[object] | None = None,
) -> SimpleNamespace:
    if repo is _UNSET:
        repo = SimpleNamespace(repo_id=2, owner="acme", name="widgets", installation_id=1)
    if run is _UNSET:
        run = SimpleNamespace(
            run_id=_RUN_ID,
            head_sha="headsha",
            baseline_sha=baseline_sha,
            pr_number=pr_number,
        )
    client = _FakeGitHub(
        log=FAILING_LOG.encode(),
        compare=_COMPARE,
        fail_logs=fail_logs,
        fail_compare=fail_compare,
    )
    factory = _FakeSessionFactory(repo, run)
    source = _StubSource(tmp_path)
    runner = _StubRunner(_error_result if runner_error else _failing_result)
    processor = SandboxProcessor(
        session_factory=factory,
        adapter=FakeAdapter(),
        source=source,
        runner=runner,
        settings=_settings(k_reruns=k_reruns),
        github_client=lambda installation_id: client,
        collectors=collectors,  # type: ignore[arg-type]
    )
    return SimpleNamespace(
        processor=processor, runner=runner, source=source, client=client, factory=factory
    )


def _rows(factory: _FakeSessionFactory, model: type) -> list[Any]:
    return [row for row in factory.session.added if isinstance(row, model)]


# --- tests --------------------------------------------------------------------------------


async def test_log_failing_tests_become_reproduction_targets(tmp_path: Path) -> None:
    harness = _make(tmp_path)

    result = await harness.processor(_job())

    assert result.status is JobStatus.SUCCEEDED
    assert harness.client.downloaded == [("acme", "widgets", _RUN_ID)]
    reruns = [spec for spec in harness.runner.specs if _is_rerun(spec)]
    assert reruns
    for spec in reruns:
        assert "tests/test_math.py::test_add" in spec.command
        assert "tests/test_math.py::test_sub" in spec.command


async def test_log_fetch_failure_runs_full_suite_only_and_succeeds(tmp_path: Path) -> None:
    harness = _make(tmp_path, fail_logs=True)

    result = await harness.processor(_job())

    assert result.status is JobStatus.SUCCEEDED
    assert harness.client.downloaded == [("acme", "widgets", _RUN_ID)]
    assert len(harness.runner.specs) == 1
    assert "tests" in harness.runner.specs[0].command
    assert not _is_rerun(harness.runner.specs[0])


async def test_janus_yml_k_reruns_overrides_setting(tmp_path: Path) -> None:
    (tmp_path / ".janus.yml").write_text("k_reruns: 2\n", encoding="utf-8")
    harness = _make(tmp_path, k_reruns=5)

    await harness.processor(_job())

    assert len([spec for spec in harness.runner.specs if _is_rerun(spec)]) == 2


async def test_settings_k_reruns_used_without_janus_yml(tmp_path: Path) -> None:
    harness = _make(tmp_path, k_reruns=3)

    await harness.processor(_job())

    assert len([spec for spec in harness.runner.specs if _is_rerun(spec)]) == 3


async def test_reproductions_signals_and_diagnosis_are_persisted(tmp_path: Path) -> None:
    harness = _make(tmp_path, collectors=[_FixedCollector(Route.CODE_BUG)])

    result = await harness.processor(_job())

    assert result.status is JobStatus.SUCCEEDED

    reproductions = _rows(harness.factory, ReproductionRow)
    assert len(reproductions) == 5  # 2 targets x k=2 reruns, plus the full suite
    assert sum(1 for row in reproductions if row.target_id is None) == 1
    assert all(row.profile == "clean" for row in reproductions)
    assert all(row.passed is False for row in reproductions)

    signals = _rows(harness.factory, Signal)
    assert len(signals) == 1
    assert signals[0].name == "determinism"
    assert signals[0].summary == "forced CODE_BUG"
    assert signals[0].details == {"forced": True}

    diagnoses = _rows(harness.factory, Diagnosis)
    assert len(diagnoses) == 1
    diagnosis = diagnoses[0]
    assert diagnosis.route == "CODE_BUG"
    assert diagnosis.confidence == 0.9
    assert diagnosis.top2_gap == 0.9
    assert set(diagnosis.evidence["scores"]) == {"CODE_BUG", "FLAKY", "ENV", "UNSURE"}
    assert diagnosis.evidence["reasons"]


async def test_check_run_posted_with_code_bug_conclusion_and_pr_comment(
    tmp_path: Path,
) -> None:
    harness = _make(tmp_path, pr_number=42, collectors=[_FixedCollector(Route.CODE_BUG)])

    result = await harness.processor(_job())

    assert result.status is JobStatus.SUCCEEDED
    assert len(harness.client.check_runs) == 1
    check = harness.client.check_runs[0]
    assert check["name"] == "Janus triage"
    assert check["head_sha"] == "headsha"
    assert check["status"] == "completed"
    assert check["conclusion"] == "action_required"
    assert check["output"]["title"] == "Janus triage: CODE_BUG"
    assert len(harness.client.comments) == 1
    assert harness.client.comments[0][0] == 42
    assert "CODE_BUG" in harness.client.comments[0][1]


async def test_check_run_neutral_and_no_comment_without_pr_number(tmp_path: Path) -> None:
    harness = _make(tmp_path, pr_number=None, collectors=[_FixedCollector(Route.FLAKY)])

    result = await harness.processor(_job())

    assert result.status is JobStatus.SUCCEEDED
    assert len(harness.client.check_runs) == 1
    assert harness.client.check_runs[0]["conclusion"] == "neutral"
    assert harness.client.comments == []


async def test_code_bug_invokes_fix_seam_and_flaky_does_not(tmp_path: Path) -> None:
    harness = _make(tmp_path, collectors=[_FixedCollector(Route.CODE_BUG)])
    fixed: list[Route] = []

    async def record(ctx: SignalContext, decision: object) -> None:
        fixed.append(decision.route)  # type: ignore[attr-defined]

    harness.processor._maybe_fix = record  # type: ignore[method-assign]
    await harness.processor(_job())
    assert fixed == [Route.CODE_BUG]

    flaky = _make(tmp_path, collectors=[_FixedCollector(Route.FLAKY)])
    flaky_fixed: list[Route] = []

    async def record_flaky(ctx: SignalContext, decision: object) -> None:
        flaky_fixed.append(decision.route)  # type: ignore[attr-defined]

    flaky.processor._maybe_fix = record_flaky  # type: ignore[method-assign]
    await flaky.processor(_job())
    assert flaky_fixed == []


async def test_absent_baseline_yields_unavailable_change_and_succeeds(
    tmp_path: Path,
) -> None:
    capture = _CaptureCollector()
    harness = _make(tmp_path, baseline_sha=None, collectors=[capture])

    result = await harness.processor(_job())

    assert result.status is JobStatus.SUCCEEDED
    assert capture.ctx is not None
    assert capture.ctx.change.available is False
    assert capture.ctx.change.base_sha is None


async def test_failing_change_resolution_yields_unavailable_change_and_succeeds(
    tmp_path: Path,
) -> None:
    capture = _CaptureCollector()
    harness = _make(tmp_path, fail_compare=True, collectors=[capture])

    result = await harness.processor(_job())

    assert result.status is JobStatus.SUCCEEDED
    assert capture.ctx is not None
    assert capture.ctx.change.available is False


async def test_sandbox_infrastructure_failure_is_retryable(tmp_path: Path) -> None:
    harness = _make(tmp_path, runner_error=True)

    result = await harness.processor(_job())

    assert result.status is JobStatus.FAILED
    assert result.retryable is True
    assert "daemon_unreachable" in (result.detail or "")
    assert _rows(harness.factory, ReproductionRow) == []


async def test_missing_repo_or_run_is_non_retryable(tmp_path: Path) -> None:
    harness = _make(tmp_path, repo=None, run=None)

    result = await harness.processor(_job())

    assert result.status is JobStatus.FAILED
    assert result.retryable is False
    assert result.detail == "repo or run missing"
    assert harness.client.downloaded == []
    assert harness.runner.specs == []


def test_build_processor_returns_stub_when_sandbox_disabled() -> None:
    settings = _settings(enabled=False)
    processor = build_processor(settings, _FakeSessionFactory(None, None))
    assert processor is stub_processor
