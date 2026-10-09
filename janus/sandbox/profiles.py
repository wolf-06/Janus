"""Turn settings into the ordered list of container runs for a job.

Two profiles exist:

* ``clean``   — a fresh, repeatable environment. Same result every time.
* ``ambient`` — an attempt to match the environment the real CI run used. When that cannot
  be reconstructed we fall back to ``clean`` and record ``ambient_reconstructed = False``,
  which later feeds the triage decision.

A run is up to two phases. **Phase A (setup)** installs dependencies and may use the
network. **Phase B (test)** runs the tests and is always offline. If the configuration
forbids network access, phase A is skipped entirely and only phase B runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from janus.sandbox.settings import SandboxSettings
from janus.sandbox.spec import SandboxPhase, SandboxSpec, build_spec


class Profile(StrEnum):
    """Which environment a run reproduces."""

    CLEAN = "clean"
    AMBIENT = "ambient"


@dataclass(frozen=True)
class RunPhase:
    """One command to run in the sandbox, plus the artifacts it produces.

    ``artifacts`` are the absolute container paths the command writes that the runner reads
    back before the container is removed. Carrying them here (rather than only on the
    adapter's ``Command``) means the plan is the single place a spec is materialised from.
    """

    phase: SandboxPhase
    command: tuple[str, ...]
    network: bool
    artifacts: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunPlan:
    """The phases to execute, in order."""

    profile: Profile
    phases: tuple[RunPhase, ...]
    ambient_reconstructed: bool = True

    @property
    def disabled(self) -> bool:
        """True when the sandbox is switched off, so no container should be started."""
        return not self.phases

    @property
    def skipped_setup(self) -> bool:
        """True when the setup phase was dropped because network access is disabled."""
        return all(phase.phase is not SandboxPhase.SETUP for phase in self.phases)

    def specs(self, settings: SandboxSettings) -> tuple[SandboxSpec, ...]:
        """Materialise the phases into runnable specs, carrying each phase's artifacts."""
        return tuple(
            build_spec(
                settings,
                command=phase.command,
                phase=phase.phase,
                network=phase.network,
                artifacts=phase.artifacts,
            )
            for phase in self.phases
        )


def build_plan(
    settings: SandboxSettings,
    *,
    profile: Profile = Profile.CLEAN,
    setup_command: Sequence[str] | None = None,
    test_command: Sequence[str] | None = None,
    ambient_reconstructed: bool = True,
) -> RunPlan:
    """Build the run plan for one job.

    Explicit ``setup_command``/``test_command`` override the configured defaults. The setup
    phase is included only when the configuration allows network access *and* a setup
    command exists; otherwise the plan goes straight to the test phase. When the sandbox is
    disabled the plan has no phases, which is the caller's signal to skip the container.
    """
    if not settings.enabled:
        return RunPlan(profile=profile, phases=(), ambient_reconstructed=ambient_reconstructed)

    setup = tuple(setup_command) if setup_command is not None else tuple(settings.setup_command)
    test = tuple(test_command) if test_command is not None else tuple(settings.test_command)

    phases: list[RunPhase] = []
    if settings.network and setup:
        phases.append(RunPhase(SandboxPhase.SETUP, setup, network=True))
    phases.append(RunPhase(SandboxPhase.TEST, test, network=False))

    return RunPlan(
        profile=profile,
        phases=tuple(phases),
        ambient_reconstructed=ambient_reconstructed,
    )


def default_plan(
    settings: SandboxSettings,
    *,
    setup_command: Sequence[str] | None = None,
    test_command: Sequence[str] | None = None,
) -> RunPlan:
    """A clean-profile plan using configured defaults."""
    return build_plan(
        settings,
        profile=Profile.CLEAN,
        setup_command=setup_command,
        test_command=test_command,
    )
