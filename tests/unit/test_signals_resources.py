"""The resources collector: OOM / memory-cap / stall → ENV, otherwise weak CODE_BUG."""

from __future__ import annotations

import json
from typing import Any

from janus.core.evidence import ChangeSummary, Reproduction, RunAttempt
from janus.core.signals.base import SignalContext
from janus.core.signals.resources import ResourcesCollector
from janus.core.triage.routes import Route
from janus.sandbox.profiles import Profile


def _attempt(
    index: int,
    *,
    duration_s: float = 1.0,
    peak_mem_mb: float = 100.0,
    oom_killed: bool = False,
) -> RunAttempt:
    return RunAttempt(
        index=index,
        target_id="tests/test_x.py::test_y",
        passed=False,
        duration_s=duration_s,
        exit_code=1,
        timed_out=False,
        oom_killed=oom_killed,
        cpu_seconds=1.0,
        peak_mem_mb=peak_mem_mb,
        stdout_excerpt="",
        stderr_excerpt="",
    )


def _ctx(*attempts: RunAttempt, metadata: dict[str, Any] | None = None) -> SignalContext:
    return SignalContext(
        reproduction=Reproduction(profile=Profile.CLEAN, attempts=attempts, full_suite=None),
        change=ChangeSummary(base_sha=None, head_sha="abc"),
        failing_targets=("tests/test_x.py::test_y",),
        metadata=metadata or {},
    )


def test_oom_killed_votes_env() -> None:
    evidence = ResourcesCollector().collect(_ctx(_attempt(0, oom_killed=True)))

    assert evidence is not None
    assert evidence.votes[Route.ENV] >= 0.8
    assert evidence.details["oom_killed"] is True


def test_exactly_at_memory_cap_votes_env() -> None:
    evidence = ResourcesCollector().collect(
        _ctx(_attempt(0, peak_mem_mb=2048.0), metadata={"memory_mb": 2048})
    )

    assert evidence is not None
    assert Route.ENV in evidence.votes
    assert evidence.details["near_memory_cap"] is True


def test_below_memory_cap_without_other_signal_votes_weak_code_bug() -> None:
    evidence = ResourcesCollector().collect(
        _ctx(_attempt(0, peak_mem_mb=1800.0), metadata={"memory_mb": 2048})
    )

    assert evidence is not None
    assert Route.CODE_BUG in evidence.votes
    assert evidence.votes[Route.CODE_BUG] <= 0.5


def test_memory_comparison_skipped_without_cap() -> None:
    evidence = ResourcesCollector().collect(_ctx(_attempt(0, peak_mem_mb=99999.0)))

    assert evidence is not None
    assert evidence.details["memory_cap_mb"] is None
    assert Route.CODE_BUG in evidence.votes


def test_duration_order_of_magnitude_votes_weak_env() -> None:
    evidence = ResourcesCollector().collect(
        _ctx(_attempt(0, duration_s=1.0), _attempt(1, duration_s=10.0))
    )

    assert evidence is not None
    assert Route.ENV in evidence.votes
    assert evidence.details["stalled"] is True


def test_no_attempts_abstains() -> None:
    assert ResourcesCollector().collect(_ctx()) is None


def test_votes_bounded_and_details_json_serialisable() -> None:
    evidence = ResourcesCollector().collect(_ctx(_attempt(0, oom_killed=True)))
    assert evidence is not None

    assert all(0.0 <= value <= 1.0 for value in evidence.votes.values())
    assert Route.UNSURE not in evidence.votes
    assert json.dumps(evidence.details)
