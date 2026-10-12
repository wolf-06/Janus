"""The flake_history collector: high rate → FLAKY, low rate → CODE_BUG, else abstain."""

from __future__ import annotations

import json

from janus.core.evidence import ChangeSummary, Reproduction
from janus.core.signals.base import SignalContext
from janus.core.signals.flake_history import FlakeHistoryCollector
from janus.core.triage.routes import Route
from janus.sandbox.profiles import Profile


def _ctx(flake_history: dict[str, float]) -> SignalContext:
    return SignalContext(
        reproduction=Reproduction(profile=Profile.CLEAN, attempts=(), full_suite=None),
        change=ChangeSummary(base_sha=None, head_sha="abc"),
        failing_targets=("tests/test_x.py::test_y",),
        flake_history=flake_history,
    )


def test_high_rate_votes_flaky() -> None:
    evidence = FlakeHistoryCollector().collect(_ctx({"tests/test_x.py::test_y": 0.5}))

    assert evidence is not None
    assert evidence.votes[Route.FLAKY] >= 0.5
    assert evidence.details["highest_rate"] == 0.5


def test_low_rate_votes_code_bug() -> None:
    evidence = FlakeHistoryCollector().collect(_ctx({"tests/test_x.py::test_y": 0.0}))

    assert evidence is not None
    assert Route.CODE_BUG in evidence.votes


def test_exactly_threshold_rate_is_flaky() -> None:
    evidence = FlakeHistoryCollector().collect(_ctx({"tests/test_x.py::test_y": 0.3}))

    assert evidence is not None
    assert Route.FLAKY in evidence.votes


def test_exactly_stable_threshold_votes_code_bug() -> None:
    evidence = FlakeHistoryCollector().collect(_ctx({"tests/test_x.py::test_y": 0.05}))

    assert evidence is not None
    assert Route.CODE_BUG in evidence.votes


def test_middle_rate_abstains() -> None:
    assert FlakeHistoryCollector().collect(_ctx({"tests/test_x.py::test_y": 0.2})) is None


def test_no_history_abstains() -> None:
    assert FlakeHistoryCollector().collect(_ctx({})) is None
    assert FlakeHistoryCollector().collect(_ctx({"other/test.py::test_z": 0.9})) is None


def test_votes_bounded_and_details_json_serialisable() -> None:
    evidence = FlakeHistoryCollector().collect(_ctx({"tests/test_x.py::test_y": 0.4}))
    assert evidence is not None

    assert all(0.0 <= value <= 1.0 for value in evidence.votes.values())
    assert Route.UNSURE not in evidence.votes
    assert json.dumps(evidence.details)
