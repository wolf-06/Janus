"""Signed webhook → delivery/run/job persisted → Check run posted.

GitHub's HTTP is mocked with respx; everything else (signature verification, the async
FastAPI endpoint, the repository layer, SQLite persistence) is real. Uses httpx's
ASGITransport so the app runs in the test's event loop alongside the async DB fixtures.
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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

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


def _client(session: AsyncSession) -> httpx.AsyncClient:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _post(
    client: httpx.AsyncClient, body: bytes, delivery: str, signature: str | None = None
) -> httpx.Response:
    return await client.post(
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
async def test_signed_webhook_persists_run_and_posts_check(
    monkeypatch: pytest.MonkeyPatch, private_key_path: Path, session: AsyncSession
) -> None:
    check_route = _mock_github()
    monkeypatch.setenv("JANUS_GITHUB_APP_ID", "123")
    monkeypatch.setenv("JANUS_GITHUB_PRIVATE_KEY_PATH", str(private_key_path))
    monkeypatch.setenv("JANUS_GITHUB_WEBHOOK_SECRET", SECRET)
    body = json.dumps(_payload()).encode()

    async with _client(session) as client:
        response = await _post(client, body, "delivery-1")

    assert response.status_code == 202
    assert check_route.call_count == 1

    run = await session.scalar(select(Run))
    assert run is not None and run.head_sha == "redsha"
    assert run.baseline_sha == "greensha"

    job = await session.scalar(select(Job))
    assert job is not None and job.state == "queued"


@respx.mock
async def test_redelivery_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, private_key_path: Path, session: AsyncSession
) -> None:
    check_route = _mock_github()
    monkeypatch.setenv("JANUS_GITHUB_APP_ID", "123")
    monkeypatch.setenv("JANUS_GITHUB_PRIVATE_KEY_PATH", str(private_key_path))
    monkeypatch.setenv("JANUS_GITHUB_WEBHOOK_SECRET", SECRET)
    body = json.dumps(_payload()).encode()

    async with _client(session) as client:
        first = await _post(client, body, "delivery-1")
        second = await _post(client, body, "delivery-1")

    assert first.status_code == 202
    assert second.status_code == 200
    assert check_route.call_count == 1
    assert await session.scalar(select(func.count()).select_from(Run)) == 1
    assert await session.scalar(select(func.count()).select_from(Job)) == 1


@respx.mock
async def test_invalid_signature_is_rejected(
    monkeypatch: pytest.MonkeyPatch, private_key_path: Path, session: AsyncSession
) -> None:
    check_route = _mock_github()
    monkeypatch.setenv("JANUS_GITHUB_APP_ID", "123")
    monkeypatch.setenv("JANUS_GITHUB_PRIVATE_KEY_PATH", str(private_key_path))
    monkeypatch.setenv("JANUS_GITHUB_WEBHOOK_SECRET", SECRET)
    body = json.dumps(_payload()).encode()

    async with _client(session) as client:
        response = await _post(client, body, "delivery-1", signature="sha256=" + "0" * 64)

    assert response.status_code == 401
    assert check_route.call_count == 0
    assert await session.scalar(select(func.count()).select_from(Run)) == 0


@respx.mock
async def test_queue_depth_backpressure_returns_503(
    monkeypatch: pytest.MonkeyPatch, private_key_path: Path, session: AsyncSession
) -> None:
    _mock_github()
    monkeypatch.setenv("JANUS_GITHUB_APP_ID", "123")
    monkeypatch.setenv("JANUS_GITHUB_PRIVATE_KEY_PATH", str(private_key_path))
    monkeypatch.setenv("JANUS_GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("JANUS_QUEUE_DEPTH_LIMIT", "0")
    body = json.dumps(_payload()).encode()

    async with _client(session) as client:
        response = await _post(client, body, "delivery-1")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"
    assert await session.scalar(select(func.count()).select_from(Run)) == 0
