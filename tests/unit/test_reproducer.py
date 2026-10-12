"""The reproducer: K reruns plus one full-suite run, measured as evidence.

No Docker and no event loop: the runner is a stub that records the specs it was given and
returns canned ``SandboxResult``s, and the adapter is the canned ``FakeAdapter``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from janus.app.config import Settings
from janus.core.adapter import TestTarget as Target
from janus.core.reproducer import Reproducer, ReproduceRequest
from janus.sandbox.profiles import Profile
from janus.sandbox.settings import SandboxSettings
from janus.sandbox.spec import SandboxPhase, SandboxResult, SandboxSpec
from tests.conformance.fake_adapter import (
    FAILING_REPORT,
    NODE_ADD,
    NODE_SUB,
    PASSING_REPORT,
    REPORT_PATH,
    FakeAdapter,
)

_SOURCE = Path("/tmp/checkout")

_TARGETS = (
    Target(id=NODE_ADD, node_ids=(NODE_ADD,)),
    Target(id=NODE_SUB, node_ids=(NODE_SUB,)),
)

_Factory = Callable[[SandboxSpec], SandboxResult]


class _StubRunner:
    """A runner returning factory-produced results and recording the specs it was given."""

    def __init__(self, factory: _Factory) -> None:
        self._factory = factory
        self.specs: list[SandboxSpec] = []
        self.source_dirs: list[Path | None] = []

    def run(self, spec: SandboxSpec, *, source_dir: Path | None = None) -> SandboxResult:
        self.specs.append(spec)
        self.source_dirs.append(source_dir)
        return self._factory(spec)


def _test_result(
    *,
    passed: bool,
    duration_s: float = 1.0,
    stdout: str = "",
    stderr: str = "",
    error: str | None = None,
    timed_out: bool = False,
) -> SandboxResult:
    report = PASSING_REPORT if passed else FAILING_REPORT
    exit_code = None if error is not None else (0 if passed else 1)
    return SandboxResult(
        image="fake:latest",
        phase=SandboxPhase.TEST,
        exit_code=exit_code,
        duration_s=duration_s,
        stdout=stdout,
        stderr=stderr,
        error=error,
        timed_out=timed_out,
        artifacts={REPORT_PATH: report},
    )


def _setup_result(*, duration_s: float = 0.0) -> SandboxResult:
    return SandboxResult(
        image="fake:latest", phase=SandboxPhase.SETUP, exit_code=0, duration_s=duration_s
    )


def _is_rerun(spec: SandboxSpec) -> bool:
    return "::" in " ".join(spec.command)


def _settings(*, network: bool = False, max_chars: int = 2000) -> Settings:
    return Settings(
        _env_file=None,
        sandbox=SandboxSettings(network=network),
        max_signal_detail_chars=max_chars,
    )


def _reproducer(
    factory: _Factory,
    *,
    network: bool = False,
    max_chars: int = 2000,
) -> tuple[Reproducer, _StubRunner]:
    runner = _StubRunner(factory)
    reproducer = Reproducer(
        adapter=FakeAdapter(),
        runner=runner,  # type: ignore[arg-type]
        settings=_settings(network=network, max_chars=max_chars),
    )
    return reproducer, runner


def test_exactly_k_reruns_then_one_full_suite_in_order() -> None:
    reproducer, runner = _reproducer(lambda spec: _test_result(passed=_is_rerun(spec)))

    reproducer.reproduce(_SOURCE, ReproduceRequest(targets=_TARGETS, k=3))

    reruns = [spec for spec in runner.specs if _is_rerun(spec)]
    full = [spec for spec in runner.specs if not _is_rerun(spec)]
    assert len(reruns) == 3
    assert len(full) == 1
    assert runner.specs[-1] is full[0]
    assert all(NODE_ADD in spec.command and NODE_SUB in spec.command for spec in reruns)
    assert full[0].command == ("python", "-m", "pytest", "-q", "tests")


def test_setup_phase_only_when_network_is_true() -> None:
    reproducer, runner = _reproducer(
        lambda spec: (
            _setup_result() if spec.phase is SandboxPhase.SETUP else _test_result(passed=False)
        ),
        network=True,
    )

    reproducer.reproduce(_SOURCE, ReproduceRequest(targets=_TARGETS, k=2))

    assert [spec.phase for spec in runner.specs] == [
        SandboxPhase.SETUP,
        SandboxPhase.TEST,
        SandboxPhase.SETUP,
        SandboxPhase.TEST,
        SandboxPhase.SETUP,
        SandboxPhase.TEST,
    ]


def test_no_setup_phase_when_network_is_false() -> None:
    reproducer, runner = _reproducer(lambda spec: _test_result(passed=False))

    reproducer.reproduce(_SOURCE, ReproduceRequest(targets=_TARGETS, k=2))

    assert all(spec.phase is SandboxPhase.TEST for spec in runner.specs)


def test_attempts_are_flattened_per_target_per_run() -> None:
    reproducer, _ = _reproducer(lambda spec: _test_result(passed=False))

    rep = reproducer.reproduce(_SOURCE, ReproduceRequest(targets=_TARGETS, k=2))

    assert [(a.index, a.target_id) for a in rep.attempts] == [
        (0, NODE_ADD),
        (0, NODE_SUB),
        (1, NODE_ADD),
        (1, NODE_SUB),
    ]
    assert rep.failures == 4
    assert rep.always_fails() is True


def test_sandbox_error_yields_attempts_and_does_not_raise() -> None:
    reproducer, _ = _reproducer(lambda spec: _test_result(passed=False, error="daemon_unreachable"))

    rep = reproducer.reproduce(_SOURCE, ReproduceRequest(targets=_TARGETS, k=2))

    assert len(rep.attempts) == 4
    assert all(a.sandbox_error == "daemon_unreachable" for a in rep.attempts)
    assert all(a.passed is False for a in rep.attempts)
    assert rep.full_suite is not None
    assert rep.full_suite.sandbox_error == "daemon_unreachable"


def test_timed_out_run_records_attempts_with_sandbox_error() -> None:
    reproducer, _ = _reproducer(lambda spec: _test_result(passed=False, timed_out=True))

    rep = reproducer.reproduce(_SOURCE, ReproduceRequest(targets=_TARGETS, k=1))

    assert len(rep.attempts) == 2
    assert all(a.timed_out for a in rep.attempts)
    assert all(a.sandbox_error == "timed_out" for a in rep.attempts)


def test_cost_minutes_sums_all_sandbox_durations() -> None:
    reproducer, _ = _reproducer(
        lambda spec: (
            _test_result(passed=False, duration_s=30.0)
            if _is_rerun(spec)
            else _test_result(passed=True, duration_s=60.0)
        )
    )

    rep = reproducer.reproduce(_SOURCE, ReproduceRequest(targets=_TARGETS, k=1))

    assert rep.cost_minutes == pytest.approx(1.5)


def test_cost_includes_setup_phase_durations() -> None:
    def factory(spec: SandboxSpec) -> SandboxResult:
        if spec.phase is SandboxPhase.SETUP:
            return _setup_result(duration_s=6.0)
        return _test_result(passed=False, duration_s=30.0)

    reproducer, _ = _reproducer(factory, network=True)

    rep = reproducer.reproduce(_SOURCE, ReproduceRequest(targets=_TARGETS, k=1))

    # rerun: setup 6 + test 30; full suite: setup 6 + test 30 -> 72s
    assert rep.cost_minutes == pytest.approx(72.0 / 60.0)


def test_empty_targets_skips_reruns_and_runs_full_suite_once() -> None:
    reproducer, runner = _reproducer(lambda spec: _test_result(passed=True))

    rep = reproducer.reproduce(_SOURCE, ReproduceRequest(targets=(), k=3))

    assert rep.attempts == ()
    assert len(runner.specs) == 1
    assert rep.full_suite is not None
    assert rep.full_suite.target_id is None


def test_k_zero_is_treated_as_one_rerun() -> None:
    reproducer, runner = _reproducer(lambda spec: _test_result(passed=False))

    rep = reproducer.reproduce(_SOURCE, ReproduceRequest(targets=_TARGETS, k=0))

    assert len([spec for spec in runner.specs if _is_rerun(spec)]) == 1
    assert len(rep.attempts) == 2


def test_excerpts_are_bounded_to_max_signal_detail_chars() -> None:
    reproducer, _ = _reproducer(
        lambda spec: _test_result(passed=False, stdout="A" * 100, stderr="B" * 100),
        max_chars=10,
    )

    rep = reproducer.reproduce(_SOURCE, ReproduceRequest(targets=_TARGETS, k=1))

    assert all(a.stdout_excerpt == "A" * 10 for a in rep.attempts)
    assert all(a.stderr_excerpt == "B" * 10 for a in rep.attempts)
    assert rep.full_suite is not None
    assert rep.full_suite.stdout_excerpt == "A" * 10
    assert rep.full_suite.stderr_excerpt == "B" * 10


def test_a_normal_red_suite_is_not_a_sandbox_error() -> None:
    reproducer, _ = _reproducer(lambda spec: _test_result(passed=False))

    rep = reproducer.reproduce(_SOURCE, ReproduceRequest(targets=_TARGETS, k=1))

    assert all(a.sandbox_error is None for a in rep.attempts)
    assert rep.full_suite is not None
    assert rep.full_suite.sandbox_error is None
    assert rep.full_suite.passed is False


def test_ambient_profile_sets_ambient_reconstructed_false() -> None:
    reproducer, _ = _reproducer(lambda spec: _test_result(passed=False))

    rep = reproducer.reproduce(
        _SOURCE, ReproduceRequest(targets=_TARGETS, k=1, profile=Profile.AMBIENT)
    )

    assert rep.profile is Profile.AMBIENT
    assert rep.ambient_reconstructed is False


def test_clean_profile_is_reconstructed() -> None:
    reproducer, _ = _reproducer(lambda spec: _test_result(passed=False))

    rep = reproducer.reproduce(_SOURCE, ReproduceRequest(targets=_TARGETS, k=1))

    assert rep.profile is Profile.CLEAN
    assert rep.ambient_reconstructed is True


def test_source_dir_is_passed_to_every_run() -> None:
    reproducer, runner = _reproducer(lambda spec: _test_result(passed=False))

    reproducer.reproduce(_SOURCE, ReproduceRequest(targets=_TARGETS, k=2))

    assert runner.source_dirs == [_SOURCE, _SOURCE, _SOURCE]
