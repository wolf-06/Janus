"""Build the strict fix-generation prompt and pull a unified diff out of a reply.

The prompt is deliberately prescriptive: it shows the failure evidence, the ranked
suspects, truncated source and similar past fixes, then restates the hard rules that
the policy gate will enforce. Rules are never truncated; when the prompt is too long,
source sections give way first.

The reply side is equally strict: :func:`extract_diff` accepts a fenced ``diff`` block
or a bare unified diff and returns ``None`` for prose, so a model that ignores the
rules cannot smuggle a non-diff answer into the proof loop.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from janus.core.evidence import Reproduction
from janus.core.triage.routes import TriageDecision

MAX_PROMPT_CHARS = 24000
"""Hard cap on the generated prompt. Source sections are truncated to fit."""

_MAX_FAILURES = 5
_MAX_ERROR_CHARS = 600
_MAX_SUSPECTS = 10
_MAX_PRIOR_FIXES = 3
_MAX_PRIOR_DIFF_CHARS = 500

_RULES = """HARD RULES (MANDATORY)
1. Reply with one unified diff and nothing else.
2. Change source files only. Never modify, add, delete or weaken tests, fixtures, or conftest files.
3. Do not change CI or workflow configuration.
4. Never add continue-on-error, || true, set +e, skips, xfails, or any other failure suppression.
5. Keep the change minimal and focused on the failing behavior.
6. Do not include explanations, commentary, or Markdown fences outside the diff."""

_DIFF_FENCE_RE = re.compile(
    r"```[ \t]*(?:diff|patch)[ \t]*\r?\n(.*?)```", re.IGNORECASE | re.DOTALL
)
_ANY_FENCE_RE = re.compile(r"```[^\n]*\r?\n(.*?)```", re.DOTALL)

_DIFF_PREFIXES = (
    "diff --git ",
    "index ",
    "--- ",
    "+++ ",
    "@@",
    "+",
    "-",
    " ",
    "\\",
    "new file mode ",
    "deleted file mode ",
    "old mode ",
    "new mode ",
    "similarity index ",
    "dissimilarity index ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "Binary files ",
    "GIT binary patch",
    "literal ",
    "delta ",
)


def _clip(text: str, limit: int) -> str:
    """Return ``text`` clipped to at most ``limit`` characters."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


def _field(obj: object, *names: str) -> Any:
    """Read the first present attribute/key from a loosely typed record."""
    if isinstance(obj, Mapping):
        mapping = dict(obj)
        for name in names:
            if name in mapping and mapping[name] is not None:
                return mapping[name]
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return None


def _format_score(value: object) -> str:
    if isinstance(value, int | float):
        return f"{float(value):.3f}"
    return str(value)


def _intro_section(decision: TriageDecision) -> str:
    reasons = "; ".join(decision.reasons) if decision.reasons else "none recorded"
    return (
        "You are Janus, an automated repair agent. "
        "Fix the failing tests by changing source code only.\n\n"
        "TRIAGE\n"
        f"route: {decision.route.value}\n"
        f"confidence: {decision.confidence:.3f}\n"
        f"reasons: {_clip(reasons, 400)}"
    )


