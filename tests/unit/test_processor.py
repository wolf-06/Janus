"""The Phase 4 processor: log → reproduce → triage → persist → post.

No Docker and no network: the adapter is the canned ``FakeAdapter``, the source provider is a
context manager over a temp dir, the runner returns canned ``SandboxResult``s, the GitHub
client is a fake that records its calls, and the session factory is a fake that records the
rows it is asked to persist.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import janus.jobs.processor as processor_module
from janus.app.config import Settings
from janus.core.generate.llm import NullLLM
from janus.core.signals.base import SignalContext, SignalEvidence
from janus.core.triage.routes import Route
from janus.db.models import Diagnosis, Fix, GateResult, Job, Repository, Run, Signal, Suspect
from janus.db.models import Reproduction as ReproductionRow
from janus.jobs.processor import SandboxProcessor, build_processor
from janus.jobs.state import JobStatus
from janus.jobs.worker import stub_processor
from janus.sandbox.settings import SandboxSettings
from janus.sandbox.spec import SandboxResult, SandboxSpec
from janus.source import SourceSpec
from tests.conformance.fake_adapter import (
    COVERAGE_RESULT,
    FAILING_LOG,
    FAILING_REPORT,
    MUTATION_RESULT,
    PASSING_RESULT,
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
        self.pull_requests: list[dict[str, Any]] = []
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

    def create_pull_request(
        self,
        owner: str,
        repo: str,
        *,
        title: str,
        head: str,
        base: str,
        body: str,
        draft: bool = True,
    ) -> dict[str, Any]:
        self.pull_requests.append(
            {
                "owner": owner,
                "repo": repo,
                "title": title,
                "head": head,
                "base": base,
                "body": body,
                "draft": draft,
            }
        )
        return {"number": 1, "html_url": f"https://example.test/{head}"}

    def close(self) -> None:
        self.closed += 1


class _SmartRunner:
    """A runner whose result depends on the command and the checkout it was handed."""

    def __init__(self, factory: Callable[[SandboxSpec, Path | None], SandboxResult]) -> None:
        self._factory = factory
        self.specs: list[SandboxSpec] = []
        self.source_dirs: list[Path | None] = []

    def run(self, spec: SandboxSpec, *, source_dir: Path | None = None) -> SandboxResult:
        self.specs.append(spec)
        self.source_dirs.append(source_dir)
        return self._factory(spec, source_dir)


class _ScriptedLLM:
    """An available LLM that returns canned replies in order and records its prompts."""

    available = True

    def __init__(self, replies: Sequence[str]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, max_tokens: int, temperature: float = 0.0) -> str:
        del max_tokens, temperature
        self.prompts.append(prompt)
        return self._replies.pop(0) if self._replies else ""


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


def _settings(
    *,
    k_reruns: int = 2,
    enabled: bool = True,
    fix_mode: str = "suggest",
    mutation_gate_enabled: bool = True,
    max_fix_attempts: int = 3,
) -> Settings:
    return Settings(
        _env_file=None,
        k_reruns=k_reruns,
        fix_mode=fix_mode,
        mutation_gate_enabled=mutation_gate_enabled,
        max_fix_attempts=max_fix_attempts,
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
    settings: Settings | None = None,
    llm: object | None = None,
    runner: object | None = None,
    token_provider: Callable[[int], str] | None = None,
) -> SimpleNamespace:
    if repo is _UNSET:
        repo = SimpleNamespace(
            repo_id=2,
            owner="acme",
            name="widgets",
            installation_id=1,
            default_branch="main",
        )
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
    actual_runner = (
        runner
        if runner is not None
        else _StubRunner(_error_result if runner_error else _failing_result)
    )
    processor = SandboxProcessor(
        session_factory=factory,
        adapter=FakeAdapter(),
        source=source,
        runner=actual_runner,  # type: ignore[arg-type]
        settings=settings if settings is not None else _settings(k_reruns=k_reruns),
        github_client=lambda installation_id: client,
        token_provider=token_provider,
        llm=llm,  # type: ignore[arg-type]
        collectors=collectors,  # type: ignore[arg-type]
    )
    return SimpleNamespace(
        processor=processor,
        runner=actual_runner,
        source=source,
        client=client,
        factory=factory,
    )


def _capture_fix(harness: SimpleNamespace) -> list[Any]:
    """Wrap ``_run_fix`` so a test can inspect the exact ``FixOutcome`` it returned."""
    outcomes: list[Any] = []
    original = harness.processor._run_fix

    def capture(**kwargs: Any) -> Any:
        outcome = original(**kwargs)
        outcomes.append(outcome)
        return outcome

    harness.processor._run_fix = capture
    return outcomes


def _fix_runner(checkout: Path, *, suite_passes: bool = True) -> _SmartRunner:
    """A runner that answers coverage/mutation runs and makes the gate suite green or red."""

    def factory(spec: SandboxSpec, source_dir: Path | None) -> SandboxResult:
        command = " ".join(spec.command)
        if "--cov" in command:
            return COVERAGE_RESULT
        if "mutmut" in command:
            return MUTATION_RESULT
        if source_dir == checkout:
            return _failing_result(spec)
        if suite_passes:
            return PASSING_RESULT
        return _failing_result(spec)

    return _SmartRunner(factory)


def _clean_diff() -> str:
    return (
        "diff --git a/src/math.py b/src/math.py\n"
        "--- a/src/math.py\n"
        "+++ b/src/math.py\n"
        "@@ -1 +1 @@\n"
        "-value = 3\n"
        "+value = 4\n"
    )


def _test_edit_diff() -> str:
    return (
        "diff --git a/tests/test_math.py b/tests/test_math.py\n"
        "--- a/tests/test_math.py\n"
        "+++ b/tests/test_math.py\n"
        "@@ -1 +1 @@\n"
        "-assert 1 == 1\n"
        "+assert 1 == 2\n"
    )


def _seed_checkout(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "math.py").write_text("value = 3\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_math.py").write_text(
        "def test_add():\n    assert 1 == 1\n", encoding="utf-8"
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
    fixed = _capture_fix(harness)
    await harness.processor(_job())
    assert [outcome.status for outcome in fixed] == ["no_localization"]

    flaky = _make(tmp_path, collectors=[_FixedCollector(Route.FLAKY)])
    flaky_fixed = _capture_fix(flaky)
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


# --- S2 fix loop --------------------------------------------------------------------------


def _ci_weakening_diff() -> str:
    return (
        "diff --git a/src/math.py b/src/math.py\n"
        "--- a/src/math.py\n"
        "+++ b/src/math.py\n"
        "@@ -1 +1,2 @@\n"
        " value = 3\n"
        "+run || true\n"
    )


async def test_code_bug_with_empty_coverage_has_no_localization(tmp_path: Path) -> None:
    harness = _make(tmp_path, collectors=[_FixedCollector(Route.CODE_BUG)])

    result = await harness.processor(_job())

    assert result.status is JobStatus.SUCCEEDED
    assert "fix=no_localization" in (result.detail or "")
    assert _rows(harness.factory, Fix) == []
    assert harness.client.pull_requests == []


async def test_code_bug_without_llm_posts_the_suggest_comment(tmp_path: Path) -> None:
    _seed_checkout(tmp_path)
    harness = _make(
        tmp_path,
        pr_number=42,
        collectors=[_FixedCollector(Route.CODE_BUG)],
        runner=_fix_runner(tmp_path),
        llm=NullLLM(),
    )
    outcomes = _capture_fix(harness)

    result = await harness.processor(_job())

    assert outcomes and outcomes[0].status == "no_llm"
    assert outcomes[0].suspects
    assert "fix=no_llm" in (result.detail or "")
    assert harness.client.pull_requests == []
    bodies = [body for _, body in harness.client.comments]
    assert any("Janus analysis (no PR opened)" in body for body in bodies)
    assert _rows(harness.factory, Suspect)


async def test_policy_violating_diff_is_rejected_without_a_pr(tmp_path: Path) -> None:
    _seed_checkout(tmp_path)
    harness = _make(
        tmp_path,
        pr_number=42,
        collectors=[_FixedCollector(Route.CODE_BUG)],
        runner=_fix_runner(tmp_path),
        llm=_ScriptedLLM([_ci_weakening_diff()]),
        settings=_settings(max_fix_attempts=1),
    )
    outcomes = _capture_fix(harness)

    result = await harness.processor(_job())

    fix = outcomes[0]
    assert fix.status == "rejected"
    assert any(violation.rule == "weakens_ci" for violation in fix.violations)
    assert "fix=rejected" in (result.detail or "")
    assert harness.client.pull_requests == []
    fixes = _rows(harness.factory, Fix)
    assert [row.status for row in fixes] == ["rejected"]
    assert fixes[0].diff == _ci_weakening_diff().strip()


async def test_test_file_edit_is_rejected_by_the_policy_gate(tmp_path: Path) -> None:
    _seed_checkout(tmp_path)
    harness = _make(
        tmp_path,
        pr_number=42,
        collectors=[_FixedCollector(Route.CODE_BUG)],
        runner=_fix_runner(tmp_path),
        llm=_ScriptedLLM([_test_edit_diff()]),
        settings=_settings(max_fix_attempts=1),
    )
    outcomes = _capture_fix(harness)

    await harness.processor(_job())

    fix = outcomes[0]
    assert fix.status == "rejected"
    assert any(violation.rule == "edits_tests" for violation in fix.violations)
    assert harness.client.pull_requests == []


async def test_proven_fix_opens_a_draft_pr_and_never_forces(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _seed_checkout(tmp_path)
    (tmp_path / ".janus.yml").write_text("mode: pr\n", encoding="utf-8")
    pushes: list[dict[str, Any]] = []

    def fake_push(**kwargs: Any) -> None:
        pushes.append(kwargs)

    monkeypatch.setattr(processor_module, "commit_and_push", fake_push)
    harness = _make(
        tmp_path,
        collectors=[_FixedCollector(Route.CODE_BUG)],
        runner=_fix_runner(tmp_path),
        llm=_ScriptedLLM([_clean_diff()]),
        token_provider=lambda _installation_id: "installation-token",
    )
    outcomes = _capture_fix(harness)

    result = await harness.processor(_job())

    fix = outcomes[0]
    assert fix.status == "proven"
    assert "fix=proven" in (result.detail or "")
    assert len(pushes) == 1
    assert pushes[0]["branch"] == "janus/fix-7"
    assert pushes[0]["token"] == "installation-token"
    assert "force" not in str(pushes[0]).lower()

    assert len(harness.client.pull_requests) == 1
    pr = harness.client.pull_requests[0]
    assert pr["head"] == "janus/fix-7"
    assert pr["base"] == "main"
    assert pr["draft"] is True
    assert not hasattr(harness.client, "merge_pull_request")


async def test_janus_yml_suggest_mode_never_opens_a_pr(tmp_path: Path, monkeypatch: Any) -> None:
    _seed_checkout(tmp_path)
    (tmp_path / ".janus.yml").write_text("mode: suggest\n", encoding="utf-8")
    pushes: list[dict[str, Any]] = []

    def fake_push(**kwargs: Any) -> None:
        pushes.append(kwargs)

    monkeypatch.setattr(processor_module, "commit_and_push", fake_push)
    harness = _make(
        tmp_path,
        pr_number=42,
        collectors=[_FixedCollector(Route.CODE_BUG)],
        runner=_fix_runner(tmp_path),
        llm=_ScriptedLLM([_clean_diff()]),
        settings=_settings(fix_mode="pr"),
        token_provider=lambda _installation_id: "installation-token",
    )
    outcomes = _capture_fix(harness)

    await harness.processor(_job())

    assert outcomes[0].status == "proven"
    assert pushes == []
    assert harness.client.pull_requests == []
    bodies = [body for _, body in harness.client.comments]
    assert any(
        "Janus analysis (no PR opened)" in body and "suggest mode" in body for body in bodies
    )


async def test_gate_failure_is_gates_failed_and_never_opens_a_pr(tmp_path: Path) -> None:
    _seed_checkout(tmp_path)
    (tmp_path / ".janus.yml").write_text("mode: pr\n", encoding="utf-8")
    harness = _make(
        tmp_path,
        pr_number=42,
        collectors=[_FixedCollector(Route.CODE_BUG)],
        runner=_fix_runner(tmp_path, suite_passes=False),
        llm=_ScriptedLLM([_clean_diff()]),
        settings=_settings(max_fix_attempts=1),
        token_provider=lambda _installation_id: "installation-token",
    )
    outcomes = _capture_fix(harness)

    result = await harness.processor(_job())

    fix = outcomes[0]
    assert fix.status == "gates_failed"
    assert "fix=gates_failed" in (result.detail or "")
    assert harness.client.pull_requests == []
    fixes = _rows(harness.factory, Fix)
    assert [row.status for row in fixes] == ["gates_failed"]
    gates = _rows(harness.factory, GateResult)
    assert any(row.gate == "suite_green" and not row.passed for row in gates)
