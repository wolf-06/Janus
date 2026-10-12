"""The signatures collector: infra signatures → ENV, assertion failures → CODE_BUG."""

from __future__ import annotations

import json

from janus.core.evidence import ChangeSummary, Reproduction, RunAttempt
from janus.core.signals.base import SignalContext
from janus.core.signals.signatures import SignaturesCollector
from janus.core.triage.routes import Route
from janus.sandbox.profiles import Profile


def _attempt(index: int, *, stdout: str = "", stderr: str = "") -> RunAttempt:
    return RunAttempt(
        index=index,
        target_id="tests/test_x.py::test_y",
        passed=False,
        duration_s=1.0,
        exit_code=1,
        timed_out=False,
        oom_killed=False,
        cpu_seconds=1.0,
        peak_mem_mb=100.0,
        stdout_excerpt=stdout,
        stderr_excerpt=stderr,
    )


def _ctx(*attempts: RunAttempt) -> SignalContext:
    return SignalContext(
        reproduction=Reproduction(profile=Profile.CLEAN, attempts=attempts, full_suite=None),
        change=ChangeSummary(base_sha=None, head_sha="abc"),
        failing_targets=("tests/test_x.py::test_y",),
    )


def test_connection_reset_votes_env() -> None:
    evidence = SignaturesCollector().collect(
        _ctx(_attempt(0, stderr="ConnectionResetError: ECONNRESET"))
    )

    assert evidence is not None
    assert evidence.votes[Route.ENV] >= 0.8
    assert evidence.details["infra_matches"] == {"connection": 1}


def test_timeout_votes_env() -> None:
    evidence = SignaturesCollector().collect(_ctx(_attempt(0, stderr="command timed out")))

    assert evidence is not None
    assert evidence.votes[Route.ENV] >= 0.8
    assert "timeout" in evidence.details["infra_matches"]


def test_assertion_votes_code_bug() -> None:
    evidence = SignaturesCollector().collect(_ctx(_attempt(0, stderr="AssertionError: 1 != 2")))

    assert evidence is not None
    assert evidence.votes[Route.CODE_BUG] >= 0.5
    assert evidence.details["assertion_attempts"] == 1


def test_infra_and_assertion_both_vote() -> None:
    evidence = SignaturesCollector().collect(
        _ctx(_attempt(0, stdout="OOMKilled", stderr="AssertionError: boom"))
    )

    assert evidence is not None
    assert Route.ENV in evidence.votes
    assert Route.CODE_BUG in evidence.votes


def test_clean_output_abstains() -> None:
    assert SignaturesCollector().collect(_ctx(_attempt(0, stdout="all good"))) is None


def test_no_attempts_abstains() -> None:
    assert SignaturesCollector().collect(_ctx()) is None


def test_votes_bounded_and_details_json_serialisable() -> None:
    evidence = SignaturesCollector().collect(_ctx(_attempt(0, stderr="503 Service Unavailable")))
    assert evidence is not None

    assert all(0.0 <= value <= 1.0 for value in evidence.votes.values())
    assert Route.UNSURE not in evidence.votes
    assert json.dumps(evidence.details)
