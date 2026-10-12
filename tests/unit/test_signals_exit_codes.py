"""The exit_codes collector: 137/124 → ENV, 1/2 → CODE_BUG, None → ignored/abstain."""

from __future__ import annotations

import json

from janus.core.evidence import ChangeSummary, Reproduction, RunAttempt
from janus.core.signals.base import SignalContext
from janus.core.signals.exit_codes import ExitCodesCollector
from janus.core.triage.routes import Route
from janus.sandbox.profiles import Profile


def _attempt(index: int, exit_code: int | None) -> RunAttempt:
    return RunAttempt(
        index=index,
        target_id="tests/test_x.py::test_y",
        passed=False,
        duration_s=1.0,
        exit_code=exit_code,
        timed_out=False,
        oom_killed=False,
        cpu_seconds=1.0,
        peak_mem_mb=100.0,
        stdout_excerpt="",
        stderr_excerpt="",
    )


def _ctx(*codes: int | None) -> SignalContext:
    return SignalContext(
        reproduction=Reproduction(
            profile=Profile.CLEAN,
            attempts=tuple(_attempt(i, code) for i, code in enumerate(codes)),
            full_suite=None,
        ),
        change=ChangeSummary(base_sha=None, head_sha="abc"),
        failing_targets=("tests/test_x.py::test_y",),
    )


def test_137_votes_env() -> None:
    evidence = ExitCodesCollector().collect(_ctx(137))

    assert evidence is not None
    assert evidence.votes[Route.ENV] >= 0.7
    assert evidence.details["env_hits"] == 1


def test_124_votes_env() -> None:
    evidence = ExitCodesCollector().collect(_ctx(124))

    assert evidence is not None
    assert evidence.votes[Route.ENV] >= 0.7


def test_exit_code_one_votes_code_bug() -> None:
    evidence = ExitCodesCollector().collect(_ctx(1))

    assert evidence is not None
    assert evidence.votes[Route.CODE_BUG] >= 0.5
    assert evidence.details["code_bug_hits"] == 1


def test_exit_code_two_votes_code_bug() -> None:
    evidence = ExitCodesCollector().collect(_ctx(2))

    assert evidence is not None
    assert Route.CODE_BUG in evidence.votes


def test_none_exit_code_abstains() -> None:
    assert ExitCodesCollector().collect(_ctx(None, None)) is None


def test_mixed_codes_vote_both_routes() -> None:
    evidence = ExitCodesCollector().collect(_ctx(137, 1))

    assert evidence is not None
    assert Route.ENV in evidence.votes
    assert Route.CODE_BUG in evidence.votes
    assert evidence.details["exit_codes"] == {"1": 1, "137": 1}


def test_votes_bounded_and_details_json_serialisable() -> None:
    evidence = ExitCodesCollector().collect(_ctx(137, 124, 1))
    assert evidence is not None

    assert all(0.0 <= value <= 1.0 for value in evidence.votes.values())
    assert json.dumps(evidence.details)
