"""Last-green → first-red baseline selection."""

from __future__ import annotations

import httpx
import respx

from janus.github.app_auth import GITHUB_API_BASE
from janus.github.baseline import resolve_baseline, select_baseline_sha
from janus.github.client import GitHubClient


def test_selects_most_recent_success_before_head() -> None:
    runs = [
        {"head_sha": "red", "conclusion": "failure"},
        {"head_sha": "green1", "conclusion": "success"},
        {"head_sha": "green0", "conclusion": "success"},
    ]
    assert select_baseline_sha(runs, head_sha="red") == "green1"


def test_skips_non_success_conclusions() -> None:
    runs = [
        {"head_sha": "red", "conclusion": "failure"},
        {"head_sha": "cancelled", "conclusion": "cancelled"},
        {"head_sha": "green", "conclusion": "success"},
    ]
    assert select_baseline_sha(runs, head_sha="red") == "green"


def test_returns_none_when_no_earlier_green() -> None:
    runs = [{"head_sha": "red", "conclusion": "failure"}]
    assert select_baseline_sha(runs, head_sha="red") is None


def test_ignores_head_even_if_it_looks_green() -> None:
    runs = [{"head_sha": "red", "conclusion": "success"}]
    assert select_baseline_sha(runs, head_sha="red") is None


def _mock_runs(conclusion: str = "success") -> None:
    respx.get(f"{GITHUB_API_BASE}/repos/acme/widgets/actions/runs").mock(
        return_value=httpx.Response(
            200,
            json={
                "workflow_runs": [
                    {"head_sha": "red", "conclusion": "failure"},
                    {"head_sha": "green", "conclusion": conclusion},
                ]
            },
        )
    )


@respx.mock
def test_resolve_baseline_confirms_ancestor_and_queries_branch_runs() -> None:
    route = respx.get(f"{GITHUB_API_BASE}/repos/acme/widgets/actions/runs").mock(
        return_value=httpx.Response(
            200,
            json={
                "workflow_runs": [
                    {"head_sha": "red", "conclusion": "failure"},
                    {"head_sha": "green", "conclusion": "success"},
                ]
            },
        )
    )
    compare = respx.get(f"{GITHUB_API_BASE}/repos/acme/widgets/compare/green...red").mock(
        return_value=httpx.Response(200, json={"status": "ahead"})
    )

    with GitHubClient("ghs_token") as client:
        baseline = resolve_baseline(client, "acme", "widgets", branch="main", head_sha="red")

    assert baseline == "green"
    assert compare.call_count == 1
    request = route.calls.last.request
    assert request.url.params["branch"] == "main"
    assert request.url.params["status"] == "completed"


@respx.mock
def test_resolve_baseline_rejects_diverged_baseline() -> None:
    _mock_runs()
    respx.get(f"{GITHUB_API_BASE}/repos/acme/widgets/compare/green...red").mock(
        return_value=httpx.Response(200, json={"status": "diverged"})
    )

    with GitHubClient("ghs_token") as client:
        baseline = resolve_baseline(client, "acme", "widgets", branch="main", head_sha="red")

    assert baseline is None


@respx.mock
def test_resolve_baseline_keeps_candidate_when_compare_fails() -> None:
    _mock_runs()
    respx.get(f"{GITHUB_API_BASE}/repos/acme/widgets/compare/green...red").mock(
        return_value=httpx.Response(500)
    )

    with GitHubClient("ghs_token") as client:
        baseline = resolve_baseline(client, "acme", "widgets", branch="main", head_sha="red")

    assert baseline == "green"
