"""SQLAlchemy models.

GitHub's natural identifiers are primary keys where they are globally unique and stable
(``installation_id``, ``repo_id``, ``run_id``); internal tables use surrogate ids.

Ingest idempotency lives on ``deliveries`` (one row per ``X-GitHub-Delivery``); queue
state and leases live on ``jobs``. See docs/COMPONENTS.md and the backend spec §4.
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
    Index,
    Integer,
    MetaData,
    String,
    Text,
    func,
    text,
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


class Delivery(Base):
    """A webhook delivery, keyed on ``X-GitHub-Delivery``.

    The unique primary key plus ``INSERT ... ON CONFLICT DO NOTHING`` makes ingest
    idempotent: a redelivered event is recognised and enqueues nothing.
    """

    __tablename__ = "deliveries"

    delivery_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.repo_id", ondelete="CASCADE"), index=True
    )
    event: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    repository: Mapped[Repository] = relationship(back_populates="runs")
    jobs: Mapped[list[Job]] = relationship(back_populates="run")


class Job(Base):
    """A unit of work, and the durable queue row (backend spec §4.2).

    Lifecycle columns (``state``/``attempts``/``run_after``) and lease columns
    (``locked_by``/``lease_expires_at``) make the row the queue: claiming is a
    ``SELECT ... FOR UPDATE SKIP LOCKED`` that also stamps a lease, and a crashed worker
    is recovered by reaping an expired lease.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        # Coalesces concurrent duplicates: at most one active job per fingerprint.
        Index(
            "uq_jobs_active_fingerprint",
            "fingerprint",
            unique=True,
            postgresql_where=text("state IN ('queued','running')"),
            sqlite_where=text("state IN ('queued','running')"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    delivery_id: Mapped[str] = mapped_column(
        ForeignKey("deliveries.delivery_id", ondelete="CASCADE"), unique=True
    )
    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.repo_id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.run_id", ondelete="CASCADE"), index=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default=JobStatus.QUEUED.value, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_minutes: Mapped[float] = mapped_column(Float, default=0.0)
    llm_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
    """A runtime toggle, e.g. the global kill switch."""

    __tablename__ = "system_flags"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
