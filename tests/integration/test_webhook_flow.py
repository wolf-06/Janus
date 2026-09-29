"""Signed webhook → run/job persisted → Check run posted.

GitHub's HTTP is mocked with respx; everything else (signature verification, the
FastAPI endpoint, the repository layer, SQLite persistence) is real.
"""

from __future__ import annotations

import json
from hashlib import sha256
from hmac import new as hmac_new
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from janus.app.main import create_app
from janus.db.models import Job, Run
from janus.db.session import get_session
from janus.github.app_auth import GITHUB_API_BASE

SECRET = "webhook-secret"


@pytest.fixture
def private_key_path(tmp_path: Path) -> Path:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "app.pem"
    path.write_bytes(pem)
    return path


def _sign(body: bytes) -> str:
    return "sha256=" + hmac_new(SECRET.encode(), body, sha256).hexdigest()


def _payload() -> dict[str, Any]:
    return {
        "action": "completed",
        "installation": {"id": 99, "app_id": 123},
        "repository": {
            "id": 555,
            "name": "widgets",
            "owner": {"login": "acme"},
            "default_branch": "main",
        },
        "workflow_run": {
            "id": 777,
            "head_sha": "redsha",
            "head_branch": "main",
            "conclusion": "failure",
            "status": "completed",
            "event": "push",
        },
    }


def _mock_github() -> respx.Route:
    respx.post(f"{GITHUB_API_BASE}/app/installations/99/access_tokens").mock(
        return_value=httpx.Response(
            200, json={"token": "ghs_x", "expires_at": "2999-01-01T00:00:00Z"}
        )
    )
    respx.get(f"{GITHUB_API_BASE}/repos/acme/widgets/actions/runs").mock(
        return_value=httpx.Response(
            200,
            json={
                "workflow_runs": [
                    {"head_sha": "redsha", "conclusion": "failure"},
                    {"head_sha": "greensha", "conclusion": "success"},
                ]
            },
        )
    )
    respx.get(f"{GITHUB_API_BASE}/repos/acme/widgets/compare/greensha...redsha").mock(
        return_value=httpx.Response(200, json={"status": "ahead"})
    )
    return respx.post(f"{GITHUB_API_BASE}/repos/acme/widgets/check-runs").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )


def _app(monkeypatch: pytest.MonkeyPatch, private_key_path: Path, session: Session) -> Any:
    monkeypatch.setenv("JANUS_GITHUB_APP_ID", "123")
    monkeypatch.setenv("JANUS_GITHUB_PRIVATE_KEY_PATH", str(private_key_path))
    monkeypatch.setenv("JANUS_GITHUB_WEBHOOK_SECRET", SECRET)
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return app


def _post(client: TestClient, body: bytes, delivery: str, signature: str | None = None) -> Any:
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": signature if signature is not None else _sign(body),
            "X-GitHub-Event": "workflow_run",
            "X-GitHub-Delivery": delivery,
            "Content-Type": "application/json",
        },
    )


@respx.mock
def test_signed_webhook_persists_run_and_posts_check(
    monkeypatch: pytest.MonkeyPatch, private_key_path: Path, session: Session
) -> None:
    check_route = _mock_github()
    app = _app(monkeypatch, private_key_path, session)
    body = json.dumps(_payload()).encode()

    with TestClient(app) as client:
        response = _post(client, body, "delivery-1")

    assert response.status_code == 202
    assert check_route.call_count == 1

    run = session.scalar(select(Run))
    assert run is not None
    assert run.head_sha == "redsha"
    assert run.baseline_sha == "greensha"

    job = session.scalar(select(Job))
    assert job is not None
    assert job.status == "queued"
    assert job.run_id == run.run_id


@respx.mock
def test_redelivery_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, private_key_path: Path, session: Session
) -> None:
    check_route = _mock_github()
    app = _app(monkeypatch, private_key_path, session)
    body = json.dumps(_payload()).encode()

    with TestClient(app) as client:
        first = _post(client, body, "delivery-1")
        second = _post(client, body, "delivery-1")

    assert first.status_code == 202
    assert second.status_code == 202
    assert check_route.call_count == 1
    assert session.scalar(select(func.count()).select_from(Run)) == 1
    assert session.scalar(select(func.count()).select_from(Job)) == 1


@respx.mock
def test_invalid_signature_is_rejected(
    monkeypatch: pytest.MonkeyPatch, private_key_path: Path, session: Session
) -> None:
    check_route = _mock_github()
    app = _app(monkeypatch, private_key_path, session)
    body = json.dumps(_payload()).encode()

    with TestClient(app) as client:
        response = _post(client, body, "delivery-1", signature="sha256=" + "0" * 64)

    assert response.status_code == 401
    assert check_route.call_count == 0
    assert session.scalar(select(func.count()).select_from(Run)) == 0
