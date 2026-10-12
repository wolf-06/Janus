"""Derived helpers on the evidence types, including the empty-attempts edge case."""

from __future__ import annotations

from janus.core.evidence import ChangeSummary, CommitSummary, Reproduction, RunAttempt
from janus.sandbox.profiles import Profile


def attempt(
    index: int,
    *,
    passed: bool,
    duration_s: float = 1.0,
    target_id: str | None = "tests/test_x.py::test_a",
) -> RunAttempt:
    return RunAttempt(
        index=index,
        target_id=target_id,
        passed=passed,
        duration_s=duration_s,
        exit_code=0 if passed else 1,
        timed_out=False,
        oom_killed=False,
        cpu_seconds=0.5,
        peak_mem_mb=64.0,
        stdout_excerpt="",
        stderr_excerpt="",
    )


def reproduction(*attempts: RunAttempt, full_suite: RunAttempt | None = None) -> Reproduction:
    return Reproduction(
        profile=Profile.CLEAN,
        attempts=attempts,
        full_suite=full_suite,
    )


def test_failures_and_successes_count_attempts() -> None:
    rep = reproduction(
        attempt(0, passed=False),
        attempt(1, passed=True),
        attempt(2, passed=False),
    )

    assert rep.failures == 2
    assert rep.successes == 1
    assert rep.failure_rate() == 2 / 3


def test_always_fails_only_when_all_attempts_fail() -> None:
    assert reproduction(attempt(0, passed=False), attempt(1, passed=False)).always_fails()
    assert not reproduction(attempt(0, passed=False), attempt(1, passed=True)).always_fails()
    assert not reproduction(attempt(0, passed=True)).always_fails()


def test_intermittent_when_mixed() -> None:
    assert reproduction(attempt(0, passed=True), attempt(1, passed=False)).intermittent()
    assert not reproduction(attempt(0, passed=False), attempt(1, passed=False)).intermittent()
    assert not reproduction(attempt(0, passed=True), attempt(1, passed=True)).intermittent()


def test_passes_always_only_when_all_attempts_pass() -> None:
    assert reproduction(attempt(0, passed=True), attempt(1, passed=True)).passes_always()
    assert not reproduction(attempt(0, passed=True), attempt(1, passed=False)).passes_always()
    assert not reproduction(attempt(0, passed=False)).passes_always()


def test_empty_attempts_is_neither_always_fails_nor_passes_always() -> None:
    rep = reproduction()

    assert rep.failures == 0
    assert rep.successes == 0
    assert rep.failure_rate() == 0.0
    assert rep.always_fails() is False
    assert rep.passes_always() is False
    assert rep.intermittent() is False


def test_total_duration_includes_attempts_and_full_suite() -> None:
    rep = reproduction(
        attempt(0, passed=False, duration_s=2.0),
        attempt(1, passed=False, duration_s=3.0),
        full_suite=attempt(0, passed=True, duration_s=4.0, target_id=None),
    )

    assert rep.total_duration_s == 9.0


def test_total_duration_without_full_suite() -> None:
    assert reproduction(attempt(0, passed=False, duration_s=2.5)).total_duration_s == 2.5
    assert reproduction().total_duration_s == 0.0


def test_reproduction_defaults() -> None:
    rep = Reproduction(
        profile=Profile.AMBIENT,
        attempts=(),
        full_suite=None,
    )

    assert rep.ambient_reconstructed is True
    assert rep.cost_minutes == 0.0


def test_change_summary_available_requires_a_baseline() -> None:
    assert ChangeSummary(base_sha="abc", head_sha="def").available is True
    assert ChangeSummary(base_sha=None, head_sha="def").available is False


def test_change_summary_defaults_are_empty() -> None:
    change = ChangeSummary(base_sha="abc", head_sha="def")

    assert change.commits == ()
    assert change.files_changed == ()
    assert change.diff_excerpt == ""


def test_commit_summary_holds_its_fields() -> None:
    commit = CommitSummary(sha="abc", message="fix: thing", files=("a.py", "b.py"))

    assert commit.sha == "abc"
    assert commit.message == "fix: thing"
    assert commit.files == ("a.py", "b.py")
