"""Prompt construction and unified-diff extraction."""

from __future__ import annotations

from dataclasses import dataclass

from janus.core.evidence import Reproduction, RunAttempt
from janus.core.generate.prompt import (
    MAX_PROMPT_CHARS,
    build_fix_prompt,
    extract_diff,
    looks_like_diff,
)
from janus.core.retrieve.git_grep import PriorFix
from janus.core.triage.routes import Route, TriageDecision, decide
from janus.sandbox.profiles import Profile


@dataclass(frozen=True)
class Suspect:
    file: str
    line: int
    score: float


def _attempt(target: str, stderr: str, *, passed: bool = False) -> RunAttempt:
    return RunAttempt(
        index=0,
        target_id=target,
        passed=passed,
        duration_s=0.1,
        exit_code=0 if passed else 1,
        timed_out=False,
        oom_killed=False,
        cpu_seconds=0.1,
        peak_mem_mb=16.0,
        stdout_excerpt="",
        stderr_excerpt=stderr,
    )


def _reproduction() -> Reproduction:
    return Reproduction(
        profile=Profile.CLEAN,
        attempts=(
            _attempt("tests/test_parser.py::test_empty", "TypeError: expected str"),
            _attempt("tests/test_parser.py::test_whitespace", "AssertionError: assert 1 == 2"),
            _attempt("tests/test_parser.py::test_ok", "", passed=True),
        ),
        full_suite=None,
    )


def _decision() -> TriageDecision:
    return decide(
        {Route.CODE_BUG: 0.9, Route.FLAKY: 0.1, Route.ENV: 0.0},
        reasons=("always fails", "change touches the parser"),
    )


def _prompt(*, max_chars: int = MAX_PROMPT_CHARS, sources: dict[str, str] | None = None) -> str:
    return build_fix_prompt(
        decision=_decision(),
        reproduction=_reproduction(),
        suspects=[Suspect("src/parser.py", 42, 0.91), Suspect("src/util.py", 7, 0.4)],
        sources=sources
        if sources is not None
        else {"src/parser.py": "def parse(value):\n    ...\n"},
        prior_fixes=[
            PriorFix(
                sha="abc1234",
                subject="Fix parser crash",
                files=("src/parser.py",),
                diff_excerpt="diff --git a/src/parser.py b/src/parser.py\n@@ -1 +1 @@\n-a\n+b",
                score=2.0,
            )
        ],
        max_chars=max_chars,
    )


def test_prompt_contains_evidence_suspects_source_and_rules() -> None:
    prompt = _prompt()

    assert "tests/test_parser.py::test_empty" in prompt
    assert "TypeError: expected str" in prompt
    assert "src/parser.py:42" in prompt
    assert "RELEVANT SOURCE" in prompt
    assert "SIMILAR PAST FIXES" in prompt
    assert "abc1234" in prompt
    assert "HARD RULES (MANDATORY)" in prompt


def test_prompt_states_every_hard_rule() -> None:
    prompt = _prompt().casefold()

    assert "unified diff" in prompt
    assert "source files only" in prompt
    assert "never modify" in prompt and "tests" in prompt
    assert "ci" in prompt
    assert "continue-on-error" in prompt
    assert "|| true" in prompt
    assert "set +e" in prompt
    assert "skips" in prompt and "xfails" in prompt
    assert "minimal" in prompt


def test_prompt_is_bounded_and_keeps_the_rules() -> None:
    prompt = _prompt(max_chars=2000, sources={"src/huge.py": "x = 1\n" * 20_000})

    assert len(prompt) <= 2000
    assert "HARD RULES (MANDATORY)" in prompt
    assert not prompt.endswith("x")


def test_extract_fenced_diff() -> None:
    reply = (
        "Here is the fix:\n"
        "```diff\n"
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
        "```\n"
        "Done."
    )

    extracted = extract_diff(reply)

    assert extracted is not None
    assert extracted.startswith("diff --git a/x.py b/x.py")
    assert extracted.endswith("+b")
    assert "```" not in extracted
    assert "Here is the fix" not in extracted


def test_extract_bare_diff_stops_at_prose() -> None:
    reply = (
        "I changed the return value.\n\n"
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n\n"
        "That should fix it."
    )

    extracted = extract_diff(reply)

    assert extracted is not None
    assert extracted.startswith("diff --git a/x.py b/x.py")
    assert "That should fix it" not in extracted


def test_extract_diff_returns_none_for_prose() -> None:
    assert extract_diff("I could not find a fix.") is None
    assert extract_diff("") is None


def test_looks_like_diff() -> None:
    assert looks_like_diff("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b")
    assert not looks_like_diff("--- a/x\n+++ b/x")
    assert not looks_like_diff("prose")
