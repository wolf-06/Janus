"""The correlation collector: a CI fact (cache miss, runner, retry, matrix leg) → ENV."""

from __future__ import annotations

import json
from typing import Any

from janus.core.evidence import ChangeSummary, Reproduction
from janus.core.signals.base import SignalContext
from janus.core.signals.correlation import CorrelationCollector
from janus.core.triage.routes import Route
from janus.sandbox.profiles import Profile


def _ctx(metadata: dict[str, Any]) -> SignalContext:
    return SignalContext(
        reproduction=Reproduction(profile=Profile.CLEAN, attempts=(), full_suite=None),
        change=ChangeSummary(base_sha=None, head_sha="abc"),
        failing_targets=("tests/test_x.py::test_y",),
        metadata=metadata,
    )


def test_cache_miss_votes_env() -> None:
    evidence = CorrelationCollector().collect(_ctx({"cache_hit": False}))

    assert evidence is not None
    assert evidence.votes[Route.ENV] > 0.0
    assert "cache_miss" in evidence.details["reasons"]


def test_retry_votes_env() -> None:
    evidence = CorrelationCollector().collect(_ctx({"retry_of": "job-123"}))

    assert evidence is not None
    assert "retry_of_failing_job" in evidence.details["reasons"]


def test_runner_change_votes_env() -> None:
    evidence = CorrelationCollector().collect(
        _ctx({"runner": "ubuntu-24.04", "previous_runner": "ubuntu-22.04"})
    )

    assert evidence is not None
    assert "runner_change" in evidence.details["reasons"]


def test_same_runner_is_not_a_change() -> None:
    assert (
        CorrelationCollector().collect(
            _ctx({"runner": "ubuntu-24.04", "previous_runner": "ubuntu-24.04"})
        )
        is None
    )


def test_matrix_leg_only_fails_votes_env() -> None:
    evidence = CorrelationCollector().collect(
        _ctx({"matrix_leg": "py3.12", "matrix_only_fails_here": True})
    )

    assert evidence is not None
    assert "matrix_leg_only_fails" in evidence.details["reasons"]


def test_no_facts_abstains() -> None:
    assert CorrelationCollector().collect(_ctx({})) is None
    assert CorrelationCollector().collect(_ctx({"cache_hit": True})) is None


def test_votes_bounded_and_details_json_serialisable() -> None:
    evidence = CorrelationCollector().collect(_ctx({"cache_hit": False}))
    assert evidence is not None

    assert all(0.0 <= value <= 1.0 for value in evidence.votes.values())
    assert Route.UNSURE not in evidence.votes
    assert json.dumps(evidence.details)
