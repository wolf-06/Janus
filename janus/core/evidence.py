"""The **evidence** types shared by the reproducer, the signal collectors and the scorer.

These dataclasses carry *measured facts* — what actually happened when the failing targets
were rerun, and what changed between the last green run and the first red one. They never
carry a verdict: no route, no confidence, no "this is a code bug". A collector reads a
``Reproduction`` / ``ChangeSummary`` and turns it into a
:class:`~janus.core.signals.base.SignalEvidence`; the scorer turns the signals into a route.
Keeping the facts and the judgement apart is what lets the reproducer, the seven collectors
and the scorer be built in parallel against this one frozen surface.

Everything here is a frozen dataclass (hashable, immutable, safe to pass across threads and
to log). ``from __future__ import annotations`` keeps the annotations lazy.
"""

from __future__ import annotations

from dataclasses import dataclass

from janus.sandbox.profiles import Profile


@dataclass(frozen=True)
class RunAttempt:
    """One run of one target (or of the full suite) inside the sandbox.

    A single ``RunAttempt`` is the atom of reproduction evidence: the reproducer produces
    one per rerun. The counters below are deliberately plain measured facts — a collector
    decides what they *mean*.
    """

    index: int
    """0-based attempt number within its phase (the K reruns, or the single full-suite run)."""

    target_id: str | None
    """The failing target this run exercised; ``None`` means it was the whole-suite run."""

    passed: bool
    """Whether the target/suite passed in this run, as the adapter reported it."""

    duration_s: float
    """Wall-clock duration of the run, in seconds."""

    exit_code: int | None
    """Process exit code, or ``None`` when the sandbox never produced one (e.g. infra error)."""

    timed_out: bool
    """True when the run hit the sandbox wall-clock cap and was killed."""

    oom_killed: bool
    """True when the kernel/daemon OOM-killed the container."""

    cpu_seconds: float
    """Cumulative CPU seconds consumed by the container."""

    peak_mem_mb: float
    """Peak resident memory observed for the container, in MiB."""

    stdout_excerpt: str
    """Redacted, length-bounded stdout. Evidence only — never the raw stream."""

    stderr_excerpt: str
    """Redacted, length-bounded stderr. Evidence only — never the raw stream."""

    sandbox_error: str | None = None
    """Infrastructure failure (image unavailable, daemon unreachable, …), else ``None``."""


@dataclass(frozen=True)
class Reproduction:
    """The whole reproduction phase: the K reruns plus the final full-suite run.

    ``attempts`` holds the reruns of the *failing* targets (the reproducer's K loop);
    ``full_suite`` is the single whole-suite run made at the end. The derived helpers are
    the raw material the determinism / resources / flake-history collectors read.
    """

    profile: Profile
    """Which environment was reproduced: ``clean`` (deterministic) or ``ambient``."""

    attempts: tuple[RunAttempt, ...]
    """The K reruns of the failing targets, in run order."""

    full_suite: RunAttempt | None
    """The single full-suite run made after the reruns; ``None`` when it was not made."""

    ambient_reconstructed: bool = True
    """False when an ambient run could not match the failed run's image/env and fell back
    to clean. Feeds the triage decision (an unreconstructed ambient run leans to UNSURE)."""

    cost_minutes: float = 0.0
    """Sandbox minutes consumed by this reproduction, for the per-repo cost cap."""

    @property
    def failures(self) -> int:
        """Number of reruns that did **not** pass."""
        return sum(1 for attempt in self.attempts if not attempt.passed)

    @property
    def successes(self) -> int:
        """Number of reruns that passed."""
        return sum(1 for attempt in self.attempts if attempt.passed)

    def failure_rate(self) -> float:
        """Fraction of reruns that failed, in ``[0, 1]``. Zero when there were no reruns."""
        if not self.attempts:
            return 0.0
        return self.failures / len(self.attempts)

    def always_fails(self) -> bool:
        """True when there was at least one rerun and every rerun failed.

        The empty case is deliberately ``False`` (not vacuously true), so a reproduction
        with no attempts is neither "always fails" nor "passes always".
        """
        return bool(self.attempts) and self.failures == len(self.attempts)

    def intermittent(self) -> bool:
        """True when the reruns both passed and failed — the signature of a flaky target."""
        return self.successes > 0 and self.failures > 0

    def passes_always(self) -> bool:
        """True when there was at least one rerun and every rerun passed."""
        return bool(self.attempts) and self.successes == len(self.attempts)

    @property
    def total_duration_s(self) -> float:
        """Total measured seconds: every rerun plus the full-suite run when it was made."""
        total = sum(attempt.duration_s for attempt in self.attempts)
        if self.full_suite is not None:
            total += self.full_suite.duration_s
        return total


@dataclass(frozen=True)
class CommitSummary:
    """One commit in the last-green → first-red range."""

    sha: str
    """The commit SHA."""

    message: str
    """The commit subject/message (redacted/bounded before it reaches any UI)."""

    files: tuple[str, ...]
    """Paths touched by this commit, as reported by the commits API."""


@dataclass(frozen=True)
class ChangeSummary:
    """What changed between the last green run and the first red one.

    This is the *change attribution* input. When there is no usable baseline (no last-green
    run on the branch) the range is unavailable and every field below the two SHAs is empty;
    ``available`` reports that so a collector can decline to vote instead of guessing.
    """

    base_sha: str | None
    """Last-green commit, or ``None`` when no baseline could be resolved."""

    head_sha: str
    """The first-red (head) commit."""

    commits: tuple[CommitSummary, ...] = ()
    """Commits in the range, oldest first."""

    files_changed: tuple[str, ...] = ()
    """Union of files touched by the commits in the range."""

    diff_excerpt: str = ""
    """A redacted, bounded excerpt of the diff. Evidence only."""

    @property
    def available(self) -> bool:
        """False when there is no usable baseline (``base_sha`` is ``None``)."""
        return self.base_sha is not None
