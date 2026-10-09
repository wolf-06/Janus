"""Sandbox processor: source acquisition, spec building, and result reporting.

No Docker and no network: the adapter is the canned ``FakeAdapter``, the source provider is
a context manager over a temp dir, the runner returns canned ``SandboxResult``s, and the
session factory hands back stub repo/run rows.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from janus.app.config import Settings
from janus.db.models import Job, Repository, Run
from janus.jobs.processor import SandboxProcessor, build_processor
from janus.jobs.state import JobStatus
from janus.jobs.worker import stub_processor
from janus.sandbox.settings import SandboxSettings
from janus.sandbox.spec import SandboxPhase, SandboxResult, SandboxSpec
from janus.source import SourceSpec
from tests.conformance.fake_adapter import (
    FAILING_REPORT,
    PASSING_RESULT,
    REPORT_PATH,
    FakeAdapter,
)

_REPO = SimpleNamespace(owner="acme", name="widgets", installation_id=1)
_RUN = SimpleNamespace(head_sha="deadbeef")


class _StubSession:
    """A minimal async session exposing only ``get``."""

    def __init__(self, repo: object | None, run: object | None) -> None:
        self._repo = repo
        self._run = run

    async def __aenter__(self) -> _StubSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, model: type, pk: int) -> object | None:
        if model is Repository:
            return self._repo
        if model is Run:
            return self._run
        return None


class _StubSessionFactory:
    def __init__(self, repo: object | None, run: object | None) -> None:
        self._repo = repo
        self._run = run

    def __call__(self) -> _StubSession:
        return _StubSession(self._repo, self._run)


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
    """A runner returning canned results and recording the specs it was given."""

    def __init__(self, results: Sequence[SandboxResult]) -> None:
        self._results = list(results)
        self.specs: list[SandboxSpec] = []
        self.source_dirs: list[Path | None] = []

    def run(self, spec: SandboxSpec, *, source_dir: Path | None = None) -> SandboxResult:
        self.specs.append(spec)
        self.source_dirs.append(source_dir)
        return self._results[len(self.specs) - 1]


def _settings(*, network: bool = False, enabled: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        sandbox=SandboxSettings(network=network, enabled=enabled),
    )


def _job() -> Job:
    return Job(id=7, delivery_id="d1", repo_id=2, run_id=3)


def _make(
    results: Sequence[SandboxResult],
    *,
    network: bool = False,
    repo: object | None = _REPO,
    run: object | None = _RUN,
) -> tuple[SandboxProcessor, _StubRunner, _StubSource]:
    source = _StubSource(Path("/tmp/checkout"))
    runner = _StubRunner(results)
    processor = SandboxProcessor(
        session_factory=_StubSessionFactory(repo, run),
        adapter=FakeAdapter(),
        source=source,
        runner=runner,
        settings=_settings(network=network),
    )
    return processor, runner, source


async def test_specs_carry_argv_artifacts_and_offline_test_phase() -> None:
    setup_result = SandboxResult(
        image="fake:latest", phase=SandboxPhase.SETUP, exit_code=0, duration_s=0.5
    )
    processor, runner, source = _make([setup_result, PASSING_RESULT], network=True)

    result = await processor(_job())

    assert result.status is JobStatus.SUCCEEDED
    assert source.entered == 1
    assert source.exited == 1
    assert source.spec is not None
    assert source.spec.owner == "acme"
    assert source.spec.ref == "deadbeef"
    assert len(runner.specs) == 2

    setup_spec, test_spec = runner.specs
    assert setup_spec.phase is SandboxPhase.SETUP
    assert setup_spec.command == ("python", "-m", "pip", "install", "-r", "requirements.txt")
    assert setup_spec.artifacts == ()
    assert setup_spec.network is True

    assert test_spec.phase is SandboxPhase.TEST
    assert test_spec.command == ("python", "-m", "pytest", "-q", "tests")
    assert test_spec.artifacts == (REPORT_PATH,)
    assert test_spec.network is False
    assert runner.source_dirs == [Path("/tmp/checkout"), Path("/tmp/checkout")]


async def test_source_context_is_exited_on_failure_path() -> None:
    error_result = SandboxResult(image="fake:latest", phase=SandboxPhase.TEST, error="boom")
    processor, _, source = _make([error_result])

    result = await processor(_job())

    assert result.retryable is True
    assert source.entered == 1
    assert source.exited == 1


async def test_test_phase_error_is_retryable_and_mentions_error() -> None:
    error_result = SandboxResult(image="fake:latest", phase=SandboxPhase.TEST, error="boom")
    processor, _, _ = _make([error_result])

    result = await processor(_job())

    assert result.status is JobStatus.FAILED
    assert result.retryable is True
    assert "boom" in (result.detail or "")


async def test_timed_out_result_is_retryable() -> None:
    timed_out = SandboxResult(image="fake:latest", phase=SandboxPhase.TEST, timed_out=True)
    processor, _, _ = _make([timed_out])

    result = await processor(_job())

    assert result.status is JobStatus.FAILED
    assert result.retryable is True
    assert "timed_out" in (result.detail or "")


async def test_successful_run_reports_counts_and_cost() -> None:
    setup_result = SandboxResult(
        image="fake:latest", phase=SandboxPhase.SETUP, exit_code=0, duration_s=30.0
    )
    test_result = SandboxResult(
        image="fake:latest",
        phase=SandboxPhase.TEST,
        exit_code=1,
        duration_s=30.0,
        artifacts={REPORT_PATH: FAILING_REPORT},
    )
    processor, _, _ = _make([setup_result, test_result], network=True)

    result = await processor(_job())

    assert result.status is JobStatus.SUCCEEDED
    assert result.cost_minutes == 1.0
    detail = result.detail or ""
    assert "0 passed" in detail
    assert "2 failed" in detail
    assert "2 outcomes" in detail
    assert "tests/test_math.py::test_add" in detail


async def test_missing_repo_or_run_is_non_retryable() -> None:
    processor, runner, source = _make([PASSING_RESULT], repo=None)

    result = await processor(_job())

    assert result.status is JobStatus.FAILED
    assert result.retryable is False
    assert result.detail == "repo or run missing"
    assert source.entered == 0
    assert runner.specs == []


def test_build_processor_returns_stub_when_sandbox_disabled() -> None:
    settings = _settings(enabled=False)
    processor = build_processor(settings, _StubSessionFactory(None, None))
    assert processor is stub_processor
