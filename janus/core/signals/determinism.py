"""The **determinism** signal: did the failing targets fail every rerun, or only sometimes?

This is the strongest discriminator in ``PLAN.md`` §1.2. A target that fails every rerun is
deterministic and points at a :attr:`~janus.core.triage.routes.Route.CODE_BUG`; one that both
passes and fails across the reruns is nondeterministic and points at
:attr:`~janus.core.triage.routes.Route.FLAKY`. A reproduction where every rerun passed offers
no evidence (the failure could not be reproduced) and the collector abstains, as it does when
there were no reruns at all.
"""

from __future__ import annotations

from typing import Any

from janus.core.signals.base import SignalContext, SignalEvidence, clamp_votes
from janus.core.triage.routes import Route


class DeterminismCollector:
    """Vote ``CODE_BUG`` on always-fail, ``FLAKY`` on intermittent, abstain otherwise."""

    name = "determinism"

    def collect(self, ctx: SignalContext) -> SignalEvidence | None:
        reproduction = ctx.reproduction
        attempts = reproduction.attempts
        if not attempts:
            return None

        failures = reproduction.failures
        successes = reproduction.successes
        total = len(attempts)
        rate = reproduction.failure_rate()
        sequence = ["pass" if attempt.passed else "fail" for attempt in attempts]

        if reproduction.always_fails():
            votes = clamp_votes({Route.CODE_BUG: 0.9})
            summary = f"{failures}/{total} reruns failed (deterministic failure)"
        elif reproduction.intermittent():
            votes = clamp_votes({Route.FLAKY: 0.9})
            summary = f"{failures}/{total} reruns failed (intermittent, flaky)"
        elif reproduction.passes_always():
            return None
        else:  # pragma: no cover - exhaustive over the three helpers above
            return None

        details: dict[str, Any] = {
            "attempts": total,
            "failures": failures,
            "successes": successes,
            "failure_rate": rate,
            "sequence": sequence,
        }
        return SignalEvidence(
            name=self.name,
            summary=summary,
            details=details,
            votes=votes,
        )
