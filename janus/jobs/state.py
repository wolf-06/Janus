"""Job lifecycle: statuses and the transitions allowed between them.

A job's state is the single source of truth for "what is being done with this failure".
Encoding the legal transitions here means an illegal move is a bug we can detect and
test, not silent queue corruption.

Shape of the machine::

    QUEUED ──▶ RUNNING ──▶ SUCCEEDED
                  │
                  ├──▶ FAILED            (terminal, unclassified failure)
                  ├──▶ DEAD              (terminal, permanent or retries exhausted)
                  ├──▶ SKIPPED_BUDGET    (terminal, over a cost budget)
                  └──▶ QUEUED            (retry with backoff, or lease reaped)

There is no separate ``claimed`` state: a claim moves ``queued`` → ``running`` and sets a
lease in the same statement. ``comment_only`` is not a state — whether a run is
comment-only is behaviour driven by the triage route, not by the queue.
"""

from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    """Lifecycle states for a work item (backend spec §4.2)."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"
    SKIPPED_BUDGET = "skipped_budget"


TERMINAL_STATUSES: frozenset[JobStatus] = frozenset(
    {
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.DEAD,
        JobStatus.SKIPPED_BUDGET,
    }
)

# States in which a job is still "active" — used by the dedup partial unique index.
ACTIVE_STATUSES: frozenset[JobStatus] = frozenset({JobStatus.QUEUED, JobStatus.RUNNING})

_ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING}),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.DEAD,
            JobStatus.SKIPPED_BUDGET,
            JobStatus.QUEUED,  # retry with backoff, or lease reaped after a crash
        }
    ),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.DEAD: frozenset(),
    JobStatus.SKIPPED_BUDGET: frozenset(),
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
