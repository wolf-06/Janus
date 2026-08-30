"""Liveness endpoint."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """Return a static ok payload.

    Deliberately does not touch the database: liveness must stay green even if a
    dependency is degraded, so it only proves the process is serving requests.
    """
    return {"status": "ok"}
