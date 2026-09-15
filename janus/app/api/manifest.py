"""GitHub App manifest flow endpoints.

``/manifest/start`` renders a self-submitting form because GitHub's manifest creation
endpoint expects the manifest as a **POST** field (a plain 302 cannot carry a body).
``/manifest/callback`` receives the temporary ``code`` GitHub redirects back with and
persists the resulting credentials.
"""

from __future__ import annotations

import html
import json
import secrets
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from janus.app.config import get_settings
from janus.github.manifest import (
    build_manifest,
    exchange_manifest_code,
    persist_credentials,
)

router = APIRouter(prefix="/manifest", tags=["manifest"])

_GITHUB_NEW_APP_URL = "https://github.com/settings/apps/new"
_DEFAULT_KEY_PATH = "janus-app.pem"
_ENV_PATH = ".env"


def _base_url(request: Request) -> str:
    settings = get_settings()
    if settings.public_url:
        return settings.public_url.rstrip("/")
    return str(request.base_url).rstrip("/")


@router.get("/start", response_class=HTMLResponse)
def manifest_start(request: Request) -> HTMLResponse:
    """Render an auto-submitting form that creates the App on GitHub."""
    settings = get_settings()
    base = _base_url(request)
    manifest = build_manifest(
        name=settings.github_app_name,
        redirect_url=f"{base}/manifest/callback",
        homepage_url=base,
        webhook_url=f"{base}/webhooks/github",
    )
    state = secrets.token_urlsafe(16)
    manifest_value = html.escape(json.dumps(manifest))
    page = (
        "<!doctype html><html><body>"
        f'<form id="janus-manifest" action="{_GITHUB_NEW_APP_URL}?state={state}" '
        'method="post">'
        f'<input type="hidden" name="manifest" value="{manifest_value}">'
        "</form>"
        "<script>document.getElementById('janus-manifest').submit()</script>"
        "</body></html>"
    )
    return HTMLResponse(page)


@router.get("/callback", name="manifest_callback")
def manifest_callback(code: Annotated[str, Query()]) -> RedirectResponse:
    """Exchange the manifest code for credentials and persist them."""
    settings = get_settings()
    credentials = exchange_manifest_code(code)
    persist_credentials(
        credentials,
        private_key_path=settings.github_private_key_path or _DEFAULT_KEY_PATH,
        env_path=_ENV_PATH,
    )
    return RedirectResponse(url="/manifest/success", status_code=303)


@router.get("/success", response_class=HTMLResponse)
def manifest_success() -> HTMLResponse:
    return HTMLResponse(
        "<h1>Janus installed</h1><p>Credentials stored. You can close this window.</p>"
    )
