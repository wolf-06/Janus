"""The shared "adapter commands → runnable plan" helper.

Both the job processor and the reproducer turn an adapter's commands into the ordered
sandbox phases of a single run. Keeping that translation here — rather than in each caller —
means the setup/test phase rules, the network policy and the artifact propagation have
exactly one implementation, so the two callers cannot drift apart.

The plan types themselves live in :mod:`janus.sandbox.profiles`; this module only knows how
to build one from an :class:`~janus.core.adapter.Adapter` and a
:class:`~janus.sandbox.settings.SandboxSettings`, and how to pick one phase's result out of a
run. It holds no policy beyond "setup needs the network, the test phase never does".
"""

from __future__ import annotations

from collections.abc import Sequence

from janus.core.adapter import Adapter, TestTarget
from janus.sandbox.profiles import Profile, RunPhase, RunPlan
from janus.sandbox.settings import SandboxSettings
from janus.sandbox.spec import SandboxPhase, SandboxResult


def build_plan(
    adapter: Adapter,
    sandbox: SandboxSettings,
    *,
    targets: Sequence[TestTarget] = (),
    profile: Profile = Profile.CLEAN,
) -> RunPlan:
    """Build the run plan for one job from the adapter's commands.

    One setup phase is emitted per :meth:`Adapter.setup_commands`, but only when the sandbox
    configuration allows the network (a network-less setup could not install anything, so it
    is skipped entirely). The single test phase runs the given ``targets`` offline; an empty
    ``targets`` means "the whole suite". Each phase carries its command's ``argv`` and
    ``artifacts`` so the plan is the one place a spec is materialised from.

    A disabled sandbox yields a plan with no phases, which is the caller's signal to skip the
    container (see :attr:`~janus.sandbox.profiles.RunPlan.disabled`).
    """
    if not sandbox.enabled:
        return RunPlan(profile=profile, phases=())

    phases: list[RunPhase] = []
    if sandbox.network:
        phases.extend(
            RunPhase(
                phase=SandboxPhase.SETUP,
                command=command.argv,
                network=True,
                artifacts=command.artifacts,
            )
            for command in adapter.setup_commands()
        )

    test = adapter.test_command(targets)
    phases.append(
        RunPhase(
            phase=SandboxPhase.TEST,
            command=test.argv,
            network=False,
            artifacts=test.artifacts,
        )
    )
    return RunPlan(profile=profile, phases=tuple(phases))


def phase_result(results: Sequence[SandboxResult], phase: SandboxPhase) -> SandboxResult | None:
    """The result for ``phase``, or ``None`` when that phase did not run.

    A plan runs at most one phase of each kind, so the first match is the whole answer.
    """
    return next((result for result in results if result.phase is phase), None)
