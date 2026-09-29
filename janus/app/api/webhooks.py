"""GitHub webhook ingress.

Responsibilities, in order: verify the HMAC over the raw body (401 on failure), filter
to the events S0 cares about, persist the installation/repository/run, enqueue a job, and
post the acknowledgement Check run. The endpoint returns quickly with 202; the GitHub
calls run in a worker thread so the event loop is not blocked.

``check_run`` events are accepted but never enqueued: Janus posts Check runs itself, and
reacting to them would create a loop.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from janus.app.config import Settings, get_settings
from janus.db.repository import (
    get_or_create_run,
    set_run_baseline,
    upsert_installation,
    upsert_repository,
)
from janus.db.session import get_session
from janus.github.app_auth import installation_token
from janus.github.baseline import resolve_baseline
from janus.github.client import GitHubClient
from janus.github.webhook_verify import verify_signature
from janus.jobs.queue import enqueue
from janus.report.check_run import start_check_run

router = APIRouter()

# Conclusions that count as a "red" build worth triaging.
_FAILURE_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})
_ACCEPTED_EVENTS = frozenset({"workflow_run", "check_run"})


@router.post("/webhooks/github", status_code=202)
async def github_webhook(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    x_hub_signature_256: Annotated[str | None, Header()] = None,
    x_github_event: Annotated[str | None, Header()] = None,
    x_github_delivery: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    settings = get_settings()
    body = await request.body()

    if not verify_signature(settings.github_webhook_secret or "", body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="invalid signature")

    if x_github_event not in _ACCEPTED_EVENTS:
        return {"status": "ignored"}

    payload: dict[str, Any] = json.loads(body)
    if x_github_event == "workflow_run":
        await run_in_threadpool(_handle_workflow_run, settings, session, payload, x_github_delivery)

    return {"status": "accepted"}


def _is_failed_workflow_run(payload: dict[str, Any]) -> bool:
    if payload.get("action") != "completed":
        return False
    run = payload.get("workflow_run") or {}
    return run.get("conclusion") in _FAILURE_CONCLUSIONS


def _handle_workflow_run(
    settings: Settings,
    session: Session,
    payload: dict[str, Any],
    delivery_id: str | None,
) -> None:
    """Persist and enqueue a failed workflow run; idempotent on ``delivery_id``."""
    if not _is_failed_workflow_run(payload):
        return

    run_data = payload["workflow_run"]
    repo_data = payload["repository"]
    inst_data = payload.get("installation") or {}
    owner = repo_data["owner"]["login"]
    name = repo_data["name"]
    installation_id = int(inst_data["id"])

    upsert_installation(
        session,
        installation_id=installation_id,
        account=owner,
        app_id=int(inst_data.get("app_id") or settings.github_app_id or 0),
    )
    upsert_repository(
        session,
        repo_id=int(repo_data["id"]),
        owner=owner,
        name=name,
        default_branch=repo_data.get("default_branch") or "main",
        installation_id=installation_id,
    )
    run, created = get_or_create_run(
        session,
        run_id=int(run_data["id"]),
        repo_id=int(repo_data["id"]),
        head_sha=run_data["head_sha"],
        head_branch=run_data.get("head_branch") or "",
        conclusion=run_data.get("conclusion") or "",
        event=run_data.get("event") or "push",
        delivery_id=delivery_id,
    )
    if not created:
        session.commit()
        return

    head_sha = run_data["head_sha"]
    branch = run_data.get("head_branch") or repo_data.get("default_branch") or "main"
    token = installation_token(settings, installation_id)
    with GitHubClient(token) as client:
        baseline = resolve_baseline(client, owner, name, branch=branch, head_sha=head_sha)
        set_run_baseline(session, run.run_id, baseline)
        enqueue(session, run_id=run.run_id)
        start_check_run(client, owner, name, head_sha=head_sha)
    session.commit()
