"""Bisect planning and transcript parsing — pure, ordered, and total."""

from __future__ import annotations

from janus.core.localize.bisect import (
    BisectPlan,
    BisectResult,
    available,
    parse_bisect_output,
    plan_bisect,
)


def test_plan_orders_the_git_commands() -> None:
    plan = plan_bisect(
        good_sha="aaa1111",
        bad_sha="bbb2222",
        run_command=("pytest", "-q", "tests/test_widget.py"),
    )

    assert plan == BisectPlan(
        argv=(
            ("git", "bisect", "start"),
            ("git", "bisect", "bad", "bbb2222"),
            ("git", "bisect", "good", "aaa1111"),
            ("git", "bisect", "run", "pytest", "-q", "tests/test_widget.py"),
            ("git", "bisect", "reset"),
        )
    )


def test_parse_a_realistic_transcript() -> None:
    transcript = """\
Bisecting: 3 revisions left to test after this (roughly 2 steps)
[abcdef1234567890] add the widget
Bisecting: 1 revision left to test after this (roughly 1 step)
[1234567890abcdef] break the widget
1234567890abcdef is the first bad commit
commit 1234567890abcdef
Author: Janus <janus@example.com>
Date:   Mon Oct 20 12:00:00 2026 +0000

    break the widget
"""

    result = parse_bisect_output(transcript)

    assert result == BisectResult(
        first_bad_commit="1234567890abcdef",
        steps=2,
        log_excerpt=transcript.strip(),
    )


def test_parse_rejects_garbage_empty_and_progress_only() -> None:
    assert parse_bisect_output("") is None
    assert parse_bisect_output("   \n\t ") is None
    assert parse_bisect_output("fatal: not a git repository") is None
    assert parse_bisect_output("Bisecting: 3 revisions left to test after this") is None


def test_available_requires_real_history() -> None:
    assert available(None) is False
    assert available(1) is False
    assert available(2) is True
    assert available(50) is True
