"""The control case: always green.

The reproduce/triage loop needs a baseline that must never change, so a diagnosis that
somehow blames this test is clearly wrong.
"""

from __future__ import annotations

from src.calc import clamp


def test_always_passes() -> None:
    assert 2 + 2 == 4


def test_clamp_control() -> None:
    assert clamp(3, 0, 10) == 3
