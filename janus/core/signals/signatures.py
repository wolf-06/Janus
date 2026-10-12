"""The **signatures** signal: known infrastructure and assertion strings in the run output.

The reproducer captures bounded, redacted stdout/stderr excerpts. This collector scans them
for a small library of well-known failure signatures: timeouts, connection resets, HTTP
``429``/``503``, disk exhaustion, OOM kills, ``SIGKILL`` and DNS resolver failures — all of
which point at :attr:`~janus.core.triage.routes.Route.ENV` — plus assertion-style failures,
which point at :attr:`~janus.core.triage.routes.Route.CODE_BUG`. It abstains when nothing
matched, so a quiet excerpt never counts as evidence.
"""

from __future__ import annotations

import re
from typing import Any, Final

from janus.core.signals.base import SignalContext, SignalEvidence, clamp_votes
from janus.core.triage.routes import Route

_INFRA_SIGNATURES: Final[tuple[tuple[str, str], ...]] = (
    ("timeout", r"Timeout|timed out"),
    ("connection", r"ECONNRESET|ECONNREFUSED|ETIMEDOUT"),
    ("http_429_503", r"429|503"),
    ("disk_full", r"ENOSPC|no space left"),
    ("oom", r"OOMKilled|out of memory"),
    ("sigkill", r"SIGKILL|signal 9"),
    ("dns", r"Temporary failure in name resolution|Name or service not known"),
)
"""Infrastructure signatures → ``ENV``. Each entry is ``(key, regex)``."""

_ASSERTION_SIGNATURE: Final[re.Pattern[str]] = re.compile(r"AssertionError|assert\b")

_INFRA_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (key, re.compile(pattern, re.IGNORECASE)) for key, pattern in _INFRA_SIGNATURES
)


class SignaturesCollector:
    """Vote ``ENV`` for infra signatures, ``CODE_BUG`` for assertion-style failures."""

    name = "signatures"

    def collect(self, ctx: SignalContext) -> SignalEvidence | None:
        attempts = ctx.reproduction.attempts
        if not attempts:
            return None

        matched: dict[str, int] = {}
        assertion_attempts = 0
        for attempt in attempts:
            haystack = f"{attempt.stdout_excerpt}\n{attempt.stderr_excerpt}"
            for key, pattern in _INFRA_PATTERNS:
                if pattern.search(haystack):
                    matched[key] = matched.get(key, 0) + 1
            if _ASSERTION_SIGNATURE.search(haystack):
                assertion_attempts += 1

        if not matched and not assertion_attempts:
            return None

        votes: dict[Route, float] = {}
        if matched:
            votes[Route.ENV] = 0.85
        if assertion_attempts:
            votes[Route.CODE_BUG] = 0.6

        parts: list[str] = []
        if matched:
            parts.append("infra signatures: " + ", ".join(sorted(matched)))
        if assertion_attempts:
            parts.append(f"assertion failure in {assertion_attempts} attempt(s)")
        summary = "matched " + "; ".join(parts)

        details: dict[str, Any] = {
            "attempts": len(attempts),
            "infra_matches": dict(sorted(matched.items())),
            "assertion_attempts": assertion_attempts,
        }
        return SignalEvidence(
            name=self.name,
            summary=summary,
            details=details,
            votes=clamp_votes(votes),
        )
