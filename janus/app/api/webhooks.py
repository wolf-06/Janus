"""GitHub webhook ingress.

Responsibilities, in order: verify the HMAC over the raw body (401 on failure), reject
when the queue is saturated (503 + ``Retry-After``), dedupe the delivery
(``INSERT ... ON CONFLICT DO NOTHING``), then — in ONE transaction — record the run and
enqueue a job, and finally return 202.

The critical path does **no triage work**. The acknowledgement Check run and baseline
resolution are best-effort *after* the job is durably enqueued, run on a worker thread
because the GitHub SDK is synchronous while the database layer is async.

``check_run`` events are accepted but never enqueued: Janus posts Check runs itself, and
reacting to them would create a loop.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from janus.app.config import Settings, get_settings
from janus.db.repository import (
    get_or_create_run,
    record_delivery,
    set_run_baseline,
    upsert_installation,
    upsert_repository,
)
from janus.db.session import get_session
from janus.github.app_auth import installation_token
from janus.github.baseline import resolve_baseline
from janus.github.client import GitHubClient
from janus.github.webhook_verify import verify_signature
from janus.jobs.queue import enqueue, queued_depth
from janus.report.check_run import start_check_run

logger = structlog.get_logger(__name__)

router = APIRouter()

# Conclusions that count as a "red" build worth triaging.
_FAILURE_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})
_ACCEPTED_EVENTS = frozenset({"workflow_run", "check_run"})
_RETRY_AFTER_SECONDS = "60"


@router.post("/webhooks/github", status_code=202)
async def github_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    x_hub_signature_256: Annotated[str | None, Header()] = None,
    x_github_event: Annotated[str | None, Header()] = None,
    x_github_delivery: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    settings = get_settings()
    body = await request.body()

    if not verify_signature(settings.github_webhook_secret or "", body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="invalid signature")

    if x_github_event not in _ACCEPTED_EVENTS:
        return JSONResponse({"status": "ignored"}, status_code=202)

    payload: dict[str, Any] = json.loads(body)
    if x_github_event != "workflow_run" or not _is_failed_workflow_run(payload):
        return JSONResponse({"status": "ignored"}, status_code=202)

    if await queued_depth(session) >= settings.queue_depth_limit:
        raise HTTPException(
            status_code=503,
            detail="queue saturated",
            headers={"Retry-After": _RETRY_AFTER_SECONDS},
        )

    status_code = await _ingest(settings, session, payload, x_github_delivery)
    return JSONResponse({"status": "accepted"}, status_code=status_code)


def _is_failed_workflow_run(payload: dict[str, Any]) -> bool:
    if payload.get("action") != "completed":
        return False
    run = payload.get("workflow_run") or {}
    return run.get("conclusion") in _FAILURE_CONCLUSIONS


async def _ingest(
    settings: Settings,
    session: AsyncSession,
    payload: dict[str, Any],
    delivery_id: str | None,
) -> int:
    """Persist delivery + run + job in one transaction; return the HTTP status code."""
    run_data = payload["workflow_run"]
    repo_data = payload["repository"]
    inst_data = payload.get("installation") or {}
    owner = repo_data["owner"]["login"]
    name = repo_data["name"]
    installation_id = int(inst_data["id"])
    repo_id = int(repo_data["id"])
    run_id = int(run_data["id"])
    key = delivery_id or f"run:{run_id}"

    await upsert_installation(
        session,
        installation_id=installation_id,
        account=owner,
        app_id=int(inst_data.get("app_id") or settings.github_app_id or 0),
    )
    await upsert_repository(
        session,
        repo_id=repo_id,
        owner=owner,
        name=name,
        default_branch=repo_data.get("default_branch") or "main",
        installation_id=installation_id,
    )

    if not await record_delivery(session, delivery_id=key, repo_id=repo_id, event="workflow_run"):
        # Redelivered event: nothing to do, and nothing to enqueue.
        await session.rollback()
        return 200

    await get_or_create_run(
        session,
        run_id=run_id,
        repo_id=repo_id,
        head_sha=run_data["head_sha"],
        head_branch=run_data.get("head_branch") or "",
        conclusion=run_data.get("conclusion") or "",
        event=run_data.get("event") or "push",
    )
    await enqueue(
        session,
        delivery_id=key,
        repo_id=repo_id,
        run_id=run_id,
        max_attempts=settings.max_job_attempts,
    )
    await session.commit()

    # Past the critical path: best-effort baseline + acknowledgement Check run.
    head_sha = run_data["head_sha"]
    branch = run_data.get("head_branch") or repo_data.get("default_branch") or "main"
    await _resolve_and_report(
        settings, session, installation_id, owner, name, branch, head_sha, run_id
    )
    return 202


async def _resolve_and_report(
    settings: Settings,
    session: AsyncSession,
    installation_id: int,
    owner: str,
    name: str,
    branch: str,
    head_sha: str,
    run_id: int,
) -> None:
    try:
        baseline = await run_in_threadpool(
            _resolve_baseline_sync, settings, installation_id, owner, name, branch, head_sha
        )
        await set_run_baseline(session, run_id, baseline)
    except Exception as exc:  # noqa: BLE001 - best-effort, never fail ingest
        logger.warning("baseline_resolution_failed", error=str(exc), run_id=run_id)

    try:
        await run_in_threadpool(_post_check_sync, settings, installation_id, owner, name, head_sha)
    except Exception as exc:  # noqa: BLE001 - best-effort, never fail ingest
        logger.warning("check_run_failed", error=str(exc), run_id=run_id)

    try:
        await session.commit()
    except Exception:  # pragma: no cover - defensive
        await session.rollback()


def _resolve_baseline_sync(
    settings: Settings,
    installation_id: int,
    owner: str,
    name: str,
    branch: str,
    head_sha: str,
) -> str | None:
    token = installation_token(settings, installation_id)
    with GitHubClient(token) as client:
        return resolve_baseline(client, owner, name, branch=branch, head_sha=head_sha)


def _post_check_sync(
    settings: Settings, installation_id: int, owner: str, name: str, head_sha: str
) -> None:
    token = installation_token(settings, installation_id)
    with GitHubClient(token) as client:
        start_check_run(client, owner, name, head_sha=head_sha)
