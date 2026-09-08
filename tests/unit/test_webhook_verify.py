"""HMAC-SHA256 webhook signature verification."""

from __future__ import annotations

from hashlib import sha256
from hmac import new as hmac_new

from janus.github.webhook_verify import verify_signature

SECRET = "topsecret"
BODY = b'{"action":"completed","run_id":1}'


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac_new(secret.encode(), body, sha256).hexdigest()


def test_valid_signature_passes() -> None:
    assert verify_signature(SECRET, BODY, _sign(SECRET, BODY)) is True


def test_tampered_body_fails() -> None:
    assert verify_signature(SECRET, b'{"action":"other"}', _sign(SECRET, BODY)) is False


def test_tampered_signature_fails() -> None:
    assert verify_signature(SECRET, BODY, "sha256=" + "0" * 64) is False


def test_missing_header_fails() -> None:
    assert verify_signature(SECRET, BODY, None) is False


def test_wrong_prefix_fails() -> None:
    assert verify_signature(SECRET, BODY, "sha1=" + "0" * 40) is False


def test_wrong_secret_fails() -> None:
    assert verify_signature("different", BODY, _sign(SECRET, BODY)) is False
