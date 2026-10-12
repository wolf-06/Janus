"""The **resources** signal: cgroup telemetry and wall-clock spread across the reruns.

Resource telemetry separates an environmental break from a code bug. An OOM kill is a direct
:attr:`~janus.core.triage.routes.Route.ENV` verdict; a peak memory close to the sandbox cap
(``ctx.metadata["memory_mb"]``, when present) suggests the run was squeezed by its limits; and
a duration an order of magnitude above the fastest attempt suggests a stall (a lock, a
network hang) rather than a deterministic logic failure. Absent any of those, the telemetry
is unremarkable and weakly supports :attr:`~janus.core.triage.routes.Route.CODE_BUG`.
"""

from __future__ import annotations

from typing import Any, Final

from janus.core.signals.base import SignalContext, SignalEvidence, clamp_votes
from janus.core.triage.routes import Route

_MEMORY_RATIO: Final[float] = 0.9
"""Peak memory at or above this fraction of the cap counts as "close to the cap"."""

_DURATION_RATIO: Final[float] = 10.0
"""A slowest attempt this many times the fastest is "an order of magnitude" slower."""


def _as_float(value: Any) -> float | None:
    """Best-effort numeric coercion for a metadata value; ``None`` when not numeric."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


class ResourcesCollector:
    """Vote ``ENV`` on OOM / memory-pressure / stalls, else weakly ``CODE_BUG``."""

    name = "resources"

    def collect(self, ctx: SignalContext) -> SignalEvidence | None:
        attempts = ctx.reproduction.attempts
        if not attempts:
            return None

        oom_killed = any(attempt.oom_killed for attempt in attempts)
        peak_mem = max(attempt.peak_mem_mb for attempt in attempts)
        durations = [attempt.duration_s for attempt in attempts]
        fastest = min(durations)
        slowest = max(durations)

        cap = _as_float(ctx.metadata.get("memory_mb"))
        near_cap = cap is not None and cap > 0 and peak_mem >= _MEMORY_RATIO * cap
        stalled = fastest > 0 and slowest >= _DURATION_RATIO * fastest

        votes: dict[Route, float]
        if oom_killed:
            votes = {Route.ENV: 0.9}
            summary = "a rerun was OOM-killed"
        elif near_cap:
            votes = {Route.ENV: 0.7}
            summary = f"peak memory {peak_mem:.0f} MiB is near the {cap:.0f} MiB cap"
        elif stalled:
            votes = {Route.ENV: 0.4}
            summary = f"slowest rerun is {slowest / fastest:.1f}x the fastest (possible stall)"
        else:
            votes = {Route.CODE_BUG: 0.3}
            summary = "resource telemetry is unremarkable"

        details: dict[str, Any] = {
            "attempts": len(attempts),
            "oom_killed": oom_killed,
            "peak_mem_mb": peak_mem,
            "memory_cap_mb": cap,
            "fastest_duration_s": fastest,
            "slowest_duration_s": slowest,
            "duration_ratio": (slowest / fastest) if fastest > 0 else None,
            "near_memory_cap": near_cap,
            "stalled": stalled,
        }
        return SignalEvidence(
            name=self.name,
            summary=summary,
            details=details,
            votes=clamp_votes(votes),
        )
