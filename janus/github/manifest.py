"""GitHub App manifest creation and credential persistence.

The manifest flow lets a user create a fully configured App in one click:

1. ``build_manifest`` produces the App definition (permissions, events, redirect URL).
2. The user's browser POSTs it to GitHub, which creates the App and redirects back with
   a temporary ``code``.
3. ``exchange_manifest_code`` trades that code for the App's id, private key (PEM),
   webhook secret and OAuth client credentials.
4. ``persist_credentials`` writes the PEM to disk and records the non-secret values in
   ``.env`` (S0 choice; see docs/DECISIONS.md).

Permissions are the least-privilege set from ``PLAN.md`` §4.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import httpx

from janus.github.app_auth import API_VERSION, GITHUB_API_BASE

DEFAULT_PERMISSIONS: dict[str, str] = {
    "contents": "write",
    "pull_requests": "write",
    "checks": "write",
    "actions": "read",
    "metadata": "read",
}
DEFAULT_EVENTS: list[str] = ["workflow_run", "check_run"]

# Maps the field names in GitHub's manifest-conversion response to env keys.
_ENV_KEYS = {
    "id": "JANUS_GITHUB_APP_ID",
    "webhook_secret": "JANUS_GITHUB_WEBHOOK_SECRET",
    "client_id": "JANUS_GITHUB_CLIENT_ID",
    "client_secret": "JANUS_GITHUB_CLIENT_SECRET",
}


def build_manifest(
    *,
    name: str,
    redirect_url: str,
    homepage_url: str,
    webhook_url: str | None = None,
    description: str = "When CI goes red, Janus triages the failure.",
) -> dict[str, Any]:
    """Return the App manifest GitHub expects."""
    return {
        "name": name,
        "url": homepage_url,
        "hook_attributes": {"url": webhook_url or homepage_url, "active": True},
        "redirect_url": redirect_url,
        "description": description,
        "public": False,
        "default_permissions": dict(DEFAULT_PERMISSIONS),
        "default_events": list(DEFAULT_EVENTS),
    }


def exchange_manifest_code(
    code: str,
    *,
    client: httpx.Client | None = None,
    api_base: str = GITHUB_API_BASE,
) -> dict[str, Any]:
    """Trade a manifest ``code`` for the created App's credentials."""
    owns_client = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        response = http.post(
            f"{api_base.rstrip('/')}/app-manifests/{code}/conversions",
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        response.raise_for_status()
        return cast("dict[str, Any]", response.json())
    finally:
        if owns_client:
            http.close()


def _update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Set ``key=value`` pairs in ``path``, preserving comments and other keys."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            result.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            result.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            result.append(line)
    for key, value in updates.items():
        if key not in seen:
            result.append(f"{key}={value}")
    path.write_text("\n".join(result) + "\n", encoding="utf-8")


def persist_credentials(
    credentials: dict[str, Any],
    *,
    private_key_path: str | Path,
    env_path: str | Path,
) -> Path:
    """Write the App PEM to disk and the non-secret credentials to ``.env``.

    Returns the path the private key was written to.
    """
    key_path = Path(private_key_path)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(str(credentials["pem"]), encoding="utf-8")

    updates = {
        env_key: str(credentials[field])
        for field, env_key in _ENV_KEYS.items()
        if credentials.get(field) is not None
    }
    updates["JANUS_GITHUB_PRIVATE_KEY_PATH"] = str(key_path)
    _update_env_file(Path(env_path), updates)
    return key_path
