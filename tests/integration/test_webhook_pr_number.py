"""Ingest records the PR number on the run row, or ``None`` when absent/malformed.

Reuses the signed-webhook helpers from ``test_webhook_flow`` so the whole path (signature
verification, async endpoint, repository layer, SQLite persistence) stays identical.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from janus.db.models import Run
from tests.integration.test_webhook_flow import (
    SECRET,
    _client,
    _mock_github,
    _payload,
    _post,
)
from tests.integration.test_webhook_flow import (
    private_key_path as _webhook_private_key_path,  # noqa: F401 - pytest fixture alias
)


@pytest.fixture
def private_key_path(
    _webhook_private_key_path: Path,  # noqa: F811 - pytest fixture dependency
) -> Path:
    """Reuse the signed-webhook key fixture from ``test_webhook_flow``."""
    return _webhook_private_key_path


def _configure(monkeypatch: pytest.MonkeyPatch, private_key: Path) -> None:
    monkeypatch.setenv("JANUS_GITHUB_APP_ID", "123")
    monkeypatch.setenv("JANUS_GITHUB_PRIVATE_KEY_PATH", str(private_key))
    monkeypatch.setenv("JANUS_GITHUB_WEBHOOK_SECRET", SECRET)


@respx.mock
async def test_pr_number_recorded_when_present(
    monkeypatch: pytest.MonkeyPatch, private_key_path: Path, session: AsyncSession
) -> None:
    _mock_github()
    _configure(monkeypatch, private_key_path)
    payload = _payload()
    payload["workflow_run"]["pull_requests"] = [{"number": 42}]
    body = json.dumps(payload).encode()

    async with _client(session) as client:
        response = await _post(client, body, "delivery-pr")

    assert response.status_code == 202
    run = await session.scalar(select(Run))
    assert run is not None and run.pr_number == 42


@respx.mock
async def test_pr_number_is_none_when_absent(
    monkeypatch: pytest.MonkeyPatch, private_key_path: Path, session: AsyncSession
) -> None:
    _mock_github()
    _configure(monkeypatch, private_key_path)
    body = json.dumps(_payload()).encode()

    async with _client(session) as client:
        response = await _post(client, body, "delivery-no-pr")

    assert response.status_code == 202
    run = await session.scalar(select(Run))
    assert run is not None and run.pr_number is None


@pytest.mark.parametrize("pull_requests", [[], "nope", [{"number": "42"}], [{"number": None}]])
@respx.mock
async def test_malformed_pr_payload_stores_none(
    monkeypatch: pytest.MonkeyPatch,
    private_key_path: Path,
    session: AsyncSession,
    pull_requests: Any,
) -> None:
    _mock_github()
    _configure(monkeypatch, private_key_path)
    payload = _payload()
    payload["workflow_run"]["pull_requests"] = pull_requests
    body = json.dumps(payload).encode()

    async with _client(session) as client:
        response = await _post(client, body, "delivery-bad-pr")

    assert response.status_code == 202
    run = await session.scalar(select(Run))
    assert run is not None and run.pr_number is None
