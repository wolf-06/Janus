"""Manifest construction, code exchange and credential persistence."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from janus.app.main import create_app
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


def test_manifest_start_sets_state_cookie() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/manifest/start")

    assert response.status_code == 200
    assert "janus_manifest_state" in response.cookies
    assert "manifest" in response.text


def test_callback_rejects_missing_state() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/manifest/callback", params={"code": "abc"})

    assert response.status_code == 400


def test_callback_rejects_state_mismatch() -> None:
    with TestClient(create_app()) as client:
        client.cookies.set("janus_manifest_state", "expected")
        response = client.get("/manifest/callback", params={"code": "abc", "state": "other"})

    assert response.status_code == 400


def test_callback_with_valid_state_persists_and_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "janus.app.api.manifest.exchange_manifest_code",
        lambda code: {"id": 1, "pem": "x"},
    )

    def fake_persist(
        credentials: dict[str, object], *, private_key_path: object, env_path: object
    ) -> None:
        captured["credentials"] = credentials

    monkeypatch.setattr("janus.app.api.manifest.persist_credentials", fake_persist)

    with TestClient(create_app()) as client:
        client.cookies.set("janus_manifest_state", "nonce")
        response = client.get(
            "/manifest/callback",
            params={"code": "abc", "state": "nonce"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/manifest/success"
    assert captured["credentials"] == {"id": 1, "pem": "x"}
