"""The evidence renderer: a decision plus its evidence → Check-run payload + Markdown.

Per ``PLAN.md`` §4 ("no claim without a gate") this module states **only measured facts**. It
renders the per-attempt rerun results, the full-suite run, every signal's summary and votes,
and the rule's reasons — nothing it cannot back up with a :class:`Reproduction`,
:class:`ChangeSummary` or :class:`SignalEvidence`. When the baseline could not be resolved it
says so rather than implying nothing changed.

Everything here is pure and total: no network, no database, no clock. The Markdown is bounded
by :data:`MAX_COMMENT_CHARS` with a truncation note, so a hostile or noisy input can never
produce an unbounded comment.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, Final

from janus.core.evidence import ChangeSummary, Reproduction, RunAttempt
from janus.core.signals.base import SignalEvidence
from janus.core.triage.routes import TRIAGE_ROUTES, Route, TriageDecision

MAX_COMMENT_CHARS: Final[int] = 6000
"""Hard cap on the rendered Markdown. Beyond this the comment is truncated with a note."""

_TRUNCATION_NOTE: Final[str] = (
    "\n\n_Comment truncated to fit the limit; the Check run carries the full evidence._"
)

_NEXT_STEP: Final[Mapping[Route, str]] = MappingProxyType(
    {
        Route.CODE_BUG: "Janus will proceed to a code fix (localize, patch, prove); this is S2.",
        Route.FLAKY: "Comment only: this looks like a flaky test; Janus will not change code.",
        Route.ENV: "Comment only: env/pipeline fault; Janus will not change code.",
        Route.UNSURE: "Comment only: the evidence is inconclusive; Janus will not change code.",
    }
)
"""The per-route "next step" line. Only ``CODE_BUG`` proceeds; the rest are comment-only."""

_CONCLUSION: Final[Mapping[Route, str]] = MappingProxyType(
    {
        Route.CODE_BUG: "action_required",
        Route.FLAKY: "neutral",
        Route.ENV: "neutral",
        Route.UNSURE: "neutral",
    }
)
"""Check-run ``conclusion`` per route: only ``CODE_BUG`` asks a human to act."""


def check_conclusion(route: Route) -> str:
    """Map a route to its Check-run ``conclusion``.

    ``CODE_BUG`` → ``"action_required"``; ``FLAKY``, ``ENV`` and ``UNSURE`` → ``"neutral"``.
    Total over every :class:`Route`.
    """
    return _CONCLUSION[route]


def _percent(value: float) -> int:
    """Render a ``[0, 1]`` confidence as a whole percentage."""
    return round(min(1.0, max(0.0, value)) * 100)


def _heading(decision: TriageDecision) -> str:
    """The one-line route/confidence heading, e.g. ``**CODE_BUG** — 87% confidence (…)``."""
    return (
        f"**{decision.route.value}** — {_percent(decision.confidence)}% confidence "
        f"(top-2 gap {decision.top2_gap:.2f})"
    )


def _cell(value: str) -> str:
    """Collapse whitespace and escape pipes so a value cannot break a Markdown table."""
    return " ".join(value.split()).replace("|", "\\|")


def _vote(votes: Mapping[Route, float], route: Route) -> float:
    """Read one route's vote for display, defaulting to ``0`` and clamping into ``[0, 1]``."""
    return min(1.0, max(0.0, float(votes.get(route, 0.0))))


def _attempt_row(attempt: RunAttempt) -> str:
    target = _cell(attempt.target_id) if attempt.target_id is not None else "-"
    result = "passed" if attempt.passed else "failed"
    exit_code = "-" if attempt.exit_code is None else str(attempt.exit_code)
    timed_out = "yes" if attempt.timed_out else "no"
    return (
        f"| {attempt.index} | {target} | {result} | {attempt.duration_s:.2f} "
        f"| {exit_code} | {timed_out} |"
    )


def _full_suite_line(attempt: RunAttempt | None) -> str:
    if attempt is None:
        return "Full suite: not run."
    result = "passed" if attempt.passed else "failed"
    exit_code = "-" if attempt.exit_code is None else str(attempt.exit_code)
    return f"Full suite {result} in {attempt.duration_s:.2f} s (exit {exit_code})."


