"""The **flake_history** signal: how often these targets have failed historically.

``ctx.flake_history`` maps a target id to its historical failure rate. A target that already
fails often is very likely flaky now (:attr:`~janus.core.triage.routes.Route.FLAKY`); a target
with a near-perfect history is very unlikely to be flaky, so its failure now leans
:attr:`~janus.core.triage.routes.Route.CODE_BUG`. Rates between the two thresholds are
inconclusive and the collector abstains, as it does when it has no history for any failing
target.
"""

from __future__ import annotations

from typing import Any, Final

from janus.core.signals.base import SignalContext, SignalEvidence, clamp_votes
from janus.core.triage.routes import Route

FLAKY_THRESHOLD: Final[float] = 0.3
"""A historical failure rate at or above this is "high" → ``FLAKY``."""

STABLE_THRESHOLD: Final[float] = 0.05
"""A historical failure rate at or below this is "low" → ``CODE_BUG``."""


class FlakeHistoryCollector:
    """Vote ``FLAKY`` on high historical rates, ``CODE_BUG`` on low ones, else abstain."""

    name = "flake_history"

    def collect(self, ctx: SignalContext) -> SignalEvidence | None:
        rates = {
            target: float(ctx.flake_history[target])
            for target in ctx.failing_targets
            if target in ctx.flake_history
        }
        if not rates:
            return None

        highest = max(rates.values())
        votes: dict[Route, float]
        if highest >= FLAKY_THRESHOLD:
            votes = {Route.FLAKY: 0.8}
            summary = (
                f"historical failure rate up to {highest:.2f} "
                f"(>= {FLAKY_THRESHOLD:.2f}) across {len(rates)} target(s)"
            )
        elif all(rate <= STABLE_THRESHOLD for rate in rates.values()):
            votes = {Route.CODE_BUG: 0.5}
            summary = (
                f"historical failure rate at most {highest:.2f} "
                f"(<= {STABLE_THRESHOLD:.2f}) — targets are normally stable"
            )
        else:
            return None

        details: dict[str, Any] = {
            "rates": dict(sorted(rates.items())),
            "highest_rate": highest,
            "flaky_threshold": FLAKY_THRESHOLD,
            "stable_threshold": STABLE_THRESHOLD,
        }
        return SignalEvidence(
            name=self.name,
            summary=summary,
            details=details,
            votes=clamp_votes(votes),
        )
