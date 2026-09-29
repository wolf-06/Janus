"""Concurrency: two workers never claim the same job.

SQLite ignores ``FOR UPDATE``, so the real ``SKIP LOCKED`` guarantee can only be
exercised on Postgres. The test is skipped unless ``JANUS_TEST_DATABASE_URL`` is set (CI
supplies it via a service container; locally point it at the compose Postgres).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from janus.db.models import Base
from janus.db.repository import get_or_create_run, upsert_installation, upsert_repository
from janus.jobs.queue import claim_next, enqueue
from janus.jobs.state import JobStatus

pytestmark = pytest.mark.skipif(
    not os.environ.get("JANUS_TEST_DATABASE_URL"),
    reason="requires JANUS_TEST_DATABASE_URL (Postgres)",
)


@pytest.fixture
def pg_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(os.environ["JANUS_TEST_DATABASE_URL"])
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _seed(factory: sessionmaker[Session], run_ids: list[int]) -> None:
    with factory() as session:
        upsert_installation(session, installation_id=1, account="acme", app_id=1)
        upsert_repository(
            session,
            repo_id=2,
            owner="acme",
            name="widgets",
            default_branch="main",
            installation_id=1,
        )
        for run_id in run_ids:
            get_or_create_run(
                session,
                run_id=run_id,
                repo_id=2,
                head_sha=f"sha{run_id}",
                head_branch="main",
                conclusion="failure",
                event="push",
            )
            enqueue(session, run_id=run_id)
        session.commit()


def test_skip_locked_prevents_double_claim(pg_factory: sessionmaker[Session]) -> None:
    _seed(pg_factory, [10])
    first = pg_factory()
    second = pg_factory()
    try:
        claimed = claim_next(first)  # locks the only job
        skipped = claim_next(second)  # SKIP LOCKED -> nothing
        assert claimed is not None
        assert claimed.status == JobStatus.CLAIMED.value
        assert skipped is None

        first.commit()
        assert claim_next(second) is None  # committed as claimed, no longer queued
    finally:
        first.close()
        second.close()


def test_two_workers_claim_distinct_jobs(pg_factory: sessionmaker[Session]) -> None:
    _seed(pg_factory, [10, 11])
    first = pg_factory()
    second = pg_factory()
    try:
        job_a = claim_next(first)
        job_b = claim_next(second)
        assert job_a is not None and job_b is not None
        assert job_a.id != job_b.id
    finally:
        first.rollback()
        second.rollback()
        first.close()
        second.close()
