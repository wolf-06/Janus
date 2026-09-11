"""GitHub App JWT minting and installation-token caching."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from janus.app.config import Settings
from janus.github.app_auth import (
    GITHUB_API_BASE,
    app_jwt,
    clear_token_cache,
    installation_token,
)


@pytest.fixture(autouse=True)
def _reset_token_cache() -> Iterator[None]:
    clear_token_cache()
    yield
    clear_token_cache()


@pytest.fixture
def private_key(tmp_path: Path) -> tuple[Path, Any]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "app.pem"
    path.write_bytes(pem)
    return path, key.public_key()


@pytest.fixture
def settings(private_key: tuple[Path, Any]) -> Settings:
    path, _ = private_key
    return Settings(
        _env_file=None,
        github_app_id=123,
        github_private_key_path=str(path),
    )


def test_app_jwt_is_decodable_and_under_ten_minutes(
    settings: Settings, private_key: tuple[Path, Any]
) -> None:
    _, public_key = private_key
    token = app_jwt(settings, now=1_000_000)

    claims = jwt.decode(
        token,
        public_key,
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )

    assert claims["iss"] == "123"
    assert claims["exp"] - claims["iat"] <= 600


def test_app_jwt_requires_configuration() -> None:
    with pytest.raises(ValueError):
        app_jwt(Settings(_env_file=None))


@respx.mock
def test_installation_token_mints_then_caches(settings: Settings) -> None:
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    route = respx.post(f"{GITHUB_API_BASE}/app/installations/42/access_tokens").mock(
        return_value=httpx.Response(200, json={"token": "ghs_abc", "expires_at": expires})
    )

    first = installation_token(settings, 42, now=1000.0)
    second = installation_token(settings, 42, now=1000.0)

    assert first == "ghs_abc"
    assert second == "ghs_abc"
    assert route.call_count == 1


@respx.mock
def test_installation_token_refreshes_when_expired(settings: Settings) -> None:
    # Expires at t=1500, so it is valid at now=1000 but stale at now=2000.
    expires = datetime.fromtimestamp(1500, tz=UTC).isoformat().replace("+00:00", "Z")
    route = respx.post(f"{GITHUB_API_BASE}/app/installations/7/access_tokens").mock(
        return_value=httpx.Response(200, json={"token": "ghs_new", "expires_at": expires})
    )

    installation_token(settings, 7, now=1000.0)
    installation_token(settings, 7, now=2000.0)

    assert route.call_count == 2
