"""The GitHub client opens pull requests as drafts by default, and never merges."""

from __future__ import annotations

import json

import httpx
import respx

from janus.github.app_auth import GITHUB_API_BASE
from janus.github.client import GitHubClient


@respx.mock
def test_create_pull_request_posts_expected_payload() -> None:
    route = respx.post(f"{GITHUB_API_BASE}/repos/acme/widgets/pulls").mock(
        return_value=httpx.Response(201, json={"number": 7, "draft": True})
    )

    with GitHubClient("ghs_token") as client:
        result = client.create_pull_request(
            "acme",
            "widgets",
            title="Fix the parser",
            head="janus/fix-parser",
            base="main",
            body="Diagnosis and verification table.",
        )

    assert result == {"number": 7, "draft": True}
    request = route.calls.last.request
    assert request.method == "POST"
    assert str(request.url) == f"{GITHUB_API_BASE}/repos/acme/widgets/pulls"
    assert json.loads(request.content) == {
        "title": "Fix the parser",
        "head": "janus/fix-parser",
        "base": "main",
        "body": "Diagnosis and verification table.",
        "draft": True,
    }
    assert request.headers["Authorization"] == "Bearer ghs_token"


@respx.mock
def test_create_pull_request_allows_explicit_non_draft() -> None:
    route = respx.post(f"{GITHUB_API_BASE}/repos/acme/widgets/pulls").mock(
        return_value=httpx.Response(201, json={"number": 8, "draft": False})
    )

    with GitHubClient("ghs_token") as client:
        result = client.create_pull_request(
            "acme",
            "widgets",
            title="Fix",
            head="janus/fix",
            base="main",
            body="body",
            draft=False,
        )

    assert result["draft"] is False
    assert json.loads(route.calls.last.request.content)["draft"] is False
