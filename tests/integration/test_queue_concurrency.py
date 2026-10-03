"""Concurrency: two workers never claim the same job.

SQLite ignores ``FOR UPDATE``, so the real ``SKIP LOCKED`` guarantee can only be
exercised on Postgres. Skipped unless ``JANUS_TEST_DATABASE_URL`` is set (CI provides it;
locally point it at a dedicated ``janus_test`` database).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from janus.db.models import Base
from janus.db.repository import (
    get_or_create_run,
    record_delivery,
    upsert_installation,
    upsert_repository,
)
from janus.jobs.queue import claim_next, enqueue
from janus.jobs.state import JobStatus

pytestmark = pytest.mark.skipif(
    not os.environ.get("JANUS_TEST_DATABASE_URL"),
    reason="requires JANUS_TEST_DATABASE_URL (Postgres)",
)


@pytest.fixture
async def pg_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(os.environ["JANUS_TEST_DATABASE_URL"])
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


async def _seed(factory: async_sessionmaker[AsyncSession], delivery_ids: list[str]) -> None:
    async with factory() as session:
        await upsert_installation(session, installation_id=1, account="acme", app_id=1)
        await upsert_repository(
            session,
            repo_id=2,
            owner="acme",
            name="widgets",
            default_branch="main",
            installation_id=1,
        )
        for index, delivery_id in enumerate(delivery_ids):
            run_id = 10 + index
            await record_delivery(session, delivery_id=delivery_id, repo_id=2, event="workflow_run")
            await get_or_create_run(
                session,
                run_id=run_id,
                repo_id=2,
                head_sha=f"sha{run_id}",
                head_branch="main",
                conclusion="failure",
                event="push",
            )
            await enqueue(session, delivery_id=delivery_id, repo_id=2, run_id=run_id)
        await session.commit()


async def test_skip_locked_prevents_double_claim(
    pg_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(pg_factory, ["d1"])
    first = pg_factory()
    second = pg_factory()
    try:
        claimed = await claim_next(first, worker_id="w1")
        skipped = await claim_next(second, worker_id="w2")
        assert claimed is not None
        assert claimed.state == JobStatus.RUNNING.value
        assert skipped is None

        await first.commit()
        assert await claim_next(second, worker_id="w2") is None
    finally:
        await first.close()
        await second.close()


async def test_two_workers_claim_distinct_jobs(
    pg_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(pg_factory, ["d1", "d2"])
    first = pg_factory()
    second = pg_factory()
    try:
        job_a = await claim_next(first, worker_id="w1")
        job_b = await claim_next(second, worker_id="w2")
        assert job_a is not None and job_b is not None
        assert job_a.id != job_b.id
    finally:
        await first.rollback()
        await second.rollback()
        await first.close()
        await second.close()
