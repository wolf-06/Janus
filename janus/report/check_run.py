"""Check-run surface.

S0's only claim to the outside world: when a red build is enqueued, post an
``in_progress`` Check run. It proves the webhook → Check path end to end before any
triage logic exists. Later phases replace the summary with real evidence.
"""

from __future__ import annotations

from typing import Any

from janus.github.client import GitHubClient

CHECK_NAME = "Janus / triage"
_DEFAULT_TITLE = "Janus received a red build"
_DEFAULT_SUMMARY = "Enqueued for reproduction and triage. This is an S0 acknowledgement."


def start_check_run(
    client: GitHubClient,
    owner: str,
    repo: str,
    *,
    head_sha: str,
    title: str = _DEFAULT_TITLE,
    summary: str = _DEFAULT_SUMMARY,
) -> dict[str, Any]:
    """Create an ``in_progress`` Check run for ``head_sha``."""
    return client.create_check_run(
        owner,
        repo,
        name=CHECK_NAME,
        head_sha=head_sha,
        status="in_progress",
        output={"title": title, "summary": summary},
    )
