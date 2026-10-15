"""Retrieve similar past fixes by grepping ``git log -p`` — no vector database.

The retriever has three pure stages, each independently testable:

* :func:`build_terms` turns a fault signature (failing test ids, suspect files, error
  lines) into a small ordered set of search terms, dropping the noise that makes a
  ``git log -G`` query useless (timestamps, absolute paths, line numbers, hashes).
* :func:`git_log_argv` builds a shell-free argv for ``git log --oneline -p -G<term>``.
* :func:`parse_git_log` parses that output into :class:`PriorFix` rows, and
  :func:`rank` scores those rows by how many distinct terms they mention.

Nothing here runs a process or touches the network. The caller supplies the log text
and decides whether retrieval is worth doing at all (``git_log_argv`` returns ``()``
when there are no terms).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

MAX_DIFF_EXCERPT_CHARS = 4000
"""Upper bound for the excerpt kept per commit. A retrieval prompt never needs more."""

_DEFAULT_TERM_LIMIT = 8


@dataclass(frozen=True)
class PriorFix:
    """One past commit that may resemble the current fault."""

    sha: str
    """The abbreviated or full commit SHA from the ``--oneline`` header."""

    subject: str
    """The commit subject line."""

    files: tuple[str, ...]
    """Paths touched by the commit, as recovered from ``diff --git`` lines."""

    diff_excerpt: str
    """A bounded excerpt of the patch (empty for a merge with no diff)."""

    score: float = 0.0
    """Ranking score set by :func:`rank`; number of distinct matching terms."""


_COMMIT_LINE_RE = re.compile(r"^([0-9a-f]{7,40})[ \t]+(.+)$")
_DIFF_GIT_RE = re.compile(r'^diff --git "?a/(.+?)"? "?b/(.+?)"?$')

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_BRACKET_TIME_RE = re.compile(r"\[\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\]")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_ADDRESS_RE = re.compile(r"\b0x[0-9a-fA-F]+\b")
_ABSOLUTE_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|/)[^\s\"'`<>|]+")
_LINE_NUMBER_RE = re.compile(r"(?<!\w):\d+(?::\d+)?\b")
_HASH_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_STOPWORDS = frozenset(
    {
        "a",
        "address",
        "an",
        "and",
        "are",
        "as",
        "assert",
        "assertion",
        "at",
        "be",
        "because",
        "but",
        "by",
        "call",
        "called",
        "calls",
        "class",
        "cls",
        "could",
        "did",
        "do",
        "does",
        "error",
        "errors",
        "exception",
        "expected",
        "fail",
        "failed",
        "failure",
        "failures",
        "false",
        "file",
        "files",
        "float",
        "for",
        "from",
        "function",
        "got",
        "has",
        "have",
        "in",
        "int",
        "into",
        "is",
        "it",
        "its",
        "line",
        "lines",
        "list",
        "method",
        "module",
        "none",
        "null",
        "object",
        "of",
        "on",
        "or",
        "pass",
        "passed",
        "result",
        "results",
        "run",
        "running",
        "runs",
        "self",
        "set",
        "should",
        "str",
        "string",
        "test",
        "testing",
        "tests",
        "that",
        "the",
        "this",
        "to",
        "traceback",
        "true",
        "tuple",
        "use",
        "used",
        "using",
        "value",
        "values",
        "was",
        "were",
        "with",
        "without",
    }
)


def _strip_parametrisation(name: str) -> str:
    """Drop a pytest ``[param]`` suffix."""
    bracket = name.find("[")
    return name[:bracket] if bracket >= 0 else name


def _basename(path: str) -> str:
    """Return the final path component, tolerant of Windows separators."""
    text = path.strip().replace("\\", "/").rstrip("/")
    if not text:
        return ""
    return text.rsplit("/", 1)[-1].strip()


def _test_id_terms(test_id: str) -> list[str]:
    """Extract searchable names from a pytest node id, most specific first."""
    parts = [part.strip() for part in test_id.split("::") if part.strip()]
    if not parts:
        return []
    if len(parts) == 1:
        only = _strip_parametrisation(parts[0])
        if only.endswith(".py") or "/" in only or "\\" in only:
            name = _basename(only)
            stem = PurePosixPath(name).stem
            return [stem] if stem else []
        return [only]

    terms = [_strip_parametrisation(parts[-1])]
    terms.extend(_strip_parametrisation(part) for part in reversed(parts[1:-1]))
    module = _strip_parametrisation(parts[0])
    if module.endswith(".py") or "/" in module or "\\" in module:
        stem = PurePosixPath(_basename(module)).stem
        if stem:
            terms.append(stem)
    else:
        terms.append(module)
    return [term for term in terms if term]


def _clean_error_line(line: str) -> str:
    """Remove timestamps, paths, line numbers and other low-signal noise."""
    text = _ANSI_RE.sub(" ", line)
    text = _TIMESTAMP_RE.sub(" ", text)
    text = _BRACKET_TIME_RE.sub(" ", text)
    text = _UUID_RE.sub(" ", text)
    text = _ADDRESS_RE.sub(" ", text)
    text = _ABSOLUTE_PATH_RE.sub(" ", text)
    text = _LINE_NUMBER_RE.sub(" ", text)
    text = re.sub(r"(?i)\bline\s+\d+\b", " ", text)
    text = _HASH_RE.sub(" ", text)
    return text


def _is_noise_token(token: str) -> bool:
    lower = token.lower()
    if lower in _STOPWORDS:
        return True
    if len(token) < 4 and not any(char.isupper() for char in token) and "_" not in token:
        return True
    if len(token) >= 7 and all(char in "0123456789abcdefABCDEF" for char in token):
        return any(char.isdigit() for char in token)
    return False


def _token_priority(token: str) -> int:
    """Lower is more distinctive: exception classes, identifiers, then long words."""
    if any(char.isupper() for char in token):
        return 0
    if "_" in token:
        return 1
    if len(token) >= 7:
        return 2
    if len(token) >= 4:
        return 3
    return 4


def build_terms(
    *,
    failing_tests: Sequence[str],
    suspect_files: Sequence[str],
    error_lines: Sequence[str],
    limit: int = _DEFAULT_TERM_LIMIT,
) -> tuple[str, ...]:
    """Derive a small, de-duplicated, ordered set of ``git log -G`` terms.

    Terms come from three groups in this order: test names, suspect basenames, and the
    most distinctive tokens from the error lines. Duplicates are removed
    case-insensitively while preserving the first spelling. Noise — timestamps,
    absolute paths, line numbers, addresses and commit-like hashes — is dropped.
    ``limit <= 0`` yields an empty tuple.
    """
    if limit <= 0:
        return ()

    terms: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> bool:
        cleaned = term.strip().strip("`'\"")
        if not cleaned:
            return False
        key = cleaned.casefold()
        if key in seen:
            return False
        seen.add(key)
        terms.append(cleaned)
        return len(terms) >= limit

    for test_id in failing_tests:
        for term in _test_id_terms(test_id):
            if add(term):
                return tuple(terms)

    for path in suspect_files:
        basename = _basename(path)
        if basename and add(basename):
            return tuple(terms)

    candidates: list[tuple[int, int, str]] = []
    index = 0
    for line in error_lines:
        for match in _TOKEN_RE.finditer(_clean_error_line(line)):
            token = match.group(0)
            if _is_noise_token(token):
                continue
            candidates.append((_token_priority(token), index, token))
            index += 1

    for _, _, token in sorted(candidates):
        if add(token):
            break

    return tuple(terms)


def _safe_term(term: str) -> str:
    """Remove shell metacharacters and whitespace, then regex-escape the term."""
    cleaned = re.sub(r"[\s;&|<>$`\"'\\]", "", term.strip())
    if not cleaned:
        return ""
    return re.escape(cleaned)


def git_log_argv(terms: Sequence[str], *, max_commits: int = 50) -> tuple[str, ...]:
    """Build a shell-free argv that greps history for commits mentioning ``terms``.

    Returns ``()`` when there is nothing to search for. Each term becomes its own
    ``-G`` predicate, so a commit matching any term is returned; ``--max-count`` bounds
    the history scan.
    """
    if max_commits <= 0:
        return ()
    predicates = tuple(f"-G{escaped}" for term in terms if (escaped := _safe_term(term)))
    if not predicates:
        return ()
    return (
        "git",
        "log",
        "--oneline",
        "-p",
        f"--max-count={max_commits}",
        *predicates,
    )


def _finish_commit(sha: str, subject: str, files: list[str], excerpt: list[str]) -> PriorFix:
    return PriorFix(
        sha=sha,
        subject=subject,
        files=tuple(files),
        diff_excerpt="".join(excerpt).rstrip(),
    )


def parse_git_log(text: str, *, max_commits: int = 50) -> list[PriorFix]:
    """Parse ``git log --oneline -p`` output into :class:`PriorFix` rows.

    The abbreviated SHA and subject come from each commit header; files come from
    ``diff --git`` lines (deduplicated, in order); the excerpt is the first
    :data:`MAX_DIFF_EXCERPT_CHARS` characters of the patch. Merge commits with no diff
    are kept (with empty ``files``/``diff_excerpt``), binary diffs are recognised, and
    empty or garbage input yields ``[]``.
    """
    if not text or max_commits <= 0:
        return []

    commits: list[PriorFix] = []
    sha: str | None = None
    subject = ""
    files: list[str] = []
    excerpt: list[str] = []
    excerpt_chars = 0
    in_diff = False

    def finish() -> None:
        nonlocal sha, subject, files, excerpt, excerpt_chars, in_diff
        if sha is not None:
            commits.append(_finish_commit(sha, subject, files, excerpt))
        sha = None
        subject = ""
        files = []
        excerpt = []
        excerpt_chars = 0
        in_diff = False

    for line in text.splitlines():
        header = _COMMIT_LINE_RE.match(line)
        if header is not None:
            if sha is not None:
                finish()
                if len(commits) >= max_commits:
                    return commits
            sha = header.group(1)
            subject = header.group(2).strip()
            continue

        if sha is None:
            continue

        diff_match = _DIFF_GIT_RE.match(line)
        if diff_match is not None:
            in_diff = True
            path = diff_match.group(2) or diff_match.group(1)
            if path not in files:
                files.append(path)

        if in_diff and excerpt_chars < MAX_DIFF_EXCERPT_CHARS:
            remaining = MAX_DIFF_EXCERPT_CHARS - excerpt_chars
            piece = line[:remaining]
            excerpt.append(piece)
            excerpt_chars += len(piece)
            if len(piece) < len(line):
                excerpt.append("\n")
                excerpt_chars += 1

    if sha is not None and len(commits) < max_commits:
        finish()
    return commits


def rank(candidates: Sequence[PriorFix], terms: Sequence[str]) -> list[PriorFix]:
    """Score candidates by distinct term hits and return them best-first.

    Matching is case-insensitive over the subject and diff excerpt only. Duplicate
    terms count once. Ties preserve the input order, so the result is deterministic.
    """
    distinct: list[str] = []
    seen: set[str] = set()
    for term in terms:
        key = term.casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        distinct.append(key)

    scored: list[tuple[int, float, PriorFix]] = []
    for index, candidate in enumerate(candidates):
        haystack = f"{candidate.subject}\n{candidate.diff_excerpt}".casefold()
        score = float(sum(1 for term in distinct if term in haystack))
        scored.append((index, score, candidate))

    scored.sort(key=lambda item: (-item[1], item[0]))
    return [replace(candidate, score=score) for _, score, candidate in scored]
