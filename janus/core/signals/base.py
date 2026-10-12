"""The signal-collector interface.

A **collector** reads the measured evidence (a :class:`~janus.core.evidence.Reproduction`, a
:class:`~janus.core.evidence.ChangeSummary`, per-target flake history and CI correlation
facts) and emits one :class:`SignalEvidence`: a short human summary, a bag of raw details,
and **per-route support** votes in ``[0, 1]``. The scorer aggregates those votes with the
weights in :mod:`janus.core.triage.routes`; a collector never picks a route itself.

The seven signal names are frozen in
:data:`janus.core.triage.routes.SIGNAL_NAMES`; each collector's ``name`` is one of them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from janus.core.evidence import ChangeSummary, Reproduction
from janus.core.triage.routes import Route


@dataclass(frozen=True)
class SignalEvidence:
    """One collector's output: what it saw and how much it supports each route."""

    name: str
    """The signal name — one of :data:`janus.core.triage.routes.SIGNAL_NAMES`."""

    summary: str
    """A short, human-readable statement of what this signal observed."""

    details: Mapping[str, Any]
    """Raw measured detail (counts, matched signatures, …) for the evidence comment."""

    votes: Mapping[Route, float]
    """Per-route support in ``[0, 1]``. A missing route means 0. ``UNSURE`` is never voted
    (see :func:`clamp_votes`); the decision rule produces ``UNSURE``, collectors do not."""


@dataclass(frozen=True)
class SignalContext:
    """Everything a collector may read. Frozen, so collectors cannot mutate shared state."""

    reproduction: Reproduction
    """The reruns and the full-suite run — the primary evidence."""

    change: ChangeSummary
    """What changed between the last green run and the first red one."""

    failing_targets: tuple[str, ...]
    """The target ids CI reported as failing (the ids the reproducer reran)."""

    flake_history: Mapping[str, float] = field(default_factory=dict)
    """Target id → historical failure rate, for the flake-history signal."""

    metadata: Mapping[str, Any] = field(default_factory=dict)
    """CI correlation facts: runner, matrix leg, cache hit, and similar."""


class Collector(Protocol):
    """A signal collector: pure evidence in, one :class:`SignalEvidence` out.

    ``collect`` returns ``None`` when it has **no evidence available** — a signal that does
    not apply (e.g. no baseline diff, or no flake history for these targets). ``None`` means
    "abstain", not "vote zero": the scorer treats a missing signal as absent, not as a
    contradicting vote.
    """

    name: str

    def collect(self, ctx: SignalContext) -> SignalEvidence | None:
        """Produce evidence for this signal, or ``None`` to abstain."""
        ...


def clamp_votes(votes: Mapping[Route, float]) -> dict[Route, float]:
    """Drop any ``UNSURE`` vote and clamp the rest into ``[0, 1]``.

    Collectors vote only on the three real routes; ``UNSURE`` is the decision rule's
    reserved output. This helper is the single place that guarantees it, so a collector
    cannot invent an ``UNSURE`` vote and a stray out-of-range value cannot skew the scorer.
    """
    clamped: dict[Route, float] = {}
    for route, value in votes.items():
        if route is Route.UNSURE:
            continue
        clamped[route] = min(1.0, max(0.0, float(value)))
    return clamped
