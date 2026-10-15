"""The **static policy gate** for a generated fix.

This module answers one question: *does this diff cheat?* It inspects the unified diff's text
and rejects the shapes that make a failing suite pass without fixing the underlying fault:
edits to tests, edits to protected configuration, weakened CI, skipped tests, lowered coverage
thresholds, removed assertions, and net test-line deletion.

This is deliberately **static**. Nothing here runs a test, a mutator or a coverage tool; a
diff can look clean and still fail the four measured gates in
:mod:`janus.core.prove.gates`. The two gates are complements, not alternatives: the policy
gate catches obvious cheating cheaply, and the measured gates decide whether the change
actually holds. Neither is a claim on its own.

Everything is pure and total: malformed or empty input yields no violations, never an
exception. Violations are emitted in a fixed rule order so an identical diff produces an
identical list.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

_CI_WEAKENING_MARKERS: tuple[str, ...] = ("continue-on-error", "|| true", "set +e")

_SKIP_PATTERN = re.compile(
    r"(?:\.mark\.(?:skip|skipif|xfail)\b"
    r"|\bpytest\.(?:skip|skipif|xfail)\b"
    r"|\bunittest\.skip\b"
    r"|@Ignore\b"
    r"|\b(?:it|test|describe|context)\.skip\b"
    r"|\bt\.Skip[f]?\b"
    r"|\bxfail\b)"
)

_COVERAGE_THRESHOLD_PATTERN = re.compile(
    r"(?:--cov-fail-under\s*=\s*|fail_under\s*=\s*)(\d+(?:\.\d+)?)"
)

_ASSERT_PATTERN = re.compile(r"\bassert\b")

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

_DIFF_GIT_PREFIX = "diff --git "
_PLUS_HEADER = "+++ "
_MINUS_HEADER = "--- "

_EXCERPT_CHARS = 200


@dataclass(frozen=True)
class PolicyViolation:
    """One static-policy rule that a diff breaks.

    ``rule`` is a stable machine-readable identifier (see :func:`check_policy`); ``detail``
    is a bounded, human-readable explanation suitable for a PR or comment.
    """

    rule: str
    detail: str


@dataclass(frozen=True)
class _ParsedDiff:
    files: tuple[str, ...]
    added: tuple[tuple[str | None, str], ...]
    removed: tuple[tuple[str | None, str], ...]


def _strip_prefix(path: str) -> str:
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _unquote(path: str) -> str:
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        return path[1:-1]
    return path


def _clean_path(path: str) -> str | None:
    candidate = _unquote(path.split("\t", 1)[0].strip())
    if not candidate or candidate == "/dev/null":
        return None
    return _strip_prefix(candidate)


def _header_paths(line: str) -> tuple[str, ...]:
    parts = line[len(_DIFF_GIT_PREFIX) :].split()
    paths: list[str] = []
    for part in parts[:2]:
        cleaned = _clean_path(part)
        if cleaned is not None and cleaned not in paths:
            paths.append(cleaned)
    return tuple(paths)


def _hunk_lengths(line: str) -> tuple[int, int] | None:
    match = _HUNK_HEADER.match(line)
    if match is None:
        return None
    old = int(match.group(2)) if match.group(2) is not None else 1
    new = int(match.group(4)) if match.group(4) is not None else 1
    return old, new


def _parse(diff: str) -> _ParsedDiff:
    files: list[str] = []
    added: list[tuple[str | None, str]] = []
    removed: list[tuple[str | None, str]] = []
    current: str | None = None
    old_left = 0
    new_left = 0
    in_hunk = False

    for line in diff.splitlines():
        if in_hunk and old_left > 0 or in_hunk and new_left > 0:
            if line.startswith("+"):
                added.append((current, line[1:]))
                new_left -= 1
                continue
            if line.startswith("-"):
                removed.append((current, line[1:]))
                old_left -= 1
                continue
            if line.startswith(" "):
                old_left -= 1
                new_left -= 1
                continue
            if line.startswith("\\"):
                continue
            in_hunk = False

        if line.startswith(_DIFF_GIT_PREFIX):
            for path in _header_paths(line):
                if path not in files:
                    files.append(path)
            current = _header_paths(line)[0] if _header_paths(line) else current
            in_hunk = False
            continue
        if line.startswith(_PLUS_HEADER):
            candidate = _clean_path(line[len(_PLUS_HEADER) :])
            if candidate is not None:
                if candidate not in files:
                    files.append(candidate)
                current = candidate
            in_hunk = False
            continue
        if line.startswith(_MINUS_HEADER):
            candidate = _clean_path(line[len(_MINUS_HEADER) :])
            if candidate is not None and candidate not in files:
                files.append(candidate)
            in_hunk = False
            continue
        lengths = _hunk_lengths(line)
        if lengths is not None:
            old_left, new_left = lengths
            in_hunk = old_left > 0 or new_left > 0
            continue

    return _ParsedDiff(tuple(files), tuple(added), tuple(removed))


def diff_files(diff: str) -> tuple[str, ...]:
    """Return every path touched by a unified diff, in first-seen order.

    Paths come from ``diff --git a/x b/x`` headers and ``+++ b/x`` markers. ``/dev/null``
    is skipped, so a created or deleted file contributes only its real path. Total.
    """
    return _parse(diff).files


def added_lines(diff: str) -> tuple[str, ...]:
    """Return the added lines of a unified diff, without their leading ``+``.

    ``+++`` file headers are not added lines and are excluded. Total.
    """
    return tuple(text for _, text in _parse(diff).added)


def _normalise(path: str) -> str:
    return path.replace("\\", "/")


def _is_test_path(path: str, test_files: Sequence[str]) -> bool:
    normalised = _normalise(path).lower()
    if normalised in {_normalise(candidate).lower() for candidate in test_files}:
        return True
    return "test_" in normalised or "_test" in normalised or "tests/" in normalised


def _is_protected(path: str, protected_files: Sequence[str]) -> bool:
    normalised = _normalise(path).lower()
    return normalised in {_normalise(candidate).lower() for candidate in protected_files}


def _excerpt(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _EXCERPT_CHARS:
        return collapsed
    return collapsed[:_EXCERPT_CHARS].rstrip() + "..."


def _threshold_value(line: str) -> float | None:
    match = _COVERAGE_THRESHOLD_PATTERN.search(line)
    if match is None:
        return None
    return float(match.group(1))


def _asserts(lines: Iterator[tuple[str | None, str]]) -> list[str]:
    return [text for _, text in lines if _ASSERT_PATTERN.search(text)]


def check_policy(
    diff: str,
    *,
    test_files: Sequence[str] = (),
    protected_files: Sequence[str] = (),
) -> list[PolicyViolation]:
    """Run every static policy rule over ``diff``.

    The rules, in deterministic emission order, are:

    ``edits_tests``
        A touched path looks like a test (contains ``test_``, ``_test`` or ``tests/``) or is
        listed in ``test_files``.
    ``edits_protected``
        A touched path is listed in ``protected_files`` (CI workflows, coverage config, …).
    ``weakens_ci``
        An added line contains ``continue-on-error``, ``|| true`` or ``set +e``.
    ``skips_tests``
        An added line adds a skip/xfail marker (pytest, unittest, Jest/Vitest, Go, JUnit).
    ``lowers_threshold``
        An added coverage threshold is lower than a threshold the diff removes.
    ``removes_assertions``
        A removed ``assert`` line has no corresponding added ``assert`` line.
    ``weakens_tests``
        The diff deletes more lines from test files than it adds to them.

    An empty diff yields ``[]``. Unknown or malformed hunks are skipped rather than raising.
    This is the *static* gate only; the measured gates live in :mod:`janus.core.prove.gates`.
    """
    parsed = _parse(diff)
    violations: list[PolicyViolation] = []

    for path in parsed.files:
        if _is_test_path(path, test_files):
            violations.append(
                PolicyViolation("edits_tests", f"diff touches test path {_excerpt(path)!r}")
            )

    for path in parsed.files:
        if _is_protected(path, protected_files):
            violations.append(
                PolicyViolation(
                    "edits_protected",
                    f"diff touches protected path {_excerpt(path)!r}",
                )
            )

    for _, text in parsed.added:
        lowered = text.lower()
        marker = next((item for item in _CI_WEAKENING_MARKERS if item in lowered), None)
        if marker is not None:
            violations.append(
                PolicyViolation(
                    "weakens_ci",
                    f"added line weakens CI with {marker!r}: {_excerpt(text)!r}",
                )
            )

    for _, text in parsed.added:
        if _SKIP_PATTERN.search(text):
            violations.append(
                PolicyViolation(
                    "skips_tests",
                    f"added line skips or xfails a test: {_excerpt(text)!r}",
                )
            )

    removed_thresholds = [
        value for _, text in parsed.removed if (value := _threshold_value(text)) is not None
    ]
    if removed_thresholds:
        baseline = max(removed_thresholds)
        for _, text in parsed.added:
            value = _threshold_value(text)
            if value is not None and value < baseline:
                violations.append(
                    PolicyViolation(
                        "lowers_threshold",
                        f"coverage threshold lowered from {baseline:g} to {value:g}",
                    )
                )

    added_asserts = _asserts(iter(parsed.added))
    removed_asserts = _asserts(iter(parsed.removed))
    for text in removed_asserts[len(added_asserts) :]:
        violations.append(
            PolicyViolation(
                "removes_assertions",
                f"removed assertion has no corresponding added assertion: {_excerpt(text)!r}",
            )
        )

    test_tally: dict[str, list[int]] = {}
    for entry_path, _ in parsed.removed:
        if entry_path is not None and _is_test_path(entry_path, test_files):
            test_tally.setdefault(entry_path, [0, 0])[0] += 1
    for entry_path, _ in parsed.added:
        if entry_path is not None and _is_test_path(entry_path, test_files):
            test_tally.setdefault(entry_path, [0, 0])[1] += 1
    removed_test_lines = sum(removed for removed, _ in test_tally.values())
    added_test_lines = sum(added for _, added in test_tally.values())
    if removed_test_lines > added_test_lines:
        violations.append(
            PolicyViolation(
                "weakens_tests",
                f"diff deletes {removed_test_lines} test lines but adds only {added_test_lines}",
            )
        )

    return violations
