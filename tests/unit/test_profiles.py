"""Run planning: two phases when network is allowed, test-only when it is not."""

from __future__ import annotations

from janus.sandbox.profiles import Profile, RunPhase, RunPlan, build_plan, default_plan
from janus.sandbox.settings import SandboxSettings
from janus.sandbox.spec import SandboxPhase


def test_network_disabled_skips_setup() -> None:
    settings = SandboxSettings(
        network=False,
        setup_command=["pip", "install", "-e", "."],
        test_command=["pytest", "-q"],
    )
    plan = build_plan(settings)

    assert plan.skipped_setup is True
    assert [phase.phase for phase in plan.phases] == [SandboxPhase.TEST]
    assert plan.phases[0].command == ("pytest", "-q")


def test_network_enabled_adds_setup_phase() -> None:
    settings = SandboxSettings(
        network=True,
        setup_command=["pip", "install", "-e", "."],
        test_command=["pytest", "-q"],
    )
    plan = build_plan(settings)

    assert plan.skipped_setup is False
    assert [phase.phase for phase in plan.phases] == [SandboxPhase.SETUP, SandboxPhase.TEST]
    assert plan.phases[0].network is True
    assert plan.phases[1].network is False


def test_network_enabled_without_setup_command_runs_test_only() -> None:
    settings = SandboxSettings(network=True, setup_command=[], test_command=["pytest"])
    plan = build_plan(settings)

    assert plan.skipped_setup is True
    assert len(plan.phases) == 1


def test_explicit_commands_override_settings() -> None:
    settings = SandboxSettings(network=True)
    plan = build_plan(settings, setup_command=["uv", "sync"], test_command=["pytest", "tests"])

    assert plan.phases[0].command == ("uv", "sync")
    assert plan.phases[1].command == ("pytest", "tests")


def test_default_plan_is_clean_profile() -> None:
    plan = default_plan(SandboxSettings(test_command=["pytest"]))
    assert plan.profile is Profile.CLEAN
    assert plan.ambient_reconstructed is True


def test_specs_materialise_network_flags() -> None:
    settings = SandboxSettings(
        network=True,
        setup_command=["pip", "install", "-e", "."],
        test_command=["pytest"],
    )
    specs = build_plan(settings).specs(settings)

    assert specs[0].network is True
    assert specs[0].network_mode == "bridge"
    assert specs[1].network is False
    assert specs[1].network_mode == "none"
    assert specs[1].allow_hosts == ()


def test_disabled_sandbox_produces_no_phases() -> None:
    plan = build_plan(SandboxSettings(enabled=False, test_command=["pytest"]))

    assert plan.disabled is True
    assert plan.phases == ()
    assert plan.specs(SandboxSettings(enabled=False)) == ()


def test_ambient_flag_is_carried() -> None:
    plan = build_plan(SandboxSettings(), profile=Profile.AMBIENT, ambient_reconstructed=False)
    assert plan.profile is Profile.AMBIENT
    assert plan.ambient_reconstructed is False


def test_run_phase_artifacts_default_to_empty() -> None:
    phase = RunPhase(SandboxPhase.TEST, ("pytest",), False)
    assert phase.artifacts == ()


def test_specs_carry_phase_artifacts() -> None:
    settings = SandboxSettings(network=True, test_command=["pytest"])
    plan = RunPlan(
        profile=Profile.CLEAN,
        phases=(
            RunPhase(
                SandboxPhase.SETUP,
                ("pip", "install"),
                True,
                artifacts=("/work/setup.json",),
            ),
            RunPhase(
                SandboxPhase.TEST,
                ("pytest",),
                False,
                artifacts=("/work/report.json",),
            ),
        ),
    )

    specs = plan.specs(settings)

    assert specs[0].artifacts == ("/work/setup.json",)
    assert specs[1].artifacts == ("/work/report.json",)
    assert specs[1].network is False
