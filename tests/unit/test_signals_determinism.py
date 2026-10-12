"""The determinism collector: always-fail → CODE_BUG, intermittent → FLAKY, else abstain."""

from __future__ import annotations

import json

from janus.core.evidence import ChangeSummary, Reproduction, RunAttempt
from janus.core.signals.base import SignalContext
from janus.core.signals.determinism import DeterminismCollector
from janus.core.triage.routes import Route
from janus.sandbox.profiles import Profile


def _attempt(index: int, passed: bool) -> RunAttempt:
    return RunAttempt(
        index=index,
        target_id="tests/test_x.py::test_y",
        passed=passed,
        duration_s=1.0,
        exit_code=0 if passed else 1,
        timed_out=False,
        oom_killed=False,
        cpu_seconds=1.0,
        peak_mem_mb=100.0,
        stdout_excerpt="",
        stderr_excerpt="",
    )


def _ctx(passed: tuple[bool, ...]) -> SignalContext:
    reproduction = Reproduction(
        profile=Profile.CLEAN,
        attempts=tuple(_attempt(i, ok) for i, ok in enumerate(passed)),
        full_suite=None,
    )
    return SignalContext(
        reproduction=reproduction,
        change=ChangeSummary(base_sha=None, head_sha="abc"),
        failing_targets=("tests/test_x.py::test_y",),
    )


def test_always_fails_votes_code_bug() -> None:
    evidence = DeterminismCollector().collect(_ctx((False, False, False)))

    assert evidence is not None
    assert evidence.name == "determinism"
    assert evidence.votes[Route.CODE_BUG] >= 0.8
    assert Route.UNSURE not in evidence.votes
    assert evidence.details["failure_rate"] == 1.0
    assert evidence.details["sequence"] == ["fail", "fail", "fail"]


def test_intermittent_votes_flaky() -> None:
    evidence = DeterminismCollector().collect(_ctx((False, True, False)))

    assert evidence is not None
    assert evidence.votes[Route.FLAKY] >= 0.8
    assert evidence.details["failures"] == 2
    assert evidence.details["successes"] == 1


def test_passes_always_abstains() -> None:
    assert DeterminismCollector().collect(_ctx((True, True, True))) is None


def test_no_attempts_abstains() -> None:
    assert DeterminismCollector().collect(_ctx(())) is None


def test_votes_are_bounded_and_details_json_serialisable() -> None:
    evidence = DeterminismCollector().collect(_ctx((False, True, True)))
    assert evidence is not None

    assert all(0.0 <= value <= 1.0 for value in evidence.votes.values())
    assert json.dumps(evidence.details)
