"""Arithmetic helpers for the S1 sample repository.

One function here carries a deliberate off-by-one bug so the sample suite has a
deterministic CODE_BUG failure to reproduce. See ``README.md``.
"""

from __future__ import annotations


def clamp(value: int, low: int, high: int) -> int:
    """Clamp ``value`` into the inclusive range ``[low, high]``."""
    if value < low:
        return low
    if value > high:
        return high
    return value


def sum_to(n: int) -> int:
    """Return the sum of the integers from ``1`` to ``n`` inclusive.

    BUG: the range stops one short (``range(1, n)`` omits ``n``), so ``sum_to(3)`` is
    ``3`` (1 + 2) instead of the correct ``6``. The fix is ``range(1, n + 1)``.
    """
    total = 0
    for value in range(1, n):
        total += value
    return total
