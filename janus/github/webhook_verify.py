"""GitHub webhook signature verification.

GitHub signs the **raw request body** with HMAC-SHA256 and sends the digest in the
``X-Hub-Signature-256`` header as ``sha256=<hex>``. Verification must run over the exact
bytes GitHub signed — re-serialising parsed JSON would change whitespace and break the
digest — and must use a constant-time comparison so a timing side channel cannot leak
the expected digest.
"""

from __future__ import annotations

import hmac
from hashlib import sha256

_HEADER_PREFIX = "sha256="


def verify_signature(secret: str | bytes, body: bytes, header: str | None) -> bool:
    """Return ``True`` iff ``header`` is a valid HMAC-SHA256 of ``body``.

    Never raises: a missing, malformed or mismatched header is simply ``False``.
    """
    if not secret or not header or not header.startswith(_HEADER_PREFIX):
        return False

    provided = header[len(_HEADER_PREFIX) :]
    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
    expected = hmac.new(secret_bytes, body, sha256).hexdigest()
    return hmac.compare_digest(expected, provided)
