"""``clamp_votes`` drops the reserved ``UNSURE`` vote and clamps support into ``[0, 1]``."""

from __future__ import annotations

from janus.core.evidence import ChangeSummary, Reproduction
from janus.core.signals.base import SignalContext, SignalEvidence, clamp_votes
from janus.core.triage.routes import Route
from janus.sandbox.profiles import Profile


def test_clamp_votes_drops_unsure() -> None:
    votes = clamp_votes({Route.CODE_BUG: 0.4, Route.UNSURE: 0.9})

    assert Route.UNSURE not in votes
    assert votes == {Route.CODE_BUG: 0.4}


def test_clamp_votes_clamps_out_of_range_values() -> None:
    votes = clamp_votes({Route.CODE_BUG: 1.5, Route.FLAKY: -0.5, Route.ENV: 0.5})

    assert votes[Route.CODE_BUG] == 1.0
    assert votes[Route.FLAKY] == 0.0
    assert votes[Route.ENV] == 0.5


def test_clamp_votes_preserves_in_range_values() -> None:
    votes = clamp_votes({Route.ENV: 0.0, Route.FLAKY: 1.0})

    assert votes == {Route.ENV: 0.0, Route.FLAKY: 1.0}


def test_clamp_votes_on_empty_mapping() -> None:
    assert clamp_votes({}) == {}


def test_signal_evidence_holds_its_fields() -> None:
    evidence = SignalEvidence(
        name="determinism",
        summary="3/3 reruns failed",
        details={"failures": 3, "attempts": 3},
        votes={Route.CODE_BUG: 0.9},
    )

    assert evidence.name == "determinism"
    assert evidence.votes[Route.CODE_BUG] == 0.9


def test_signal_context_defaults_mappings_to_empty() -> None:
    ctx = SignalContext(
        reproduction=Reproduction(profile=Profile.CLEAN, attempts=(), full_suite=None),
        change=ChangeSummary(base_sha=None, head_sha="abc"),
        failing_targets=("tests/test_x.py::test_a",),
    )

    assert ctx.flake_history == {}
    assert ctx.metadata == {}
