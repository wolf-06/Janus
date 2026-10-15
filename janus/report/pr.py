"""The pull-request and suggest-mode surfaces.

A fix reaches a human through one of two surfaces: a PR body (``mode: pr``) or a comment
(``mode: suggest``, or any failed proof). Both tell the same story — diagnosis, ranked
suspects, the proposed diff, the four measured gates and the static policy gate — and both
are bounded so a noisy diff or a long evidence list can never produce an unbounded payload.

Per ``PLAN.md`` §4 this module is **PR-only**: it renders text, never merges, never
force-pushes and never claims a result that is not in ``gates``. ``render_suggest_comment``
exists precisely so a failed proof still has an honest surface: it says what was found and
why no PR was opened.

Everything here is pure and total: no network, no database, no clock, and empty inputs render
a valid (if sparse) document.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from janus.core.evidence import ChangeSummary, Reproduction
from janus.core.prove.gates import GateResult, all_passed
from janus.core.prove.policy import PolicyViolation
from janus.core.triage.routes import Route, TriageDecision

MAX_PR_BODY_CHARS: Final[int] = 20000
"""Hard cap on a PR body or suggest comment. Beyond this a truncation note is appended."""

_TRUNCATION_NOTE: Final[str] = "\n\n_Truncated to fit the size limit._"

_TITLE_LIMIT: Final[int] = 120


def _percent(value: float) -> int:
    return round(min(1.0, max(0.0, value)) * 100)


def _cell(value: str) -> str:
    return " ".join(value.split()).replace("|", "\\|")


def _bound(text: str, limit: int = MAX_PR_BODY_CHARS) -> str:
    if len(text) <= limit:
        return text
    keep = limit - len(_TRUNCATION_NOTE)
    if keep <= 0:
        return text[:limit]
    return text[:keep].rstrip() + _TRUNCATION_NOTE


def _field(suspect: object, name: str) -> Any:
    if isinstance(suspect, Mapping):
        return suspect.get(name)
    return getattr(suspect, name, None)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _suspect_location(suspect: object) -> str:
    path = _text(_field(suspect, "file"), "unknown")
    line = _integer(_field(suspect, "line"))
    if line is None:
        return path
    return f"{path}:{line}"


def _suspect_line(suspect: object) -> str:
    rank = _integer(_field(suspect, "rank"))
    prefix = f"{rank}. " if rank is not None else "- "
    details: list[str] = []
    score = _number(_field(suspect, "score"))
    if score is not None:
        details.append(f"score {score:.2f}")
    commit = _text(_field(suspect, "introducing_commit"))
    if commit:
        details.append(f"commit {commit}")
    suffix = f" ({', '.join(details)})" if details else ""
    return f"- {prefix}{_cell(_suspect_location(suspect))}{suffix}"


def _suspect_lines(suspects: Sequence[object]) -> list[str]:
    if not suspects:
        return ["- (no suspects recorded)"]
    return [_suspect_line(suspect) for suspect in suspects]


def _first_location(suspects: Sequence[object]) -> str | None:
    if not suspects:
        return None
    location = _suspect_location(suspects[0])
    if not location or location == "unknown":
        return None
    return location


def _diagnosis_lines(decision: TriageDecision) -> list[str]:
    return [
        f"**Route:** `{decision.route.value}`",
        "",
        f"**Confidence:** {_percent(decision.confidence)}% (top-2 gap {decision.top2_gap:.2f})",
        "",
    ]


def _reproduction_line(reproduction: Reproduction) -> str:
    total = len(reproduction.attempts)
    return (
        f"Reproduction: {reproduction.profile.value} profile, "
        f"{reproduction.failures} failed of {total} rerun(s)."
    )


def _change_lines(change: ChangeSummary) -> list[str]:
    if not change.available:
        return ["Change baseline: unavailable (no last-green run)."]
    base = change.base_sha or "?"
    return [
        f"Change range: `{base[:12]}` to `{change.head_sha[:12]}` "
        f"({len(change.files_changed)} file(s))."
    ]


def _gate_table(gates: Sequence[GateResult]) -> list[str]:
    lines = [
        "| gate | passed | measured | baseline |",
        "|---|---|---|---|",
    ]
    if not gates:
        lines.append("| - | - | no gates recorded | - |")
        return lines
    for gate in gates:
        lines.append(
            f"| {_cell(gate.gate)} | {'pass' if gate.passed else 'fail'} "
            f"| {_cell(gate.measured_value or '-')} "
            f"| {_cell(gate.baseline_value or '-')} |"
        )
    return lines


def _policy_lines(violations: Sequence[PolicyViolation]) -> list[str]:
    if not violations:
        return ["Policy gate passed: no violations."]
    lines = [f"Policy gate failed: {len(violations)} violation(s)."]
    lines.extend(
        f"- **{_cell(violation.rule)}**: {_cell(violation.detail)}" for violation in violations
    )
    return lines


def _fenced_diff(diff: str) -> list[str]:
    body = diff.rstrip("\n")
    if not body:
        body = "(no diff produced)"
    fence = "```"
    while fence in body:
        fence += "`"
    return [f"{fence}diff", body, fence]


def branch_name(job_id: int) -> str:
    """Return the deterministic branch name for a fix job, e.g. ``janus/fix-42``.

    Only the integer is interpolated, so the result is filesystem- and ref-safe for any
    ``int`` input.
    """
    return f"janus/fix-{job_id}"


def render_pr_body(
    *,
    decision: TriageDecision,
    reproduction: Reproduction,
    change: ChangeSummary,
    suspects: Sequence[object],
    diff: str,
    gates: Sequence[GateResult],
    violations: Sequence[PolicyViolation],
) -> str:
    """Render the PR body: diagnosis, root cause, diff, verification table and policy gate.

    The body never asserts a green result that ``gates`` does not contain; an empty gate list
    renders an explicit "no gates recorded" row. Bounded by :data:`MAX_PR_BODY_CHARS`.
    """
    lines: list[str] = ["## Janus fix proposal", ""]
    lines.extend(_diagnosis_lines(decision))
    lines.extend([_reproduction_line(reproduction), ""])
    lines.extend(_change_lines(change))
    lines.append("")

    lines.extend(["### Root cause", "", "Ranked suspects:"])
    lines.extend(_suspect_lines(suspects))
    lines.append("")

    lines.extend(["### Proposed diff", ""])
    lines.extend(_fenced_diff(diff))
    lines.append("")

    lines.extend(["### Verification", ""])
    lines.extend(_gate_table(gates))
    lines.append("")

    lines.extend(["### Policy gate", ""])
    lines.extend(_policy_lines(violations))
    lines.append("")

    lines.extend(
        [
            "### Guardrails",
            "",
            "PR only: Janus never merges and never force-pushes.",
        ]
    )
    return _bound("\n".join(lines))


def _open_reason(
    decision: TriageDecision,
    gates: Sequence[GateResult],
    violations: Sequence[PolicyViolation],
) -> str:
    if decision.route is not Route.CODE_BUG:
        return (
            f"No PR was opened: `{decision.route.value}` is comment-only; "
            "only `CODE_BUG` proceeds to a fix."
        )
    if violations:
        return "No PR was opened: the static policy gate rejected the diff."
    if not gates:
        return "No PR was opened: no proof gates ran, so no claim can be made."
    if not all_passed(gates):
        failed = ", ".join(gate.gate for gate in gates if not gate.passed)
        return f"No PR was opened: the proof gates did not all pass ({failed})."
    return "No PR was opened: Janus is in suggest mode, so it only comments."


def render_suggest_comment(
    *,
    decision: TriageDecision,
    suspects: Sequence[object],
    diff: str | None,
    gates: Sequence[GateResult],
    violations: Sequence[PolicyViolation],
) -> str:
    """Render the comment-only surface for suggest mode or a failed proof.

    Contains the diagnosis, the ranked suspects, the proposed diff when one exists, the gate
    table, the policy result, and an explicit statement of why no PR was opened. Bounded by
    :data:`MAX_PR_BODY_CHARS`.
    """
    lines: list[str] = ["## Janus analysis (no PR opened)", ""]
    lines.extend(_diagnosis_lines(decision))

    lines.extend(["### What was found", ""])
    lines.extend(_suspect_lines(suspects))
    lines.append("")

    lines.extend(["### Proposed diff", ""])
    if diff:
        lines.extend(_fenced_diff(diff))
    else:
        lines.append("No diff was produced.")
    lines.append("")

    lines.extend(["### Verification", ""])
    lines.extend(_gate_table(gates))
    lines.append("")

    lines.extend(["### Policy gate", ""])
    lines.extend(_policy_lines(violations))
    lines.append("")

    lines.extend(["### Why no PR was opened", "", _open_reason(decision, gates, violations)])
    return _bound("\n".join(lines))


def render_pr_title(*, decision: TriageDecision, suspects: Sequence[object]) -> str:
    """Render a short, single-line PR title naming the route and the top suspect."""
    location = _first_location(suspects)
    title = f"Janus fix ({decision.route.value})"
    if location:
        title = f"{title}: {location}"
    title = _cell(title)
    if len(title) > _TITLE_LIMIT:
        title = title[: _TITLE_LIMIT - 3].rstrip() + "..."
    return title
