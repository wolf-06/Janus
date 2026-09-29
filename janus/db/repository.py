"""Typed data-access helpers.

Functions here **flush but do not commit**: the caller owns the transaction boundary
(typically one webhook delivery = one transaction). This keeps retries and idempotency
decisions in one place.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from janus.db.models import Diagnosis, Installation, Repository, Run


def upsert_installation(
    session: Session,
    *,
    installation_id: int,
    account: str,
    app_id: int,
    scopes: list[str] | None = None,
) -> Installation:
    """Insert or update an installation keyed on its GitHub id."""
    installation = session.get(Installation, installation_id)
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
    session.flush()
    return installation


def get_repository(session: Session, repo_id: int) -> Repository | None:
    return session.get(Repository, repo_id)


def upsert_repository(
    session: Session,
    *,
    repo_id: int,
    owner: str,
    name: str,
    default_branch: str,
    installation_id: int,
) -> Repository:
    """Insert or update a repository keyed on its GitHub id."""
    repository = session.get(Repository, repo_id)
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
    session.flush()
    return repository


def get_or_create_run(
    session: Session,
    *,
    run_id: int,
    repo_id: int,
    head_sha: str,
    head_branch: str,
    conclusion: str,
    event: str,
    delivery_id: str | None = None,
) -> tuple[Run, bool]:
    """Return ``(run, created)``.

    Idempotent on ``delivery_id`` (a redelivered webhook returns the existing run) and
    on ``run_id`` (the same workflow run observed twice is not duplicated).
    """
    if delivery_id is not None:
        existing = session.scalar(select(Run).where(Run.delivery_id == delivery_id))
        if existing is not None:
            return existing, False

    existing_run = session.get(Run, run_id)
    if existing_run is not None:
        return existing_run, False

    run = Run(
        run_id=run_id,
        repo_id=repo_id,
        head_sha=head_sha,
        head_branch=head_branch,
        conclusion=conclusion,
        event=event,
        delivery_id=delivery_id,
    )
    session.add(run)
    session.flush()
    return run, True


def set_run_baseline(session: Session, run_id: int, baseline_sha: str | None) -> Run:
    """Store the last-green commit used as the base of the fault diff."""
    run = session.get(Run, run_id)
    if run is None:
        raise LookupError(f"run {run_id} not found")
    run.baseline_sha = baseline_sha
    session.flush()
    return run


def create_diagnosis(
    session: Session,
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
    session.flush()
    return diagnosis
