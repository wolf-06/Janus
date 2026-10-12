"""The **correlation** signal: CI facts that correlate the failure with the pipeline itself.

Some red runs are explained entirely by *where* and *how* they ran: a cache miss that rebuilt
the world, a different runner, a matrix leg that only fails there, or a retry of a job that was
already failing. This collector reads those CI facts out of ``ctx.metadata`` and votes
:attr:`~janus.core.triage.routes.Route.ENV`. With no such fact it abstains — the absence of a
correlation is not evidence for anything.

Recognised facts (all optional):

* ``cache_hit`` is present and falsy → cache miss.
* ``retry_of`` is present and truthy → retry of a previously failing job.
* ``runner`` and ``previous_runner`` are both present and differ → runner change.
* ``matrix_leg`` is present and ``matrix_only_fails_here`` (or ``other_legs_green``) is truthy
  → only this matrix leg fails.
"""

from __future__ import annotations

from typing import Any

from janus.core.signals.base import SignalContext, SignalEvidence, clamp_votes
from janus.core.triage.routes import Route


def _truthy(value: Any) -> bool:
    return bool(value)


class CorrelationCollector:
    """Vote ``ENV`` when a CI fact explains the failure, else abstain."""

    name = "correlation"

    def collect(self, ctx: SignalContext) -> SignalEvidence | None:
        metadata = ctx.metadata
        reasons: list[str] = []

        if "cache_hit" in metadata and not _truthy(metadata["cache_hit"]):
            reasons.append("cache_miss")

        if _truthy(metadata.get("retry_of")):
            reasons.append("retry_of_failing_job")

        runner = metadata.get("runner")
        previous_runner = metadata.get("previous_runner")
        if runner is not None and previous_runner is not None and runner != previous_runner:
            reasons.append("runner_change")

        if metadata.get("matrix_leg") is not None and (
            _truthy(metadata.get("matrix_only_fails_here"))
            or _truthy(metadata.get("other_legs_green"))
        ):
            reasons.append("matrix_leg_only_fails")

        if not reasons:
            return None

        details: dict[str, Any] = {
            "reasons": reasons,
            "runner": runner,
            "previous_runner": previous_runner,
            "matrix_leg": metadata.get("matrix_leg"),
            "cache_hit": metadata.get("cache_hit"),
            "retry_of": metadata.get("retry_of"),
        }
        return SignalEvidence(
            name=self.name,
            summary="CI correlation suggests an infrastructure cause: " + ", ".join(reasons),
            details=details,
            votes=clamp_votes({Route.ENV: 0.6}),
        )
