"""Typed async data-access helpers.

Functions here **flush but do not commit**: the caller owns the transaction boundary
(typically one webhook delivery = one transaction). This keeps retries and idempotency
decisions in one place.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, Insert, case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from janus.db.models import (
    Delivery,
    Diagnosis,
    Fix,
    GateResult,
    Installation,
    Job,
    Repository,
    Reproduction,
    Run,
    Signal,
    Suspect,
)


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


async def set_diagnosis(
    session: AsyncSession,
    *,
    job_id: int,
    route: str,
    confidence: float,
    top2_gap: float,
    evidence: dict[str, Any] | None = None,
) -> Diagnosis:
    """Upsert the single triage outcome for a job (one diagnosis row per job)."""
    existing = await session.scalar(select(Diagnosis).where(Diagnosis.job_id == job_id))
    if existing is None:
        return await create_diagnosis(
            session,
            job_id=job_id,
            route=route,
            confidence=confidence,
            top2_gap=top2_gap,
            evidence=evidence,
        )
    existing.route = route
    existing.confidence = confidence
    existing.top2_gap = top2_gap
    existing.evidence = evidence or {}
    await session.flush()
    return existing


async def add_reproductions(
    session: AsyncSession, job_id: int, reproductions: Sequence[Reproduction]
) -> list[Reproduction]:
    """Attach rerun evidence rows to a job; flush, never commit."""
    for reproduction in reproductions:
        reproduction.job_id = job_id
    session.add_all(reproductions)
    await session.flush()
    return list(reproductions)


async def add_signals(
    session: AsyncSession, job_id: int, signals: Sequence[Signal]
) -> list[Signal]:
    """Attach scored triage signals to a job; flush, never commit."""
    for signal in signals:
        signal.job_id = job_id
    session.add_all(signals)
    await session.flush()
    return list(signals)


async def add_suspects(
    session: AsyncSession, job_id: int, suspects: Sequence[Suspect]
) -> list[Suspect]:
    """Attach ranked suspects to a job; flush, never commit."""
    for suspect in suspects:
        suspect.job_id = job_id
    session.add_all(suspects)
    await session.flush()
    return list(suspects)


async def add_fix(
    session: AsyncSession,
    *,
    job_id: int,
    suspect_id: int | None,
    attempt: int,
    diff: str,
    status: str,
) -> Fix:
    """Record a generated fix attempt; flush, never commit."""
    fix = Fix(
        job_id=job_id,
        suspect_id=suspect_id,
        attempt=attempt,
        diff=diff,
        status=status,
    )
    session.add(fix)
    await session.flush()
    return fix


async def add_gate_results(
    session: AsyncSession, fix_id: int, results: Sequence[GateResult]
) -> list[GateResult]:
    """Attach proof/policy gate outcomes to a fix; flush, never commit."""
    for result in results:
        result.fix_id = fix_id
    session.add_all(results)
    await session.flush()
    return list(results)


async def set_run_pr_number(session: AsyncSession, run_id: int, pr_number: int | None) -> Run:
    """Store the pull request a failing run belongs to (``None`` when absent)."""
    run = await session.get(Run, run_id)
    if run is None:
        raise LookupError(f"run {run_id} not found")
    run.pr_number = pr_number
    await session.flush()
    return run


async def flake_history(
    session: AsyncSession, repo_id: int, target_ids: Sequence[str]
) -> dict[str, float]:
    """Historical failure rate per target for a repository's past reproductions.

    For each requested target id this is failed attempts / total attempts across the
    repository's ``reproductions`` rows (joined to ``jobs`` on ``repo_id``). Full-suite
    rows (``target_id IS NULL``) are ignored. Every requested id is present in the
    result; ids with no history map to ``0.0``. An empty request short-circuits to ``{}``
    without querying. Flush-only, like the rest of this module: no commit.
    """
    requested = list(dict.fromkeys(target_ids))
    if not requested:
        return {}

    failed = func.sum(case((Reproduction.passed.is_(False), 1), else_=0))
    statement = (
        select(Reproduction.target_id, func.count(), failed)
        .join(Job, Job.id == Reproduction.job_id)
        .where(
            Job.repo_id == repo_id,
            Reproduction.target_id.is_not(None),
            Reproduction.target_id.in_(requested),
        )
        .group_by(Reproduction.target_id)
    )
    rows = (await session.execute(statement)).all()

    rates: dict[str, float] = {target_id: 0.0 for target_id in requested}
    for target_id, total, failures in rows:
        if target_id is not None and total:
            rates[target_id] = failures / total
    return rates
