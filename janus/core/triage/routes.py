"""Triage routes, the seven signal names, default weights and the decision rule.

This module freezes the **vocabulary** of triage (routes, signal names, weights) and the
**decision rule** from ``PLAN.md`` §1.2: score the seven signals into the three real
routes, then pick the argmax only if it clears ``tau`` *and* beats the runner-up by more
than ``epsilon``; otherwise the answer is ``UNSURE``. It deliberately does **not** aggregate
signal votes into route scores — that is the scorer's job (``janus/core/triage/scorer.py``)
and is built against these types.

Only ``CODE_BUG`` proceeds to a fix. ``FLAKY``, ``ENV`` and ``UNSURE`` are comment-only
(see :data:`COMMENT_ONLY_ROUTES`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class Route(StrEnum):
    """The outcome of triage. Three real routes plus ``UNSURE``."""

    CODE_BUG = "CODE_BUG"
    """A fault in the repository's own code — the only route that proceeds to a fix."""

    FLAKY = "FLAKY"
    """The failure is nondeterministic (a flaky test); comment only."""

    ENV = "ENV"
    """The failure is environmental or pipeline-related (infra break); comment only."""

    UNSURE = "UNSURE"
    """Not enough evidence to route confidently; comment only."""


TRIAGE_ROUTES: Final[tuple[Route, Route, Route]] = (Route.CODE_BUG, Route.FLAKY, Route.ENV)
"""The three real routes, in tie-break order (earlier wins an exact tie). ``UNSURE`` is not
a scored route: it is produced by the decision rule, never voted for by a collector."""

COMMENT_ONLY_ROUTES: Final[frozenset[Route]] = frozenset({Route.FLAKY, Route.ENV, Route.UNSURE})
"""Routes that must never proceed to a fix. Only ``CODE_BUG`` is absent."""

SIGNAL_NAMES: Final[tuple[str, ...]] = (
    "determinism",
    "signatures",
    "exit_codes",
    "resources",
    "change_attribution",
    "correlation",
    "flake_history",
)
"""The seven signal names. Stable dict keys used by collectors, the scorer and the reporter."""

DEFAULT_TAU: Final[float] = 0.80
"""Minimum top score for a real route to be chosen instead of ``UNSURE``."""

DEFAULT_EPSILON: Final[float] = 0.05
"""Minimum gap between the top two scores; a smaller gap forces ``UNSURE``."""

DEFAULT_WEIGHTS: Final[Mapping[str, float]] = MappingProxyType(
    {
        "determinism": 0.22,
        "signatures": 0.18,
        "change_attribution": 0.16,
        "exit_codes": 0.14,
        "correlation": 0.10,
        "flake_history": 0.10,
        "resources": 0.10,
    }
)
"""Default signal weights, summing to 1.0.

The two strongest discriminators carry the most weight: **determinism** (always-fail vs
intermittent separates CODE_BUG from FLAKY) and **signatures** (timeout / ECONNRESET / OOM /
SIGKILL identify an infra break). **change_attribution** (a diff between the last green and
the first red run) is the direct CODE_BUG evidence, so it ranks third. **exit_codes** is the
coarser taxonomy behind signatures; **correlation**, **flake_history** and **resources** are
each single-purpose corroborators. These are defaults only — the scorer may take overrides
from ``.janus.yml``.
"""


@dataclass(frozen=True)
class TriageDecision:
    """The frozen result of :func:`decide`."""

    route: Route
    """The chosen route; ``UNSURE`` when confidence or separation was too low."""

    confidence: float
    """The top real-route score, in ``[0, 1]`` (0 when no route was scored)."""

    top2_gap: float
    """Difference between the top two real-route scores (0 when fewer than two exist)."""

    scores: Mapping[Route, float]
    """All four routes → score, for rendering. ``UNSURE`` is always present (default 0)."""

    reasons: tuple[str, ...]
    """Human-readable justification, including the caller's ``reasons`` plus the rule's own."""


def decide(
    scores: Mapping[Route, float],
    *,
    reasons: Sequence[str] = (),
    tau: float = DEFAULT_TAU,
    epsilon: float = DEFAULT_EPSILON,
) -> TriageDecision:
    """Apply the ``PLAN.md`` §1.2 decision rule to per-route scores.

    The argmax is taken over the three real routes in :data:`TRIAGE_ROUTES` order. Ties are
    resolved deterministically: the earlier route in that order wins (``CODE_BUG`` >
    ``FLAKY`` > ``ENV``). The result is ``UNSURE`` when the top score is below ``tau`` or
    when the top two are within ``epsilon`` of each other; otherwise it is the argmax route.

    ``confidence`` is the top score and ``top2_gap`` the top-two difference. ``reasons``
    carries the caller's justification followed by the rule's own, so the evidence comment
    can show exactly why the route was chosen.
    """
    ranked = sorted(
        ((route, float(scores.get(route, 0.0))) for route in TRIAGE_ROUTES),
        key=lambda item: (-item[1], TRIAGE_ROUTES.index(item[0])),
    )
    top_route, top_score = ranked[0]
    second_score = ranked[1][1]
    gap = top_score - second_score

    notes = list(reasons)
    if top_score < tau:
        route = Route.UNSURE
        notes.append(f"top score {top_score:.3f} is below tau {tau:.2f}")
    elif gap < epsilon:
        route = Route.UNSURE
        notes.append(f"top two routes are within epsilon (gap {gap:.3f} < {epsilon:.2f})")
    else:
        route = top_route
        notes.append(f"{top_route.value} is the argmax at {top_score:.3f} (gap {gap:.3f})")

    return TriageDecision(
        route=route,
        confidence=top_score,
        top2_gap=gap,
        scores={r: float(scores.get(r, 0.0)) for r in Route},
        reasons=tuple(notes),
    )
