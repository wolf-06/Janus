"""SQLAlchemy declarative base.

Concrete tables (``installations``, ``repositories``, ``runs``, ``jobs``,
``diagnoses``) are added in Phase 1. Alembic autogenerate and ``create_all`` both
target ``Base.metadata``.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model."""
