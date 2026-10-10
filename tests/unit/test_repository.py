"""Repository helpers for the Phase 4/5/6 tables (flush, never commit)."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from janus.db.models import (
    Diagnosis,
    GateResult,
    Reproduction,
    Run,
    Signal,
    Suspect,
)
from janus.db.repository import (
    add_fix,
    add_gate_results,
    add_reproductions,
    add_signals,
    add_suspects,
    flake_history,
    get_or_create_run,
    record_delivery,
    set_diagnosis,
    set_run_pr_number,
    upsert_repository,
)
from janus.jobs.queue import enqueue


async def _job_id(session: AsyncSession, run: Run) -> int:
    await record_delivery(session, delivery_id="d1", repo_id=run.repo_id, event="workflow_run")
    job = await enqueue(session, delivery_id="d1", repo_id=run.repo_id, run_id=run.run_id)
    await session.flush()
    return job.id


async def test_add_reproductions_stamps_job_id_and_persists(
    session: AsyncSession, seeded_run: Run
) -> None:
    job_id = await _job_id(session, seeded_run)
    rows = await add_reproductions(
        session,
        job_id,
        [
            Reproduction(
                profile="clean",
                attempt_index=0,
                target_id="tests/test_x.py::test_a",
                passed=False,
                duration_s=1.5,
                exit_code=1,
            ),
            Reproduction(
                profile="clean",
                attempt_index=1,
                target_id=None,
                passed=True,
                duration_s=0.5,
            ),
        ],
    )

    assert [row.job_id for row in rows] == [job_id, job_id]
    stored = (
        await session.scalars(select(Reproduction).where(Reproduction.job_id == job_id))
    ).all()
    assert len(stored) == 2
    assert stored[1].target_id is None


async def test_add_signals_persists_details(session: AsyncSession, seeded_run: Run) -> None:
    job_id = await _job_id(session, seeded_run)
    rows = await add_signals(
        session,
        job_id,
        [
            Signal(name="determinism", summary="always fails", details={"runs": 5}),
            Signal(name="exit_code", summary="exit 137", details={"code": 137}),
        ],
    )

    assert all(row.job_id == job_id for row in rows)
    count = await session.scalar(
        select(func.count()).select_from(Signal).where(Signal.job_id == job_id)
    )
    assert count == 2


async def test_add_suspects_persists_ranking(session: AsyncSession, seeded_run: Run) -> None:
    job_id = await _job_id(session, seeded_run)
    rows = await add_suspects(
        session,
        job_id,
        [
            Suspect(file="src/a.py", line=10, score=0.9, rank=1),
            Suspect(file="src/b.py", line=None, score=0.4, rank=2, introducing_commit="abc123"),
        ],
    )

    assert all(row.job_id == job_id for row in rows)
    stored = (
        await session.scalars(
            select(Suspect).where(Suspect.job_id == job_id).order_by(Suspect.rank)
        )
    ).all()
    assert [row.rank for row in stored] == [1, 2]
    assert stored[1].introducing_commit == "abc123"


async def test_add_fix_and_gate_results(session: AsyncSession, seeded_run: Run) -> None:
    job_id = await _job_id(session, seeded_run)
    suspects = await add_suspects(
        session, job_id, [Suspect(file="src/a.py", line=10, score=0.9, rank=1)]
    )

    fix = await add_fix(
        session,
        job_id=job_id,
        suspect_id=suspects[0].id,
        attempt=1,
        diff="--- a\n+++ b\n",
        status="pending",
    )
    assert fix.job_id == job_id
    assert fix.suspect_id == suspects[0].id

    results = await add_gate_results(
        session,
        fix.id,
        [
            GateResult(gate="full_suite", passed=True, measured_value="green"),
            GateResult(
                gate="mutation",
                passed=False,
                measured_value="0.61",
                baseline_value="0.70",
            ),
        ],
    )

    assert all(result.fix_id == fix.id for result in results)
    count = await session.scalar(
        select(func.count()).select_from(GateResult).where(GateResult.fix_id == fix.id)
    )
    assert count == 2


async def test_set_diagnosis_upserts_one_row_per_job(
    session: AsyncSession, seeded_run: Run
) -> None:
    job_id = await _job_id(session, seeded_run)

    first = await set_diagnosis(
        session,
        job_id=job_id,
        route="UNSURE",
        confidence=0.5,
        top2_gap=0.01,
        evidence={"note": "first"},
    )
    second = await set_diagnosis(
        session,
        job_id=job_id,
        route="CODE_BUG",
        confidence=0.87,
        top2_gap=0.4,
        evidence={"note": "second"},
    )

    assert first.id == second.id
    stored = (await session.scalars(select(Diagnosis).where(Diagnosis.job_id == job_id))).all()
    assert len(stored) == 1
    assert stored[0].route == "CODE_BUG"
    assert stored[0].confidence == 0.87
    assert stored[0].evidence == {"note": "second"}


async def test_set_run_pr_number(session: AsyncSession, seeded_run: Run) -> None:
    run = await set_run_pr_number(session, seeded_run.run_id, 1234)
    assert run.pr_number == 1234

    run = await set_run_pr_number(session, seeded_run.run_id, None)
    assert run.pr_number is None


async def test_set_run_pr_number_missing_run_raises(session: AsyncSession) -> None:
    with pytest.raises(LookupError):
        await set_run_pr_number(session, 999, 1)


def _reproduction(target_id: str | None, passed: bool) -> Reproduction:
    return Reproduction(
        profile="clean",
        attempt_index=0,
        target_id=target_id,
        passed=passed,
        duration_s=1.0,
        exit_code=0 if passed else 1,
    )


async def test_flake_history_empty_targets_returns_empty(
    session: AsyncSession, seeded_run: Run
) -> None:
    assert await flake_history(session, seeded_run.repo_id, []) == {}


async def test_flake_history_unknown_target_is_zero(session: AsyncSession, seeded_run: Run) -> None:
    rates = await flake_history(session, seeded_run.repo_id, ["tests/test_a.py::test_missing"])
    assert rates == {"tests/test_a.py::test_missing": 0.0}


async def test_flake_history_computes_failure_rate(session: AsyncSession, seeded_run: Run) -> None:
    job_id = await _job_id(session, seeded_run)
    await add_reproductions(
        session,
        job_id,
        [
            _reproduction("tests/test_a.py::test_flaky", passed=True),
            _reproduction("tests/test_a.py::test_flaky", passed=False),
            _reproduction("tests/test_a.py::test_flaky", passed=False),
            _reproduction("tests/test_a.py::test_solid", passed=True),
        ],
    )

    rates = await flake_history(
        session,
        seeded_run.repo_id,
        ["tests/test_a.py::test_flaky", "tests/test_a.py::test_solid"],
    )

    assert rates == {
        "tests/test_a.py::test_flaky": pytest.approx(2 / 3),
        "tests/test_a.py::test_solid": 0.0,
    }


async def test_flake_history_ignores_full_suite_rows(
    session: AsyncSession, seeded_run: Run
) -> None:
    job_id = await _job_id(session, seeded_run)
    await add_reproductions(
        session,
        job_id,
        [
            _reproduction(None, passed=False),
            _reproduction(None, passed=False),
            _reproduction("tests/test_a.py::test_a", passed=True),
        ],
    )

    rates = await flake_history(session, seeded_run.repo_id, ["tests/test_a.py::test_a"])

    assert rates == {"tests/test_a.py::test_a": 0.0}


async def test_flake_history_only_counts_requested_repository(
    session: AsyncSession, seeded_run: Run
) -> None:
    other_job_id = await _job_id(session, seeded_run)

    await upsert_repository(
        session,
        repo_id=42,
        owner="acme",
        name="other",
        default_branch="main",
        installation_id=1,
    )
    other_run, _ = await get_or_create_run(
        session,
        run_id=99,
        repo_id=42,
        head_sha="sha",
        head_branch="main",
        conclusion="failure",
        event="push",
    )
    await record_delivery(session, delivery_id="d2", repo_id=42, event="workflow_run")
    other_job = await enqueue(session, delivery_id="d2", repo_id=42, run_id=other_run.run_id)
    await add_reproductions(
        session,
        other_job.id,
        [
            _reproduction("tests/test_a.py::test_a", passed=False),
            _reproduction("tests/test_a.py::test_a", passed=False),
        ],
    )
    await add_reproductions(
        session,
        other_job_id,
        [_reproduction("tests/test_a.py::test_a", passed=True)],
    )

    rates = await flake_history(session, seeded_run.repo_id, ["tests/test_a.py::test_a"])

    assert rates == {"tests/test_a.py::test_a": 0.0}


async def test_flake_history_returns_entry_for_every_requested_id(
    session: AsyncSession, seeded_run: Run
) -> None:
    job_id = await _job_id(session, seeded_run)
    await add_reproductions(
        session,
        job_id,
        [_reproduction("tests/test_a.py::test_a", passed=False)],
    )

    rates = await flake_history(
        session,
        seeded_run.repo_id,
        ["tests/test_a.py::test_a", "tests/test_b.py::test_b"],
    )

    assert set(rates) == {"tests/test_a.py::test_a", "tests/test_b.py::test_b"}
    assert rates["tests/test_a.py::test_a"] == 1.0
    assert rates["tests/test_b.py::test_b"] == 0.0
