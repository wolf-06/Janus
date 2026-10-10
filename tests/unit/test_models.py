"""Schema shape for the Phase 4/5/6 persistence tables.

Pure metadata assertions: no database is touched, so the tables, columns, foreign keys
and relationships are pinned even before a migration is applied.
"""

from __future__ import annotations

from sqlalchemy import Integer

from janus.db.models import (
    Base,
    Fix,
    GateResult,
    Job,
    Reproduction,
    Run,
    Signal,
    Suspect,
)

NEW_TABLES = {"reproductions", "signals", "suspects", "fixes", "gate_results"}


def test_new_tables_are_registered() -> None:
    assert NEW_TABLES <= set(Base.metadata.tables)


def test_run_pr_number_is_a_nullable_integer() -> None:
    column = Run.__table__.c.pr_number
    assert isinstance(column.type, Integer)
    assert column.nullable is True


def test_reproduction_columns() -> None:
    assert set(Reproduction.__table__.c.keys()) == {
        "id",
        "job_id",
        "profile",
        "attempt_index",
        "target_id",
        "passed",
        "duration_s",
        "exit_code",
        "timed_out",
        "oom_killed",
        "cpu_seconds",
        "peak_mem_mb",
        "stdout_excerpt",
        "stderr_excerpt",
        "created_at",
    }
    assert Reproduction.__table__.c.target_id.nullable is True
    assert Reproduction.__table__.c.exit_code.nullable is True


def test_signal_columns() -> None:
    assert set(Signal.__table__.c.keys()) == {
        "id",
        "job_id",
        "name",
        "summary",
        "details",
        "created_at",
    }


def test_suspect_columns() -> None:
    assert set(Suspect.__table__.c.keys()) == {
        "id",
        "job_id",
        "file",
        "line",
        "score",
        "rank",
        "introducing_commit",
        "created_at",
    }
    assert Suspect.__table__.c.line.nullable is True
    assert Suspect.__table__.c.introducing_commit.nullable is True


def test_fix_columns() -> None:
    assert set(Fix.__table__.c.keys()) == {
        "id",
        "job_id",
        "suspect_id",
        "attempt",
        "diff",
        "status",
        "created_at",
    }
    assert Fix.__table__.c.suspect_id.nullable is True


def test_gate_result_columns() -> None:
    assert set(GateResult.__table__.c.keys()) == {
        "id",
        "fix_id",
        "gate",
        "passed",
        "measured_value",
        "baseline_value",
        "created_at",
    }
    assert GateResult.__table__.c.measured_value.nullable is True
    assert GateResult.__table__.c.baseline_value.nullable is True


def _fk_ondelete(table_name: str, column: str) -> str | None:
    table = Base.metadata.tables[table_name]
    fk = next(iter(table.c[column].foreign_keys))
    return fk.ondelete


def test_foreign_key_delete_rules() -> None:
    assert _fk_ondelete("reproductions", "job_id") == "CASCADE"
    assert _fk_ondelete("signals", "job_id") == "CASCADE"
    assert _fk_ondelete("suspects", "job_id") == "CASCADE"
    assert _fk_ondelete("fixes", "job_id") == "CASCADE"
    assert _fk_ondelete("fixes", "suspect_id") == "SET NULL"
    assert _fk_ondelete("gate_results", "fix_id") == "CASCADE"


def test_job_has_phase_4_6_relationships() -> None:
    relationships = set(Job.__mapper__.relationships.keys())
    assert {"reproductions", "signals", "suspects", "fixes"} <= relationships
