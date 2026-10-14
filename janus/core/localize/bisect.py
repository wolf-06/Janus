"""Planning and parsing for a future ``git bisect`` run.

The localizer's primary signal is SBFL over per-test coverage. Bisect would name the
commit that introduced a fault, but it needs commit ancestry and the source provider
fetches a ``--depth 1`` checkout: a shallow clone has a single commit and nothing to
bisect. That is why :func:`available` returns ``False`` for a depth of ``1`` or ``None``
and why ``Settings.bisect_enabled`` defaults to off.

This module deliberately does **not** execute anything. It builds the ordered argv for a
bisect and parses the transcript git prints, so the sandbox runner can execute it later
when a history-aware checkout is available. Both functions are pure; the parser is total
and returns ``None`` for anything it cannot recognise.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

_FIRST_BAD_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]*([0-9a-fA-F]{7,64})[ \t]+is the first bad commit\b",
    re.MULTILINE,
)


@dataclass(frozen=True)
class BisectPlan:
    """The ordered git commands that perform one bisect, as argv tuples."""

    argv: tuple[tuple[str, ...], ...]
    """Commands in execution order: start, bad, good, run, reset."""


@dataclass(frozen=True)
class BisectResult:
    """The outcome of a completed bisect."""

    first_bad_commit: str
    """The SHA git reported as the first bad commit."""

    steps: int
    """How many ``Bisecting:`` progress lines the transcript contained."""

    log_excerpt: str
    """The (trimmed) transcript the result was parsed from."""


def plan_bisect(*, good_sha: str, bad_sha: str, run_command: Sequence[str]) -> BisectPlan:
    """Build the git argv for bisecting between ``good_sha`` and ``bad_sha``.

    ``run_command`` is the command git will run at each step (for example the test
    invocation); it is embedded verbatim after ``git bisect run``. Nothing is executed.
    """
    return BisectPlan(
        argv=(
            ("git", "bisect", "start"),
            ("git", "bisect", "bad", bad_sha),
            ("git", "bisect", "good", good_sha),
            ("git", "bisect", "run", *run_command),
            ("git", "bisect", "reset"),
        )
    )


def parse_bisect_output(text: str) -> BisectResult | None:
    """Parse git's bisect transcript.

    Recognises the line ``"<sha> is the first bad commit"`` and counts the
    ``"Bisecting:"`` progress lines as steps. Returns ``None`` when the transcript is
    empty, garbage, or does not contain a first-bad-commit line.
    """
    match = _FIRST_BAD_RE.search(text)
    if match is None:
        return None
    return BisectResult(
        first_bad_commit=match.group(1),
        steps=text.count("Bisecting:"),
        log_excerpt=text.strip(),
    )


def available(source_depth: int | None) -> bool:
    """Whether a checkout has enough history for ``git bisect``.

    ``None`` means the depth is unknown and ``1`` means a shallow single-commit clone;
    both have no usable ancestry, so bisect is unavailable (and off by default). Any
    depth greater than one is treated as deep enough.
    """
    return source_depth is not None and source_depth > 1
