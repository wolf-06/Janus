"""The scorer: aggregate the seven signals' votes into route scores, then decide.

This module is the only place that turns per-route **votes** (from the collectors) into
per-route **scores** and a :class:`~janus.core.triage.routes.TriageDecision`. It is pure and
total: no I/O, no clock, no randomness, no shared state. Two functions:

* :func:`aggregate` — the weighted average of the per-route votes over the signals that
  actually reported. The denominator is the sum of the weights of the **present** signals, so
  a collector that abstains (returns ``None``) does not dilute the score, and a signal whose
  name is not in ``weights`` is ignored entirely. ``UNSURE`` is never produced here — it is the
  decision rule's reserved output.
* :func:`score` — aggregate, build a short per-signal ``reasons`` tuple, and delegate to
  :func:`janus.core.triage.routes.decide`.

The ``tau``/``epsilon`` rule itself lives in :mod:`janus.core.triage.routes`; this module only
feeds it scores and reasons. Both functions are built against the frozen types and change
nothing about them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from janus.core.signals.base import SignalEvidence
from janus.core.triage.routes import (
    DEFAULT_EPSILON,
    DEFAULT_TAU,
    DEFAULT_WEIGHTS,
    TRIAGE_ROUTES,
    Route,
    TriageDecision,
    decide,
)

_REASON_MAX_CHARS: Final[int] = 160
"""Upper bound on one ``reasons`` entry, so a verbose collector summary cannot flood the
evidence comment. ``score`` truncates to this length with an ellipsis."""


def _vote(votes: Mapping[Route, float], route: Route) -> float:
    """Read one route's vote, defaulting to ``0`` and clamping into ``[0, 1]``.

    A missing route means the collector did not vote for it (support ``0``). Clamping here
    mirrors :func:`janus.core.signals.base.clamp_votes`: the ``[0, 1]`` contract is the
    collector's, but the scorer refuses to let a stray out-of-range value skew a route score.
    """
    return min(1.0, max(0.0, float(votes.get(route, 0.0))))


def aggregate(
    evidence: Sequence[SignalEvidence],
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
) -> dict[Route, float]:
    """Weighted average of the per-route votes over the signals that reported.

    For each of the three real routes the numerator is ``sum(weight * vote)`` across the
    present signals and the denominator is the sum of those signals' weights — so a collector
    that abstained does not drag the score toward zero. A signal whose ``name`` is not a key in
    ``weights`` is skipped (its vote is not counted, in numerator or denominator). When no
    present signal has a usable weight the result is all-zero scores. The returned mapping has
    exactly the three routes in :data:`TRIAGE_ROUTES`; ``UNSURE`` is never a key.
    """
    totals: dict[Route, float] = dict.fromkeys(TRIAGE_ROUTES, 0.0)
    present_weight = 0.0
    for item in evidence:
        weight = weights.get(item.name)
        if weight is None:
            continue
        present_weight += weight
        for route in TRIAGE_ROUTES:
            totals[route] += weight * _vote(item.votes, route)

    if present_weight <= 0.0:
        return dict.fromkeys(TRIAGE_ROUTES, 0.0)
    return {route: totals[route] / present_weight for route in TRIAGE_ROUTES}


def _reason(name: str, summary: str) -> str:
    """One bounded ``reasons`` entry: the signal name, a colon, and its summary."""
    collapsed = " ".join(summary.split())
    text = f"{name}: {collapsed}" if collapsed else f"{name}: (no summary)"
    if len(text) > _REASON_MAX_CHARS:
        text = text[: _REASON_MAX_CHARS - 3].rstrip() + "..."
    return text


def score(
    evidence: Sequence[SignalEvidence],
    *,
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
    tau: float = DEFAULT_TAU,
    epsilon: float = DEFAULT_EPSILON,
) -> TriageDecision:
    """Aggregate ``evidence`` into scores, build reasons, and apply the decision rule.

    The ``reasons`` are one bounded entry per signal (prefixed with the signal's name),
    followed by the rule's own justification for the route. With no evidence at all the scores
    are all zero, the route is ``UNSURE`` with confidence ``0.0``, and the first reason says no
    signal evidence was collected. The decision itself is delegated to
    :func:`janus.core.triage.routes.decide`, so ``tau``/``epsilon`` and the tie-break live in
    one place.
    """
    items = tuple(evidence)
    scores = aggregate(items, weights)
    reasons: tuple[str, ...]
    if items:
        reasons = tuple(_reason(item.name, item.summary) for item in items)
    else:
        reasons = ("no signal evidence was collected",)
    return decide(scores, reasons=reasons, tau=tau, epsilon=epsilon)
