"""Baseline (last-green) resolution for the fault diff.

To show *what changed* between a passing and failing build, we need a base commit. The
provisional definition (``docs/IMPLEMENTATION_PLAN.md`` §5 Q2) is **the previous
successful workflow run on the same branch**. GitHub returns runs newest-first, so the
first completed ``success`` that is not the failing head is the most recent green.

Caveat: this does not verify that the green run is an ancestor of the failing head. A
force-push or a rebase could make "most recent success on the branch" diverge from the
true last-green ancestor. The merge-base-with-default-branch alternative is deferred.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from janus.github.client import GitHubClient

_GREEN = "success"


def select_baseline_sha(runs: Sequence[Mapping[str, Any]], *, head_sha: str) -> str | None:
    """Return the head SHA of the most recent successful run that is not ``head_sha``.

    ``runs`` must be ordered newest-first (GitHub's default). Returns ``None`` when no
    earlier green run exists, in which case callers should treat the diff as unknown.
    """
    for run in runs:
        if run.get("head_sha") == head_sha:
            continue
        if run.get("conclusion") == _GREEN:
            sha = run.get("head_sha")
            if sha is not None:
                return str(sha)
    return None


def resolve_baseline(
    client: GitHubClient,
    owner: str,
    repo: str,
    *,
    branch: str,
    head_sha: str,
) -> str | None:
    """Fetch recent completed runs on ``branch`` and pick the last-green baseline."""
    runs = client.list_workflow_runs(owner, repo, branch=branch, status="completed")
    return select_baseline_sha(runs, head_sha=head_sha)
