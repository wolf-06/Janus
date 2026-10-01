"""Typed async data-access helpers.

Functions here **flush but do not commit**: the caller owns the transaction boundary
(typically one webhook delivery = one transaction). This keeps retries and idempotency
decisions in one place.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, Insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from janus.db.models import Delivery, Diagnosis, Installation, Repository, Run


async def upsert_installation(
    session: AsyncSession,
    *,
    installation_id: int,
    account: str,
    app_id: int,
    scopes: list[str] | None = None,
) -> Installation:
    """Insert or update an installation keyed on its GitHub id."""
    installation = await session.get(Installation, installation_id)
    if installation is None:
        installation = Installation(
            installation_id=installation_id,
            account=account,
            app_id=app_id,
            scopes=scopes or [],
        )
        session.add(installation)
    else:
        installation.account = account
        installation.app_id = app_id
        if scopes is not None:
            installation.scopes = scopes
    await session.flush()
    return installation


async def upsert_repository(
    session: AsyncSession,
    *,
    repo_id: int,
    owner: str,
    name: str,
    default_branch: str,
    installation_id: int,
) -> Repository:
    """Insert or update a repository keyed on its GitHub id."""
    repository = await session.get(Repository, repo_id)
    if repository is None:
        repository = Repository(
            repo_id=repo_id,
            owner=owner,
            name=name,
            default_branch=default_branch,
            installation_id=installation_id,
        )
        session.add(repository)
    else:
        repository.owner = owner
        repository.name = name
        repository.default_branch = default_branch
        repository.installation_id = installation_id
    await session.flush()
    return repository


async def record_delivery(
    session: AsyncSession, *, delivery_id: str, repo_id: int, event: str
) -> bool:
    """Record a webhook delivery; return ``True`` only if it is newly inserted.

    Uses ``INSERT ... ON CONFLICT DO NOTHING`` on the ``deliveries`` primary key so a
    redelivered event is a no-op (backend spec §4.1). The row is flushed inside the
    caller's transaction.
    """
    values = {"delivery_id": delivery_id, "repo_id": repo_id, "event": event}
    dialect = session.get_bind().dialect.name
    statement: Insert
    if dialect == "postgresql":
        statement = (
            pg_insert(Delivery)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["delivery_id"])
        )
    elif dialect == "sqlite":
        statement = (
            sqlite_insert(Delivery)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["delivery_id"])
        )
    else:  # pragma: no cover - only Postgres and SQLite are supported
        raise RuntimeError(f"unsupported dialect for upsert: {dialect}")

    result = await session.execute(statement)
    return bool(cast("CursorResult[Any]", result).rowcount)


async def get_or_create_run(
    session: AsyncSession,
    *,
    run_id: int,
    repo_id: int,
    head_sha: str,
    head_branch: str,
    conclusion: str,
    event: str,
) -> tuple[Run, bool]:
    """Return ``(run, created)``; idempotent on ``run_id``."""
    existing = await session.get(Run, run_id)
    if existing is not None:
        return existing, False

    run = Run(
        run_id=run_id,
        repo_id=repo_id,
        head_sha=head_sha,
        head_branch=head_branch,
        conclusion=conclusion,
        event=event,
    )
    session.add(run)
    await session.flush()
    return run, True


async def set_run_baseline(session: AsyncSession, run_id: int, baseline_sha: str | None) -> Run:
    """Store the last-green commit used as the base of the fault diff."""
    run = await session.get(Run, run_id)
    if run is None:
        raise LookupError(f"run {run_id} not found")
    run.baseline_sha = baseline_sha
    await session.flush()
    return run


async def create_diagnosis(
    session: AsyncSession,
    *,
    job_id: int,
    route: str,
    confidence: float,
    top2_gap: float,
    evidence: dict[str, Any] | None = None,
) -> Diagnosis:
    """Record a triage outcome for a job."""
    diagnosis = Diagnosis(
        job_id=job_id,
        route=route,
        confidence=confidence,
        top2_gap=top2_gap,
        evidence=evidence or {},
    )
    session.add(diagnosis)
    await session.flush()
    return diagnosis
