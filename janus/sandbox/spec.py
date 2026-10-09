"""Sandbox execution spec and result.

A ``SandboxSpec`` is a plain description of one container run — image, command, limits,
network. ``SandboxResult`` is what comes back. Keeping both as plain data (rather than live
containers) means they can be logged, asserted on, and tested without Docker.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from janus.sandbox.allowlist import docker_network_mode
from janus.sandbox.settings import SandboxSettings


class SandboxPhase(StrEnum):
    """Which half of a run a command belongs to."""

    SETUP = "setup"
    TEST = "test"


@dataclass(frozen=True)
class SandboxSpec:
    """One container run, fully described."""

    image: str
    command: tuple[str, ...]
    workdir: str
    user: str
    timeout_seconds: float
    memory_mb: int
    cpus: float
    pids_limit: int
    network: bool = False
    allow_hosts: tuple[str, ...] = ()
    phase: SandboxPhase = SandboxPhase.TEST
    pull_image: bool = True
    max_output_bytes: int = 1_048_576
    # Absolute container paths to read back before the container is removed, and the per-file
    # size cap. A structured result (a pytest JSON report, a coverage file) lives only inside
    # the container otherwise, since stdout/stderr is all the caller sees.
    artifacts: tuple[str, ...] = ()
    max_artifact_bytes: int = 4 * 1024 * 1024

    @property
    def network_mode(self) -> str:
        """Docker network mode: ``none`` unless the run is explicitly allowed a network."""
        return docker_network_mode(self.network)


@dataclass
class SandboxResult:
    """What a container run produced."""

    image: str
    phase: SandboxPhase
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False
    oom_killed: bool = False
    cpu_seconds: float = 0.0
    peak_mem_mb: float = 0.0
    # Infrastructure failure (image unavailable, daemon unreachable, …) rather than a
    # non-zero exit from the command itself.
    error: str | None = None
    command: tuple[str, ...] = field(default_factory=tuple)
    # Requested container paths → raw bytes. Deliberately *not* redacted: these are structured
    # data the core parses, and redaction could corrupt a report or a coverage map. See
    # ``SandboxRunner.run``.
    artifacts: dict[str, bytes] = field(default_factory=dict)
    # Requested paths that could not be read, mapped to why (``not_found``, ``too_large``,
    # ``unreadable: ...``). Never silently dropped, and never truncated.
    artifact_errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True only for a clean, in-time, zero-exit run with no infrastructure error."""
        return self.error is None and not self.timed_out and self.exit_code == 0


def build_spec(
    settings: SandboxSettings,
    *,
    command: Sequence[str],
    phase: SandboxPhase = SandboxPhase.TEST,
    network: bool | None = None,
    artifacts: Sequence[str] = (),
) -> SandboxSpec:
    """Build a ``SandboxSpec`` from settings.

    ``network`` defaults to the phase's intent: setup may use the network, the test phase
    never does. It is forced off when the configuration forbids network access. ``artifacts``
    lists absolute container paths the runner reads back before removing the container.
    """
    if network is None:
        use_network = phase is SandboxPhase.SETUP
    else:
        use_network = network
    if not settings.network:
        use_network = False
    return SandboxSpec(
        image=settings.image,
        command=tuple(command),
        workdir=settings.workdir,
        user=settings.user,
        timeout_seconds=settings.timeout_seconds,
        memory_mb=settings.memory_mb,
        cpus=settings.cpus,
        pids_limit=settings.pids_limit,
        network=use_network,
        allow_hosts=tuple(settings.allow_hosts) if use_network else (),
        phase=phase,
        pull_image=settings.pull_image,
        max_output_bytes=settings.max_output_bytes,
        artifacts=tuple(artifacts),
        max_artifact_bytes=settings.max_artifact_bytes,
    )
