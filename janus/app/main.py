"""FastAPI application factory.

Using a factory (``create_app``) keeps import side effects out of tests and lets the
ASGI entrypoint be a single module-level ``app``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from janus.app.api import admin, health, manifest, webhooks
from janus.app.config import get_settings
from janus.app.logging import configure_logging
from janus.db.session import get_engine


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Configure logging and construct the engine on startup; dispose on shutdown."""
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = get_engine()
    try:
        yield
    finally:
        await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="Janus", version="0.0.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(webhooks.router)
    app.include_router(manifest.router)
    app.include_router(admin.router)
    return app


app = create_app()
