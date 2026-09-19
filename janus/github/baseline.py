"""Baseline (last-green) resolution for the fault diff.

To show *what changed* between a passing and failing build, we need a base commit. The
definition (``docs/IMPLEMENTATION_PLAN.md`` §5 Q2) is **the previous successful workflow
run on the same branch**, validated to be an ancestor of the failing head.

GitHub returns runs newest-first, so the first completed ``success`` that is not the
failing head is the most recent green. That candidate is then confirmed with the compare
API: only a base that is ``ahead`` (head ahead of base) or ``identical`` is a true
ancestor. A ``diverged``/``behind`` base means the branch was force-pushed or rebased and
the diff would be meaningless, so we return ``None`` (unknown) rather than guess.

If the compare call itself fails (network, oversized diff), we keep the candidate:
losing the diff is worse than a best-effort base, and the failure is non-fatal.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from janus.github.client import GitHubClient

_GREEN = "success"
_ANCESTOR_STATUSES = frozenset({"ahead", "identical"})


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


def is_ancestor(client: GitHubClient, owner: str, repo: str, base: str, head: str) -> bool:
    """Whether ``base`` is an ancestor of ``head`` per the compare API."""
    data = client.compare_commits(owner, repo, base, head)
    return data.get("status") in _ANCESTOR_STATUSES


def resolve_baseline(
    client: GitHubClient,
    owner: str,
    repo: str,
    *,
    branch: str,
    head_sha: str,
) -> str | None:
    """Pick the last-green baseline on ``branch`` and confirm it is an ancestor of head."""
    runs = client.list_workflow_runs(owner, repo, branch=branch, status="completed")
    candidate = select_baseline_sha(runs, head_sha=head_sha)
    if candidate is None:
        return None
    try:
        if not is_ancestor(client, owner, repo, candidate, head_sha):
            return None
    except httpx.HTTPError:
        return candidate
    return candidate