def _signal_row(evidence: SignalEvidence) -> str:
    votes = " | ".join(f"{_vote(evidence.votes, route):.2f}" for route in TRIAGE_ROUTES)
    return f"| {_cell(evidence.name)} | {_cell(evidence.summary)} | {votes} |"


def _bound(text: str, limit: int = MAX_COMMENT_CHARS) -> str:
    """Truncate ``text`` to ``limit`` characters, appending a truncation note when cut."""
    if len(text) <= limit:
        return text
    keep = limit - len(_TRUNCATION_NOTE)
    if keep <= 0:
        return text[:limit]
    return text[:keep].rstrip() + _TRUNCATION_NOTE


def _render_body(
    *,
    decision: TriageDecision,
    reproduction: Reproduction,
    change: ChangeSummary | None,
    evidence: Sequence[SignalEvidence],
    failing_targets: Sequence[str],
    run_url: str | None,
) -> str:
    lines: list[str] = ["## Janus triage evidence", "", _heading(decision), ""]
    if run_url:
        lines.extend([f"Run: {run_url}", ""])
    if failing_targets:
        lines.extend(["Failing targets: " + ", ".join(failing_targets), ""])

    lines.extend(["### Attempts", ""])
    lines.append("| # | target | result | duration (s) | exit | timed out |")
    lines.append("|---|---|---|---|---|---|")
    if reproduction.attempts:
        lines.extend(_attempt_row(attempt) for attempt in reproduction.attempts)
    else:
        lines.append("| - | - | no reruns recorded | - | - | - |")
    lines.append("")

    lines.extend(["### Full suite", "", _full_suite_line(reproduction.full_suite), ""])

    if change is not None:
        lines.extend(["### Change attribution", ""])
        if change.available:
            lines.append(
                f"Baseline {change.base_sha} to head {change.head_sha}: "
                f"{len(change.files_changed)} file(s) changed."
            )
        else:
            lines.append(
                "The baseline could not be resolved (no last-green run on this branch), "
                "so change attribution is unavailable."
            )
        lines.append("")

    lines.extend(["### Signals", ""])
    header = "| signal | summary | " + " | ".join(route.value for route in TRIAGE_ROUTES) + " |"
    separator = "|---|---|" + "---|" * len(TRIAGE_ROUTES)
    lines.append(header)
    lines.append(separator)
    if evidence:
        lines.extend(_signal_row(item) for item in evidence)
    else:
        empty_votes = " | ".join("-" for _ in TRIAGE_ROUTES)
        lines.append(f"| - | no signal evidence | {empty_votes} |")
    lines.append("")

    lines.extend(["### Why", ""])
    if decision.reasons:
        lines.extend(f"- {reason}" for reason in decision.reasons)
    else:
        lines.append("- (no reasons recorded)")
    lines.append("")

    lines.extend(["### Next step", "", _NEXT_STEP[decision.route]])
    return _bound("\n".join(lines))


def render_evidence(
    *,
    decision: TriageDecision,
    reproduction: Reproduction,
    change: ChangeSummary,
    evidence: Sequence[SignalEvidence],
    failing_targets: Sequence[str],
    run_url: str | None = None,
) -> str:
    """Render the full Markdown evidence comment for a triage decision.

    Contains the route/confidence heading, the per-attempt results table, the full-suite
    result, the signal table, the reasons, a per-route "next step" line, and — when
    ``change.available`` is ``False`` — an explicit note that the baseline could not be
    resolved. Bounded by :data:`MAX_COMMENT_CHARS`.
    """
    return _render_body(
        decision=decision,
        reproduction=reproduction,
        change=change,
        evidence=evidence,
        failing_targets=failing_targets,
        run_url=run_url,
    )


def render_check_output(
    *,
    decision: TriageDecision,
    reproduction: Reproduction,
    evidence: Sequence[SignalEvidence],
    run_url: str | None = None,
) -> dict[str, Any]:
    """Build the Check-run ``output`` payload: ``title``, one-line ``summary`` and bounded ``text``.

    The Markdown is the same measured-facts body as :func:`render_evidence` minus the
    change-attribution section, which this payload has no :class:`ChangeSummary` for.
    """
    text = _render_body(
        decision=decision,
        reproduction=reproduction,
        change=None,
        evidence=evidence,
        failing_targets=(),
        run_url=run_url,
    )
    return {
        "title": f"Janus triage: {decision.route.value}",
        "summary": _heading(decision),
        "text": text,
    }