def _failure_excerpt(attempt: object) -> str:
    for name in ("stderr_excerpt", "stdout_excerpt", "sandbox_error"):
        value = _field(attempt, name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _failure_section(reproduction: Reproduction) -> str:
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for attempt in reproduction.attempts:
        if attempt.passed:
            continue
        target = attempt.target_id or "(full suite)"
        if target in seen:
            continue
        seen.add(target)
        entries.append((target, _failure_excerpt(attempt)))
    if not entries and reproduction.full_suite is not None and not reproduction.full_suite.passed:
        entries.append(("(full suite)", _failure_excerpt(reproduction.full_suite)))
    if not entries:
        return "FAILING TESTS AND ERROR EXCERPTS\n(none recorded)"

    lines = ["FAILING TESTS AND ERROR EXCERPTS"]
    for target, excerpt in entries[:_MAX_FAILURES]:
        lines.append(f"- {target}")
        if excerpt:
            clipped = _clip(excerpt, _MAX_ERROR_CHARS)
            lines.extend(f"  {line}" for line in clipped.splitlines())
        else:
            lines.append("  (no output captured)")
    return "\n".join(lines)


def _suspect_ref(suspect: object) -> str:
    if isinstance(suspect, str):
        return suspect
    path = _field(suspect, "file", "path", "filename")
    line = _field(suspect, "line", "lineno")
    if path is None:
        return str(suspect)
    ref = str(path)
    if line is not None:
        ref += f":{line}"
    return ref


def _suspect_section(suspects: Sequence[object]) -> str:
    if not suspects:
        return "RANKED SUSPECTS\n(none)"
    lines = ["RANKED SUSPECTS"]
    for index, suspect in enumerate(suspects[:_MAX_SUSPECTS], start=1):
        ref = _suspect_ref(suspect)
        details: list[str] = []
        score = _field(suspect, "score")
        rank_value = _field(suspect, "rank")
        if rank_value is not None:
            details.append(f"rank {rank_value}")
        if score is not None:
            details.append(f"score {_format_score(score)}")
        suffix = f" ({', '.join(details)})" if details else ""
        lines.append(f"{index}. {ref}{suffix}")
    return "\n".join(lines)


def _prior_section(prior_fixes: Sequence[object]) -> str:
    if not prior_fixes:
        return ""
    lines = ["SIMILAR PAST FIXES"]
    for fix in prior_fixes[:_MAX_PRIOR_FIXES]:
        if isinstance(fix, str):
            lines.append(f"- {fix}")
            continue
        sha = _field(fix, "sha", "commit", "id")
        subject = _field(fix, "subject", "message", "title")
        files = _field(fix, "files")
        excerpt = _field(fix, "diff_excerpt", "diff", "patch")

        label_parts = [str(part) for part in (sha, subject) if part]
        label = " ".join(label_parts) if label_parts else "(unnamed commit)"
        lines.append(f"- {label}")
        if isinstance(files, Sequence) and not isinstance(files, str) and files:
            lines.append("  files: " + ", ".join(str(item) for item in files))
        if isinstance(excerpt, str) and excerpt.strip():
            lines.append("  diff excerpt:")
            clipped = _clip(excerpt.strip(), _MAX_PRIOR_DIFF_CHARS)
            lines.extend(f"    {line}" for line in clipped.splitlines())
    return "\n".join(lines)


def _source_section(sources: Mapping[str, str], budget: int) -> str:
    """Render as many source files as fit in ``budget`` characters."""
    if budget <= 0 or not sources:
        return ""
    out = "RELEVANT SOURCE"
    for path, text in sources.items():
        header = f"\n\n### {path}\n```\n"
        footer = "\n```"
        remaining = budget - len(out) - len(header) - len(footer)
        if remaining <= 0:
            break
        body = str(text)
        chunk = body[:remaining]
        out += header + chunk + footer
        if len(chunk) < len(body):
            break
    return out if out != "RELEVANT SOURCE" else ""


def build_fix_prompt(
    *,
    decision: TriageDecision,
    reproduction: Reproduction,
    suspects: Sequence[object],
    sources: Mapping[str, str],
    prior_fixes: Sequence[object],
    max_chars: int = MAX_PROMPT_CHARS,
) -> str:
    """Build a strict, bounded prompt asking for one source-only unified diff.

    The prompt carries the failing tests and error excerpts, the ranked suspects, the
    relevant source, similar past fixes and the hard rules. Source is truncated first
    when the result would exceed ``max_chars``; the rules are never truncated.
    """
    cap = max(0, int(max_chars))
    if cap == 0:
        return ""

    intro = _intro_section(decision)
    failures = _failure_section(reproduction)
    suspect_text = _suspect_section(suspects)
    prior = _prior_section(prior_fixes)
    separator = "\n\n"

    before_source = separator.join(part for part in (intro, failures, suspect_text) if part)
    fixed = before_source
    if prior:
        fixed += separator + prior
    fixed += separator + _RULES

    if len(fixed) <= cap:
        source_room = cap - len(fixed) - len(separator)
        source_text = _source_section(sources, source_room) if source_room > 0 else ""
        if source_text:
            prompt = before_source + separator + source_text
            if prior:
                prompt += separator + prior
            prompt += separator + _RULES
            return prompt
        return fixed

    # Too long: drop the optional prior fixes and all source, then clip the rest.
    without_prior = before_source + separator + _RULES
    if len(without_prior) <= cap:
        return without_prior

    room = cap - len(_RULES) - len(separator)
    if room <= 0:
        return _RULES
    return _clip(before_source, room) + separator + _RULES


def _looks_like_diff_lines(text: str) -> bool:
    return "--- " in text and "+++ " in text and "@@" in text


def looks_like_diff(text: str) -> bool:
    """Cheap structural check: a plausible unified diff has headers and a hunk."""
    return _looks_like_diff_lines(text)


def _is_diff_line(line: str) -> bool:
    return line.startswith(_DIFF_PREFIXES)


def _normalise(block: str) -> str:
    return block.strip("\n")


def _bare_diff(text: str) -> str | None:
    lines = text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if line.startswith("diff --git "):
            start = index
            break
        if (
            line.startswith("--- ")
            and index + 1 < len(lines)
            and lines[index + 1].startswith("+++ ")
        ):
            start = index
            break
    if start is None:
        return None

    collected: list[str] = []
    for line in lines[start:]:
        if line == "" or _is_diff_line(line):
            collected.append(line)
            continue
        break
    candidate = _normalise("\n".join(collected))
    if not candidate:
        return None
    if candidate.startswith("diff --git ") or looks_like_diff(candidate):
        return candidate
    return None


def extract_diff(text: str) -> str | None:
    """Extract a unified diff from a model reply, or ``None`` when there is none.

    A fenced ``diff``/``patch`` block is preferred; any other fenced block is accepted
    only when its contents look like a diff. Otherwise the first bare run of diff lines
    is returned. The function never raises.
    """
    if not text:
        return None

    for pattern in (_DIFF_FENCE_RE, _ANY_FENCE_RE):
        for match in pattern.finditer(text):
            candidate = _normalise(match.group(1))
            if candidate and looks_like_diff(candidate):
                return candidate

    return _bare_diff(text)
