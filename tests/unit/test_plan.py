"""The shared adapter → run-plan helper: phases, argv/artifacts, and phase lookup."""

from __future__ import annotations

from janus.core.adapter import TestTarget as Target
from janus.core.plan import build_plan, phase_result
from janus.sandbox.profiles import Profile
from janus.sandbox.settings import SandboxSettings
from janus.sandbox.spec import SandboxPhase, SandboxResult
from tests.conformance.fake_adapter import REPORT_PATH, FakeAdapter


def test_network_disabled_runs_test_phase_only() -> None:
    plan = build_plan(FakeAdapter(), SandboxSettings(network=False))

    assert plan.profile is Profile.CLEAN
    assert [phase.phase for phase in plan.phases] == [SandboxPhase.TEST]
    test = plan.phases[0]
    assert test.command == ("python", "-m", "pytest", "-q", "tests")
    assert test.artifacts == (REPORT_PATH,)
    assert test.network is False


def test_network_enabled_adds_setup_phase_with_argv_and_artifacts() -> None:
    plan = build_plan(FakeAdapter(), SandboxSettings(network=True))

    assert [phase.phase for phase in plan.phases] == [SandboxPhase.SETUP, SandboxPhase.TEST]
    setup, test = plan.phases
    assert setup.command == ("python", "-m", "pip", "install", "-r", "requirements.txt")
    assert setup.network is True
    assert setup.artifacts == ()
    assert test.network is False
    assert test.artifacts == (REPORT_PATH,)


def test_targets_appear_in_the_test_command() -> None:
    target = Target(id="a", node_ids=("tests/test_a.py::test_a",))
    plan = build_plan(FakeAdapter(), SandboxSettings(), targets=(target,))

    assert plan.phases[-1].command == (
        "python",
        "-m",
        "pytest",
        "-q",
        "tests/test_a.py::test_a",
    )


def test_profile_is_carried_onto_the_plan() -> None:
    plan = build_plan(FakeAdapter(), SandboxSettings(), profile=Profile.AMBIENT)
    assert plan.profile is Profile.AMBIENT


def test_disabled_sandbox_produces_no_phases() -> None:
    plan = build_plan(FakeAdapter(), SandboxSettings(enabled=False))

    assert plan.phases == ()
    assert plan.disabled is True
    assert plan.specs(SandboxSettings(enabled=False)) == ()


def test_phase_result_picks_the_matching_phase() -> None:
    setup = SandboxResult(image="i", phase=SandboxPhase.SETUP)
    test = SandboxResult(image="i", phase=SandboxPhase.TEST)

    assert phase_result([setup, test], SandboxPhase.TEST) is test
    assert phase_result([setup, test], SandboxPhase.SETUP) is setup
    assert phase_result([setup], SandboxPhase.TEST) is None
    assert phase_result([], SandboxPhase.TEST) is None
