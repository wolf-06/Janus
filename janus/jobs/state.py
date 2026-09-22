"""Job lifecycle: statuses and the transitions allowed between them.

A job's status is the single source of truth for "what is being done with this failure".
Encoding the legal transitions here (rather than in the worker) means an illegal move is
a bug we can detect and test, not a silent corruption of the queue.

Shape of the machine::

    QUEUED ──▶ CLAIMED ──▶ RUNNING ──▶ SUCCEEDED        (terminal)
                  │           │
                  │           ├──▶ FAILED             (terminal, retries exhausted)
                  │           ├──▶ COMMENT_ONLY       (terminal, cap/quota/unsure)
                  │           └──▶ QUEUED             (retry with backoff)
                  └───────────────▶ QUEUED             (release: worker died / backoff)
"""

from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    """Lifecycle states for a work item."""

    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    COMMENT_ONLY = "comment_only"


TERMINAL_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.COMMENT_ONLY}
)

_ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.CLAIMED}),
    JobStatus.CLAIMED: frozenset(
        {
            JobStatus.RUNNING,
            JobStatus.QUEUED,  # released back (crash, or a pre-run backoff)
            JobStatus.FAILED,
            JobStatus.COMMENT_ONLY,
        }
    ),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.QUEUED,  # retry with backoff
            JobStatus.COMMENT_ONLY,
        }
    ),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.COMMENT_ONLY: frozenset(),
}


def is_terminal(status: JobStatus) -> bool:
    """Whether ``status`` ends the job's life (no further transitions)."""
    return status in TERMINAL_STATUSES


def can_transition(source: JobStatus, target: JobStatus) -> bool:
    """Whether moving ``source`` → ``target`` is legal."""
    return target in _ALLOWED_TRANSITIONS[source]


def assert_transition(source: JobStatus, target: JobStatus) -> None:
    """Raise ``ValueError`` if ``source`` → ``target`` is not a legal transition."""
    if not can_transition(source, target):
        raise ValueError(f"illegal job transition {source.value} -> {target.value}")
