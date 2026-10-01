"""Admin endpoints for the dead-letter queue (backend spec §4.2).

``GET /admin/dead`` lists dead-lettered jobs; ``POST /admin/jobs/{id}/requeue`` puts one
back on the queue. Requeue is an explicit operator action, so it bypasses the normal state
machine (a ``dead`` job is terminal by design).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from janus.db.models import Job
from janus.db.session import get_session
from janus.jobs.state import JobStatus

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/dead")
async def list_dead(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List dead-lettered jobs, oldest first."""
    rows = (
        await session.scalars(
            select(Job).where(Job.state == JobStatus.DEAD.value).order_by(Job.id).limit(limit)
        )
    ).all()
    return [
        {
            "id": job.id,
            "repo_id": job.repo_id,
            "run_id": job.run_id,
            "attempts": job.attempts,
            "last_error": job.last_error,
        }
        for job in rows
    ]


@router.post("/jobs/{job_id}/requeue", status_code=202)
async def requeue(
    job_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Return a dead-lettered job to the queue with a fresh attempt budget."""
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    job.state = JobStatus.QUEUED.value
    job.attempts = 0
    job.run_after = None
    job.locked_by = None
    job.lease_expires_at = None
    job.last_error = None
    job.finished_at = None
    await session.commit()
    return {"status": "requeued", "job_id": job_id}
