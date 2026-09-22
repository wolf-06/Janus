"""SQLAlchemy models for the S0 spine.

GitHub's natural identifiers are used as primary keys (``installation_id``,
``repo_id``, ``run_id``): they are globally unique and stable, so they double as the
idempotency key for repeated webhook deliveries. Internal tables (``jobs``,
``diagnoses``) use surrogate autoincrement ids.

Only the tables S0 needs are defined here; the full model in
``docs/IMPLEMENTATION_PLAN.md`` §1.4 grows in later phases.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from janus.jobs.state import JobStatus

# Deterministic constraint names keep Alembic autogenerate stable across runs.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Installation(Base):
    """A GitHub App installation on an account."""

    __tablename__ = "installations"

    installation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    account: Mapped[str] = mapped_column(String(255))
    app_id: Mapped[int] = mapped_column(BigInteger)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    repositories: Mapped[list[Repository]] = relationship(back_populates="installation")


class Repository(Base):
    """A tracked repository plus its cached per-repo config."""

    __tablename__ = "repositories"

    repo_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    owner: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    default_branch: Mapped[str] = mapped_column(String(255), default="main")
    config_cache: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    installation_id: Mapped[int] = mapped_column(
        ForeignKey("installations.installation_id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    installation: Mapped[Installation] = relationship(back_populates="repositories")
    runs: Mapped[list[Run]] = relationship(back_populates="repository")


class Run(Base):
    """An observed workflow run, with the baseline used for the fault diff."""

    __tablename__ = "runs"

    run_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.repo_id", ondelete="CASCADE"), index=True
    )
    head_sha: Mapped[str] = mapped_column(String(64))
    head_branch: Mapped[str] = mapped_column(String(255))
    conclusion: Mapped[str] = mapped_column(String(32))
    baseline_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event: Mapped[str] = mapped_column(String(64))
    # Unique per webhook delivery: a redelivered event must not create a second run/job.
    delivery_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    repository: Mapped[Repository] = relationship(back_populates="runs")
    jobs: Mapped[list[Job]] = relationship(back_populates="run")


class Job(Base):
    """A unit of work for the orchestrator."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.run_id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.QUEUED.value)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    cost_minutes: Mapped[float] = mapped_column(Float, default=0.0)
    llm_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # Deduplicates enqueue: one job per key (defaults to the run id). A redelivered
    # webhook or a re-enqueue returns the existing job instead of creating a second.
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    # Earliest time a worker may claim this job; set by exponential backoff on retry.
    run_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    run: Mapped[Run] = relationship(back_populates="jobs")
    diagnoses: Mapped[list[Diagnosis]] = relationship(back_populates="job")


class Diagnosis(Base):
    """The triage outcome for a job (route + confidence + evidence)."""

    __tablename__ = "diagnoses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    route: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column(Float)
    top2_gap: Mapped[float] = mapped_column(Float)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    job: Mapped[Job] = relationship(back_populates="diagnoses")


class SystemFlag(Base):
    """A runtime toggle, e.g. the global kill switch.

    Stored in the database (not only env) so it can be flipped while workers run and
    take effect on the next claim, without a redeploy.
    """

    __tablename__ = "system_flags"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
