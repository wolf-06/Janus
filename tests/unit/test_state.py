"""Job state-machine transitions."""

from __future__ import annotations

import pytest

from janus.jobs.state import JobStatus, assert_transition, can_transition, is_terminal


def test_queued_can_only_be_claimed() -> None:
    assert can_transition(JobStatus.QUEUED, JobStatus.RUNNING)
    assert not can_transition(JobStatus.QUEUED, JobStatus.SUCCEEDED)
    assert not can_transition(JobStatus.QUEUED, JobStatus.DEAD)


def test_running_may_retry_or_terminate() -> None:
    for target in (
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.DEAD,
        JobStatus.SKIPPED_BUDGET,
        JobStatus.QUEUED,
    ):
        assert can_transition(JobStatus.RUNNING, target)


def test_terminal_states_have_no_exits() -> None:
    for status in (
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.DEAD,
        JobStatus.SKIPPED_BUDGET,
    ):
        assert is_terminal(status)
        assert not can_transition(status, JobStatus.QUEUED)
    assert not is_terminal(JobStatus.RUNNING)


def test_assert_transition_raises_on_illegal_move() -> None:
    with pytest.raises(ValueError, match="illegal job transition"):
        assert_transition(JobStatus.DEAD, JobStatus.RUNNING)


def test_assert_transition_allows_legal_move() -> None:
    assert_transition(JobStatus.QUEUED, JobStatus.RUNNING)
