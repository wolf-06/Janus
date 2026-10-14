"""Spectrum-based fault localization (SBFL) over per-test coverage.

SBFL scores a source line by contrasting the tests that cover it: a line executed by many
failing tests and few passing tests is suspicious. That contrast can only be computed from
**per-test** coverage contexts. File-level coverage says only that *some* test touched a
file; it cannot tell us which tests were failing, so neither ``ef`` (failing tests covering
the line) nor ``ep`` (passing tests covering it) is recoverable. ``CoverageContext`` carries
exactly that per-test breakdown and is the input this module expects.

Two classic formulas are supported. Ochiai is the default; DStar (with exponent ``*`` = 2)
is the alternative. Everything here is pure and total: malformed or empty input yields an
empty ranking, and degenerate denominators are guarded rather than raised.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum

from janus.core.adapter import CoverageContext


class SbflFormula(StrEnum):
    """The supported spectrum-based scoring formulas."""

    OCHIAI = "OCHIAI"
    """``ef / sqrt((ef + nf) * (ef + ep))`` — bounded in ``[0, 1]``."""

    DSTAR = "DSTAR"
    """``ef**2 / (ep + (nf - ef))`` — DStar with exponent ``*`` = 2."""


@dataclass(frozen=True)
class SuspectLine:
    """One ranked ``file:line`` suspect produced by :func:`localize`."""

    file: str
    """Path of the source file, as it appeared in the coverage context."""

    line: int
    """1-based source line number."""

    score: float
    """The formula's suspicion score; higher is more suspicious."""

    rank: int
    """1-based rank, highest score first (ties broken by ``file`` then ``line``)."""


def _ochiai(ef: int, nf: int, ep: int) -> float:
    denominator = math.sqrt((ef + nf) * (ef + ep))
    if denominator <= 0.0:
        return 0.0
    return ef / denominator


def _dstar(ef: int, nf: int, ep: int) -> float:
    denominator = ep + (nf - ef)
    if denominator <= 0:
        # Every failing test covers the line and no passing test does: the formula is
        # undefined (a zero denominator). Return a finite, ordered stand-in instead of
        # dividing by zero; it keeps the line highly suspicious without producing inf.
        return float(ef * ef)
    return (ef * ef) / denominator


def localize(
    contexts: Sequence[CoverageContext],
    failing_test_ids: Collection[str],
    *,
    formula: SbflFormula = SbflFormula.OCHIAI,
    max_suspects: int = 5,
) -> list[SuspectLine]:
    """Rank source lines by SBFL suspicion.

    ``contexts`` is one ``CoverageContext`` per test. A context whose ``test_id`` appears in
    ``failing_test_ids`` is failing; every other context is passing. For each ``(file, line)``
    the counters are ``ef`` / ``ep`` (failing / passing tests covering it) and
    ``nf`` / ``np`` (the totals minus coverage), all derived from the supplied contexts.

    A line with ``ef == 0`` — covered only by passing tests — is not a suspect and is
    omitted. Duplicate coverage for the same test and repeated ``(file, line)`` pairs are
    counted once. The result is ordered by descending score, with ties broken by ``file``
    then ``line``, and is capped at ``max_suspects``. An empty input, or failing ids that
    never appear in ``contexts``, yields ``[]``.
    """
    if not contexts or max_suspects <= 0:
        return []

    failing = set(failing_test_ids)

    # Merge contexts for the same test id so a duplicated context cannot double-count as
    # two separate tests. Storing locations in a set also drops repeated pairs.
    covered_by_test: dict[str, set[tuple[str, int]]] = {}
    for context in contexts:
        covered = covered_by_test.setdefault(context.test_id, set())
        for file, lines in context.files.items():
            for line in lines:
                covered.add((file, line))

    if not covered_by_test:
        return []

    nf_total = sum(1 for test_id in covered_by_test if test_id in failing)
    ef_counts: dict[tuple[str, int], int] = {}
    ep_counts: dict[tuple[str, int], int] = {}
    for test_id, covered in covered_by_test.items():
        counts = ef_counts if test_id in failing else ep_counts
        for location in covered:
            counts[location] = counts.get(location, 0) + 1

    scored: list[tuple[float, str, int]] = []
    for (file, line), ef in ef_counts.items():
        if ef == 0:
            continue
        ep = ep_counts.get((file, line), 0)
        nf = nf_total - ef
        if formula is SbflFormula.OCHIAI:
            score = _ochiai(ef, nf, ep)
        else:
            score = _dstar(ef, nf, ep)
        if not math.isfinite(score):
            score = 0.0
        scored.append((score, file, line))

    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [
        SuspectLine(file=file, line=line, score=score, rank=rank)
        for rank, (score, file, line) in enumerate(scored[:max_suspects], start=1)
    ]
