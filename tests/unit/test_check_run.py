"""The GitHub client posts the expected Check-run payload."""

from __future__ import annotations

import json

import httpx
import respx

from janus.github.app_auth import GITHUB_API_BASE
from janus.github.client import GitHubClient


@respx.mock
def test_create_check_run_posts_expected_payload() -> None:
    route = respx.post(f"{GITHUB_API_BASE}/repos/acme/widgets/check-runs").mock(
        return_value=httpx.Response(201, json={"id": 1, "status": "in_progress"})
    )

    with GitHubClient("ghs_token") as client:
        result = client.create_check_run("acme", "widgets", name="Janus", head_sha="abc123")

    assert result["id"] == 1
    request = route.calls.last.request
    assert json.loads(request.content) == {
        "name": "Janus",
        "head_sha": "abc123",
        "status": "in_progress",
    }
    assert request.headers["Authorization"] == "Bearer ghs_token"
    assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"


@respx.mock
def test_create_check_run_includes_conclusion_and_output() -> None:
    route = respx.post(f"{GITHUB_API_BASE}/repos/acme/widgets/check-runs").mock(
        return_value=httpx.Response(201, json={"id": 2})
    )

    with GitHubClient("ghs_token") as client:
        client.create_check_run(
            "acme",
            "widgets",
            name="Janus",
            head_sha="abc123",
            status="completed",
            conclusion="success",
            output={"title": "Triage", "summary": "CODE_BUG"},
        )

    body = json.loads(route.calls.last.request.content)
    assert body["conclusion"] == "success"
    assert body["output"]["summary"] == "CODE_BUG"
