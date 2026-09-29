"""Job state-machine transitions."""

from __future__ import annotations

import pytest

from janus.jobs.state import JobStatus, assert_transition, can_transition, is_terminal


def test_queued_can_only_be_claimed() -> None:
    assert can_transition(JobStatus.QUEUED, JobStatus.CLAIMED)
    assert not can_transition(JobStatus.QUEUED, JobStatus.SUCCEEDED)
    assert not can_transition(JobStatus.QUEUED, JobStatus.RUNNING)


def test_running_may_retry_or_finish() -> None:
    assert can_transition(JobStatus.RUNNING, JobStatus.SUCCEEDED)
    assert can_transition(JobStatus.RUNNING, JobStatus.FAILED)
    assert can_transition(JobStatus.RUNNING, JobStatus.COMMENT_ONLY)
    assert can_transition(JobStatus.RUNNING, JobStatus.QUEUED)


def test_terminal_states_have_no_exits() -> None:
    for status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.COMMENT_ONLY):
        assert is_terminal(status)
        assert not can_transition(status, JobStatus.QUEUED)
    assert not is_terminal(JobStatus.RUNNING)


def test_assert_transition_raises_on_illegal_move() -> None:
    with pytest.raises(ValueError, match="illegal job transition"):
        assert_transition(JobStatus.SUCCEEDED, JobStatus.RUNNING)


def test_assert_transition_allows_legal_move() -> None:
    assert_transition(JobStatus.QUEUED, JobStatus.CLAIMED)
