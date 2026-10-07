"""Secret redaction.

Logs and command output can contain credentials. Everything that leaves the sandbox —
stored rows, LLM prompts, comments — must pass through :func:`redact` first.

Independent checks run in order, cheapest and most certain first:

1. PEM blocks (private keys).
2. Known token shapes (GitHub, AWS, Slack, Stripe, Google, npm, HuggingFace, Docker,
   PyPI, SSH key material, JWTs, ``user:pass@host``).
3. ``Authorization: Bearer`` / ``Authorization: Basic`` credentials — the scheme word is
   kept, only the credential is redacted.
4. ``KEY=value`` lines whose key name looks secret-ish (including quoted JSON keys).
5. Long high-entropy strings, which are almost always secrets.
6. High-entropy path *segments* — a machine-generated segment inside a path is redacted
   while the path itself (and its ``/`` separators) survives.

Filesystem paths are deliberately left intact: they are ubiquitous in tracebacks and
coverage output, and the triage stage depends on them. Pass 6 narrows that promise to the
path *structure*: the separators and ordinary segments survive, but a single long
random-looking segment is treated as a secret and replaced.

Redaction is best-effort: it reduces leakage, it does not guarantee it.
"""

from __future__ import annotations

import math
import re

REDACTED = "[REDACTED]"

_PEM_RE = re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.DOTALL)

_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bxapp-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bdckr_pat_[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\bssh-(?:rsa|ed25519) [A-Za-z0-9+/=]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b[A-Za-z0-9._%+-]+:[^\s/@]+@[A-Za-z0-9.-]+\b"),
)

_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{16,}")

# ``Authorization: Basic`` carries base64 ``user:password``; keep the scheme word, redact
# only the credential. Base64 uses ``+``, ``/`` and ``=`` padding.
_BASIC_RE = re.compile(r"(?i)\b(basic\s+)[A-Za-z0-9+/=]{8,}")

_SECRET_KEY_RE = re.compile(
    r"(?i)\b("
    r"[A-Z0-9_.\-]*"
    r"(?:SECRET|TOKEN|PASSWORD|PASSWD|API[_-]?KEY|PRIVATE[_-]?KEY|ACCESS[_-]?KEY)"
    r"[A-Z0-9_.\-]*"
    r")"
    # An optional closing quote so structured forms like ``"password": "…"`` are caught.
    r"([\"']?\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)

_ENTROPY_RE = re.compile(r"[A-Za-z0-9+/=_\-]{24,}")

# Path-shaped runs. Only characters that can legitimately appear in a filesystem path are
# consumed, so trailing prose punctuation (quotes, commas, brackets) is not swallowed.
_PATH_RE = re.compile(r"[A-Za-z0-9._~@%+=:/-]+")

_MIN_ENTROPY_LENGTH = 24
_ENTROPY_THRESHOLD = 3.7


def _shannon_entropy(value: str) -> float:
    """Bits of entropy per character of ``value``."""
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _looks_random(token: str) -> bool:
    """Heuristic for "this is a key, not a word".

    Requires enough length and entropy *and* a sign of machine generation — a digit or
    mixed case — so long snake_case identifiers are left alone.
    """
    if len(token) < _MIN_ENTROPY_LENGTH:
        return False
    if _shannon_entropy(token) < _ENTROPY_THRESHOLD:
        return False
    has_digit = any(char.isdigit() for char in token)
    mixed_case = token.lower() != token and token.upper() != token
    return has_digit or mixed_case


def _looks_like_path(token: str) -> bool:
    """True for filesystem paths, which must survive redaction.

    Tracebacks and coverage output are full of paths, and redacting them destroys the
    evidence the triage stage needs. A path is a ``/``-containing token; base64 secrets
    usually also carry ``+`` or ``=``, which paths do not.
    """
    if "/" not in token:
        return False
    return "+" not in token and "=" not in token


def _redact_if_random(match: re.Match[str]) -> str:
    token = match.group(0)
    if _looks_like_path(token):
        return token
    return REDACTED if _looks_random(token) else token


def _redact_path_segments(match: re.Match[str]) -> str:
    """Redact only the random-looking segments of a path, preserving its structure.

    The separators and ordinary segments are evidence and survive; a single long
    high-entropy segment (e.g. a signed URL component) is replaced in place.
    """
    token = match.group(0)
    if "/" not in token:
        return token
    return "/".join(REDACTED if _looks_random(segment) else segment for segment in token.split("/"))


def redact(text: str) -> str:
    """Return ``text`` with secrets replaced by ``[REDACTED]``."""
    if not text:
        return text
    result = _PEM_RE.sub(REDACTED, text)
    for pattern in _TOKEN_PATTERNS:
        result = pattern.sub(REDACTED, result)
    result = _BEARER_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", result)
    result = _BASIC_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", result)
    result = _SECRET_KEY_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", result)
    result = _ENTROPY_RE.sub(_redact_if_random, result)
    return _PATH_RE.sub(_redact_path_segments, result)


def redact_bytes(data: bytes) -> str:
    """Decode ``data`` leniently and redact it."""
    return redact(data.decode("utf-8", errors="replace"))
