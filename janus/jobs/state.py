"""Job status vocabulary.

Defined now (S0) so the schema can persist a status; the transitions that move a job
between these states are built with the orchestrator in Phase 2.
"""

from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    """Lifecycle states for a work item.

    S0 only ever creates ``QUEUED`` jobs. The remaining states are declared here so the
    vocabulary is shared with the worker in Phase 2.
    """

    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    COMMENT_ONLY = "comment_only"
