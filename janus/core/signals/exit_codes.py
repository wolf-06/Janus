"""The **exit_codes** signal: the coarse taxonomy behind the signature library.

The process exit code is a cheap, robust hint about why a run ended. ``137`` (128 + ``SIGKILL``)
and ``124`` (GNU ``timeout``'s code) indicate the sandbox killed the run — OOM or wall-clock —
and point at :attr:`~janus.core.triage.routes.Route.ENV`. Codes ``1`` and ``2`` are ordinary
test-runner failures and point at :attr:`~janus.core.triage.routes.Route.CODE_BUG`. A ``None``
exit code means the sandbox never produced one (an infrastructure error), which is not
evidence about the code, so those attempts are ignored; when no attempt carries a classifiable
code the collector abstains.
"""

from __future__ import annotations

from typing import Any, Final

from janus.core.signals.base import SignalContext, SignalEvidence, clamp_votes
from janus.core.triage.routes import Route

_ENV_CODES: Final[frozenset[int]] = frozenset({137, 124})
"""Exit codes that indicate the sandbox killed the run (OOM / timeout) → ``ENV``."""

_CODE_BUG_CODES: Final[frozenset[int]] = frozenset({1, 2})
"""Ordinary test-runner failure codes → ``CODE_BUG``."""


class ExitCodesCollector:
    """Tally exit codes across attempts and vote on the dominant class."""

    name = "exit_codes"

    def collect(self, ctx: SignalContext) -> SignalEvidence | None:
        tally: dict[int, int] = {}
        env_hits = 0
        code_bug_hits = 0
        for attempt in ctx.reproduction.attempts:
            code = attempt.exit_code
            if code is None:
                continue
            tally[code] = tally.get(code, 0) + 1
            if code in _ENV_CODES:
                env_hits += 1
            elif code in _CODE_BUG_CODES:
                code_bug_hits += 1

        if env_hits == 0 and code_bug_hits == 0:
            return None

        votes: dict[Route, float] = {}
        if env_hits:
            votes[Route.ENV] = 0.8
        if code_bug_hits:
            votes[Route.CODE_BUG] = 0.6

        if env_hits >= code_bug_hits:
            summary = f"{env_hits} attempt(s) ended with a kill/timeout exit code"
        else:
            summary = f"{code_bug_hits} attempt(s) ended with a normal failure exit code"

        details: dict[str, Any] = {
            "exit_codes": {str(code): count for code, count in sorted(tally.items())},
            "env_hits": env_hits,
            "code_bug_hits": code_bug_hits,
        }
        return SignalEvidence(
            name=self.name,
            summary=summary,
            details=details,
            votes=clamp_votes(votes),
        )
