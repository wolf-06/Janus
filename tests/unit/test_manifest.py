"""Manifest construction, code exchange and credential persistence."""

from __future__ import annotations

from pathlib import Path

import httpx
import respx

from janus.github.app_auth import GITHUB_API_BASE
from janus.github.manifest import (
    build_manifest,
    exchange_manifest_code,
    persist_credentials,
)

_CREDENTIALS = {
    "id": 123,
    "pem": "-----BEGIN RSA PRIVATE KEY-----\nFAKE\n-----END RSA PRIVATE KEY-----\n",
    "webhook_secret": "whsec",
    "client_id": "Iv1.abc",
    "client_secret": "shhh",
}


def test_build_manifest_has_least_privilege_and_redirect() -> None:
    manifest = build_manifest(
        name="Janus",
        redirect_url="https://example.test/manifest/callback",
        homepage_url="https://example.test",
        webhook_url="https://example.test/webhooks/github",
    )

    assert manifest["redirect_url"] == "https://example.test/manifest/callback"
    assert manifest["hook_attributes"]["url"] == "https://example.test/webhooks/github"
    assert manifest["default_permissions"] == {
        "contents": "write",
        "pull_requests": "write",
        "checks": "write",
        "actions": "read",
        "metadata": "read",
    }
    assert set(manifest["default_events"]) == {"workflow_run", "check_run"}
    assert manifest["public"] is False


@respx.mock
def test_exchange_manifest_code_returns_credentials() -> None:
    route = respx.post(f"{GITHUB_API_BASE}/app-manifests/abc/conversions").mock(
        return_value=httpx.Response(201, json=_CREDENTIALS)
    )

    result = exchange_manifest_code("abc")

    assert result["id"] == 123
    assert result["webhook_secret"] == "whsec"
    assert route.call_count == 1


def test_persist_credentials_writes_key_and_env(tmp_path: Path) -> None:
    key_path = tmp_path / "keys" / "app.pem"
    env_path = tmp_path / ".env"
    env_path.write_text("# existing\nJANUS_LOG_LEVEL=DEBUG\n", encoding="utf-8")

    returned = persist_credentials(_CREDENTIALS, private_key_path=key_path, env_path=env_path)

    assert returned == key_path
    assert key_path.read_text(encoding="utf-8") == _CREDENTIALS["pem"]

    env = env_path.read_text(encoding="utf-8")
    assert "JANUS_LOG_LEVEL=DEBUG" in env
    assert "JANUS_GITHUB_APP_ID=123" in env
    assert "JANUS_GITHUB_WEBHOOK_SECRET=whsec" in env
    assert "JANUS_GITHUB_CLIENT_ID=Iv1.abc" in env
    assert "JANUS_GITHUB_CLIENT_SECRET=shhh" in env
    assert f"JANUS_GITHUB_PRIVATE_KEY_PATH={key_path}" in env


def test_persist_credentials_updates_existing_keys(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("JANUS_GITHUB_APP_ID=old\n", encoding="utf-8")

    persist_credentials(_CREDENTIALS, private_key_path=tmp_path / "app.pem", env_path=env_path)

    env = env_path.read_text(encoding="utf-8")
    assert "JANUS_GITHUB_APP_ID=123" in env
    assert "JANUS_GITHUB_APP_ID=old" not in env
