"""GitHub App authentication.

Two-step auth, by design:

1. The App proves its identity to GitHub with a short-lived **RS256 JWT** signed by its
   private key (``app_jwt``). GitHub caps this JWT at 10 minutes, so we mint ~9.
2. The App exchanges that JWT for a **scoped installation token** (``installation_token``)
   which is what actually calls the API. Installation tokens expire (~1 hour) and are
   cached in memory until shortly before expiry.

Security note (``PLAN.md`` §4): installation tokens are **never** placed in clone URLs;
consumers must use a credential helper. This module only returns the token.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
import jwt

from janus.app.config import Settings

GITHUB_API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"
_JWT_TTL_SECONDS = 540  # 9 minutes, below GitHub's 10-minute maximum.
_CLOCK_SKEW_SECONDS = 60
_TOKEN_REFRESH_SKEW_SECONDS = 60


@dataclass(frozen=True)
class _CachedToken:
    token: str
    expires_at: float


_token_cache: dict[int, _CachedToken] = {}


def clear_token_cache() -> None:
    """Drop all cached installation tokens (used by tests)."""
    _token_cache.clear()


def app_jwt(settings: Settings, *, now: int | None = None) -> str:
    """Return a signed RS256 App JWT valid for ~9 minutes."""
    if settings.github_app_id is None:
        raise ValueError("github_app_id is not configured")
    if settings.github_private_key_path is None:
        raise ValueError("github_private_key_path is not configured")

    issued_at = now if now is not None else int(time.time())
    payload = {
        "iat": issued_at - _CLOCK_SKEW_SECONDS,
        "exp": issued_at + _JWT_TTL_SECONDS,
        "iss": str(settings.github_app_id),
    }
    private_key = Path(settings.github_private_key_path).read_text(encoding="utf-8")
    return jwt.encode(payload, private_key, algorithm="RS256")


def _parse_expires_at(value: str | None, now_ts: float) -> float:
    if not value:
        return now_ts + 3600.0
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def installation_token(
    settings: Settings,
    installation_id: int,
    *,
    client: httpx.Client | None = None,
    now: float | None = None,
) -> str:
    """Return a valid installation token, minting and caching one if needed."""
    now_ts = now if now is not None else time.time()

    cached = _token_cache.get(installation_id)
    if cached is not None and cached.expires_at - _TOKEN_REFRESH_SKEW_SECONDS > now_ts:
        return cached.token

    headers = {
        "Authorization": f"Bearer {app_jwt(settings, now=int(now_ts))}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
    }
    url = f"{GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens"

    owns_client = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        response = http.post(url, headers=headers)
        response.raise_for_status()
        data = response.json()
    finally:
        if owns_client:
            http.close()

    token = str(data["token"])
    expires_at = _parse_expires_at(data.get("expires_at"), now_ts)
    _token_cache[installation_id] = _CachedToken(token=token, expires_at=expires_at)
    return token
