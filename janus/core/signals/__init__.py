"""The seven signal collectors and the registry that runs them.

Each signal lives in its own module and exposes a class with a ``name`` and a
``collect(ctx) -> SignalEvidence | None`` method. :func:`default_collectors` returns one of
each in :data:`~janus.core.triage.routes.SIGNAL_NAMES` order, and :func:`collect_all` runs a
set of collectors against one :class:`~janus.core.signals.base.SignalContext`, dropping the
abstentions (``None``) and surviving a collector that raises — one broken collector must never
break triage.
"""

from __future__ import annotations

from collections.abc import Iterable

import structlog

from janus.core.signals.base import Collector, SignalContext, SignalEvidence
from janus.core.signals.change_attribution import ChangeAttributionCollector
from janus.core.signals.correlation import CorrelationCollector
from janus.core.signals.determinism import DeterminismCollector
from janus.core.signals.exit_codes import ExitCodesCollector
from janus.core.signals.flake_history import FlakeHistoryCollector
from janus.core.signals.resources import ResourcesCollector
from janus.core.signals.signatures import SignaturesCollector

logger = structlog.get_logger(__name__)

__all__ = ["collect_all", "default_collectors"]


def default_collectors() -> tuple[Collector, ...]:
    """All seven collectors, in :data:`~janus.core.triage.routes.SIGNAL_NAMES` order."""
    return (
        DeterminismCollector(),
        SignaturesCollector(),
        ExitCodesCollector(),
        ResourcesCollector(),
        ChangeAttributionCollector(),
        CorrelationCollector(),
        FlakeHistoryCollector(),
    )


def collect_all(collectors: Iterable[Collector], ctx: SignalContext) -> list[SignalEvidence]:
    """Run every collector, skipping abstentions and any collector that raises.

    A collector returning ``None`` has no evidence to offer and is simply omitted. A collector
    that throws is logged and skipped, so a single faulty signal cannot take down triage.
    """
    evidence: list[SignalEvidence] = []
    for collector in collectors:
        try:
            result = collector.collect(ctx)
        except Exception:  # noqa: BLE001 - one bad collector must not break triage
            logger.exception("signal collector failed", signal=collector.name)
            continue
        if result is not None:
            evidence.append(result)
    return evidence
