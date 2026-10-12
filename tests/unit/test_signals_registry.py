"""The collector registry: order, abstention skipping, and resilience to a raising collector."""

from __future__ import annotations

import json

from janus.core.evidence import ChangeSummary, Reproduction, RunAttempt
from janus.core.signals import collect_all, default_collectors
from janus.core.signals.base import SignalContext, SignalEvidence
from janus.core.triage.routes import SIGNAL_NAMES, Route
from janus.sandbox.profiles import Profile


def _attempt(index: int, passed: bool, *, exit_code: int | None = 1) -> RunAttempt:
    return RunAttempt(
        index=index,
        target_id="tests/test_x.py::test_y",
        passed=passed,
        duration_s=1.0,
        exit_code=exit_code,
        timed_out=False,
        oom_killed=False,
        cpu_seconds=1.0,
        peak_mem_mb=100.0,
        stdout_excerpt="timed out" if not passed else "",
        stderr_excerpt="",
    )


def _empty_ctx() -> SignalContext:
    return SignalContext(
        reproduction=Reproduction(profile=Profile.CLEAN, attempts=(), full_suite=None),
        change=ChangeSummary(base_sha=None, head_sha="abc"),
        failing_targets=("tests/test_x.py::test_y",),
    )


def _rich_ctx() -> SignalContext:
    return SignalContext(
        reproduction=Reproduction(
            profile=Profile.CLEAN,
            attempts=(_attempt(0, False, exit_code=137), _attempt(1, True, exit_code=0)),
            full_suite=None,
        ),
        change=ChangeSummary(base_sha="base", head_sha="head", files_changed=("tests/test_x.py",)),
        failing_targets=("tests/test_x.py::test_y",),
        flake_history={"tests/test_x.py::test_y": 0.6},
        metadata={"cache_hit": False},
    )


def test_default_collectors_are_the_seven_in_order() -> None:
    collectors = default_collectors()

    assert len(collectors) == 7
    assert tuple(collector.name for collector in collectors) == SIGNAL_NAMES


def test_collect_all_skips_abstentions() -> None:
    assert collect_all(default_collectors(), _empty_ctx()) == []


def test_collect_all_returns_valid_evidence() -> None:
    evidence = collect_all(default_collectors(), _rich_ctx())

    assert evidence
    names = {item.name for item in evidence}
    assert names <= set(SIGNAL_NAMES)
    for item in evidence:
        assert all(0.0 <= value <= 1.0 for value in item.votes.values())
        assert Route.UNSURE not in item.votes
        assert json.dumps(item.details)


class _RaisingCollector:
    name = "boom"

    def collect(self, ctx: SignalContext) -> SignalEvidence | None:
        raise RuntimeError("collector exploded")


def test_collect_all_survives_a_raising_collector() -> None:
    evidence = collect_all([*default_collectors(), _RaisingCollector()], _rich_ctx())

    assert evidence
    assert all(item.name != "boom" for item in evidence)
