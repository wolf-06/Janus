"""REST client for the GitHub API, authenticated with an installation token.

The client is deliberately thin: one method per endpoint S0 needs, each returning parsed
JSON (or raw log bytes). HTTP is injectable so tests can drive it with ``respx`` or a
mock transport without a network. The token is supplied by the caller (see
``app_auth.installation_token``) and is never embedded in a URL.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, cast

import httpx

from janus.github.app_auth import API_VERSION, GITHUB_API_BASE


def _as_dict(response: httpx.Response) -> dict[str, Any]:
    return cast("dict[str, Any]", response.json())


def _as_list(response: httpx.Response) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", response.json())


class GitHubClient:
    """Authenticated GitHub REST client."""

    def __init__(
        self,
        token: str,
        *,
        client: httpx.Client | None = None,
        api_base: str = GITHUB_API_BASE,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=30.0, follow_redirects=True)
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self._client.request(
            method, f"{self._api_base}{path}", headers=self._headers, **kwargs
        )
        response.raise_for_status()
        return response

    def get_workflow_run(self, owner: str, repo: str, run_id: int) -> dict[str, Any]:
        return _as_dict(self._request("GET", f"/repos/{owner}/{repo}/actions/runs/{run_id}"))

    def download_run_logs(self, owner: str, repo: str, run_id: int) -> bytes:
        """Download the raw log archive for a workflow run.

        GitHub answers with a 302 to a short-lived signed URL; the client follows
        redirects by default.
        """
        return self._request("GET", f"/repos/{owner}/{repo}/actions/runs/{run_id}/logs").content

    def list_workflow_runs(
        self,
        owner: str,
        repo: str,
        *,
        branch: str | None = None,
        status: str | None = None,
        per_page: int = 20,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"per_page": per_page}
        if branch is not None:
            params["branch"] = branch
        if status is not None:
            params["status"] = status
        data = _as_dict(self._request("GET", f"/repos/{owner}/{repo}/actions/runs", params=params))
        return cast("list[dict[str, Any]]", data.get("workflow_runs", []))

    def list_commits(
        self, owner: str, repo: str, *, sha: str | None = None, per_page: int = 100
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"per_page": per_page}
        if sha is not None:
            params["sha"] = sha
        return _as_list(self._request("GET", f"/repos/{owner}/{repo}/commits", params=params))

    def compare_commits(self, owner: str, repo: str, base: str, head: str) -> dict[str, Any]:
        return _as_dict(self._request("GET", f"/repos/{owner}/{repo}/compare/{base}...{head}"))

    def create_check_run(
        self,
        owner: str,
        repo: str,
        *,
        name: str,
        head_sha: str,
        status: str = "in_progress",
        conclusion: str | None = None,
        details_url: str | None = None,
        output: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": name, "head_sha": head_sha, "status": status}
        if conclusion is not None:
            payload["conclusion"] = conclusion
        if details_url is not None:
            payload["details_url"] = details_url
        if output is not None:
            payload["output"] = output
        return _as_dict(self._request("POST", f"/repos/{owner}/{repo}/check-runs", json=payload))

    def create_pull_request(
        self,
        owner: str,
        repo: str,
        *,
        title: str,
        head: str,
        base: str,
        body: str,
        draft: bool = True,
    ) -> dict[str, Any]:
        """Open a pull request; drafts are the default and merging is not supported."""
        payload: dict[str, Any] = {
            "title": title,
            "head": head,
            "base": base,
            "body": body,
            "draft": draft,
        }
        return _as_dict(self._request("POST", f"/repos/{owner}/{repo}/pulls", json=payload))

    def create_issue_comment(
        self, owner: str, repo: str, issue_number: int, body: str
    ) -> dict[str, Any]:
        return _as_dict(
            self._request(
                "POST",
                f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
                json={"body": body},
            )
        )
