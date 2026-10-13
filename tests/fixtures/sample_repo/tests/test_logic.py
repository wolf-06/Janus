"""The deterministic CODE_BUG case.

``sum_to`` is off by one, so this assertion fails on every run. The failure is stable:
there is no randomness, no timing and no external state involved.
"""

from __future__ import annotations

from src.calc import clamp, sum_to


def test_sum_to_includes_the_last_term() -> None:
    assert sum_to(3) == 6


def test_clamp_is_correct() -> None:
    assert clamp(5, 0, 10) == 5
    assert clamp(-1, 0, 10) == 0
    assert clamp(11, 0, 10) == 10
