"""The change_attribution collector: diff touching the failing tests → CODE_BUG, else ENV."""

from __future__ import annotations

import json

from janus.core.evidence import ChangeSummary, Reproduction
from janus.core.signals.base import SignalContext
from janus.core.signals.change_attribution import ChangeAttributionCollector
from janus.core.triage.routes import Route
from janus.sandbox.profiles import Profile


def _ctx(
    files_changed: tuple[str, ...],
    *,
    base_sha: str | None = "base",
    failing_targets: tuple[str, ...] = ("tests/test_x.py::test_y",),
) -> SignalContext:
    return SignalContext(
        reproduction=Reproduction(profile=Profile.CLEAN, attempts=(), full_suite=None),
        change=ChangeSummary(base_sha=base_sha, head_sha="head", files_changed=files_changed),
        failing_targets=failing_targets,
    )


def test_unavailable_baseline_abstains() -> None:
    assert ChangeAttributionCollector().collect(_ctx(("tests/test_x.py",), base_sha=None)) is None


def test_changed_file_matches_failing_test_file() -> None:
    evidence = ChangeAttributionCollector().collect(_ctx(("tests/test_x.py",)))

    assert evidence is not None
    assert evidence.votes[Route.CODE_BUG] >= 0.8
    assert evidence.details["related_files"] == ["tests/test_x.py"]


def test_basename_match_counts_as_related() -> None:
    evidence = ChangeAttributionCollector().collect(_ctx(("src/deep/test_x.py",)))

    assert evidence is not None
    assert Route.CODE_BUG in evidence.votes


def test_unrelated_change_votes_weak_env() -> None:
    evidence = ChangeAttributionCollector().collect(_ctx(("src/app.py",)))

    assert evidence is not None
    assert evidence.votes[Route.ENV] <= 0.5
    assert evidence.details["related_files"] == []


def test_votes_bounded_and_details_json_serialisable() -> None:
    evidence = ChangeAttributionCollector().collect(_ctx(("tests/test_x.py",)))
    assert evidence is not None

    assert all(0.0 <= value <= 1.0 for value in evidence.votes.values())
    assert Route.UNSURE not in evidence.votes
    assert json.dumps(evidence.details)
