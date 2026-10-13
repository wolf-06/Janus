"""The FLAKY case, driven by an explicit switch rather than randomness.

The outcome is chosen by the caller through the ``JANUS_SAMPLE_FLAKE`` environment
variable, so each run is deterministic for a given environment:

* unset or ``pass`` (the default) — the test passes;
* ``fail`` — the test fails with an assertion;
* anything else — the test fails loudly on the switch value itself.

A genuinely random flake would make the seed set non-deterministic, so it is avoided on
purpose. See ``README.md`` for how to drive it.
"""

from __future__ import annotations

import os

_ALLOWED = {"pass", "fail"}


def test_intermittent_switch() -> None:
    switch = os.environ.get("JANUS_SAMPLE_FLAKE", "pass").strip().lower()

    assert switch in _ALLOWED, (
        f"JANUS_SAMPLE_FLAKE must be one of {sorted(_ALLOWED)}, got {switch!r}"
    )
    assert switch != "fail", "driven flake: JANUS_SAMPLE_FLAKE=fail"
