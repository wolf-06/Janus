"""The PR body, suggest comment and branch name: contents, parity and bounds."""

from __future__ import annotations

from dataclasses import dataclass

from janus.core.evidence import ChangeSummary, Reproduction, RunAttempt
from janus.core.prove.gates import (
    GATE_COVERAGE,
    GATE_MUTATION,
    GATE_SUITE,
    GATE_TEST_HASHES,
    GateResult,
    evaluate,
)
from janus.core.prove.policy import PolicyViolation
from janus.core.triage.routes import Route, TriageDecision, decide
from janus.report.pr import (
    MAX_PR_BODY_CHARS,
    branch_name,
    render_pr_body,
    render_pr_title,
    render_suggest_comment,
)
from janus.sandbox.profiles import Profile


@dataclass(frozen=True)
class FakeSuspect:
    file: str
    line: int | None = None
    score: float = 0.9
    rank: int = 1
    introducing_commit: str | None = None


def code_bug() -> TriageDecision:
    return decide({Route.CODE_BUG: 0.87, Route.FLAKY: 0.2, Route.ENV: 0.1})


def flaky() -> TriageDecision:
    return decide({Route.FLAKY: 0.91, Route.CODE_BUG: 0.1, Route.ENV: 0.05})


def reproduction() -> Reproduction:
    attempt = RunAttempt(
        index=0,
        target_id="tests/test_foo.py::test_a",
        passed=False,
        duration_s=1.5,
        exit_code=1,
        timed_out=False,
        oom_killed=False,
        cpu_seconds=0.4,
        peak_mem_mb=64.0,
        stdout_excerpt="",
        stderr_excerpt="",
    )
    return Reproduction(profile=Profile.CLEAN, attempts=(attempt,), full_suite=attempt)


def change() -> ChangeSummary:
    return ChangeSummary(
        base_sha="abc1234567890",
        head_sha="def4567890123",
        files_changed=("janus/core/foo.py",),
    )


def gates(*, suite_passed: bool = True) -> list[GateResult]:
    return evaluate(
        suite_passed=suite_passed,
        mutation_before=None,
        mutation_after=None,
        coverage_before=80.0,
        coverage_after=81.0,
        test_hashes_before={"tests/test_foo.py": "aaa"},
        test_hashes_after={"tests/test_foo.py": "aaa"},
    )


def test_branch_name_is_deterministic_and_ref_safe() -> None:
    assert branch_name(42) == "janus/fix-42"
    assert branch_name(42) == branch_name(42)
    assert " " not in branch_name(42)
    assert branch_name(-1) == "janus/fix--1"


def test_pr_body_contains_diagnosis_suspects_diff_and_every_gate() -> None:
    gate_results = gates()
    text = render_pr_body(
        decision=code_bug(),
        reproduction=reproduction(),
        change=change(),
        suspects=[FakeSuspect("janus/core/foo.py", 10)],
        diff="--- a/janus/core/foo.py\n+++ b/janus/core/foo.py\n+    return 2\n",
        gates=gate_results,
        violations=[],
    )

    assert "CODE_BUG" in text
    assert "87%" in text
    assert "janus/core/foo.py:10" in text
    assert "```diff" in text
    assert "+    return 2" in text
    assert "| gate | passed | measured | baseline |" in text
    for gate in gate_results:
        assert gate.gate in text
    assert "Policy gate passed" in text


def test_pr_body_reports_policy_violations() -> None:
    text = render_pr_body(
        decision=code_bug(),
        reproduction=reproduction(),
        change=change(),
        suspects=[],
        diff="",
        gates=gates(),
        violations=[PolicyViolation("edits_tests", "diff touches test path 'tests/test_a.py'")],
    )

    assert "Policy gate failed" in text
    assert "edits_tests" in text
    assert "tests/test_a.py" in text


def test_suggest_comment_explains_a_comment_only_route() -> None:
    text = render_suggest_comment(
        decision=flaky(),
        suspects=[FakeSuspect("tests/test_foo.py", 5)],
        diff="--- a/tests/test_foo.py\n+++ b/tests/test_foo.py\n+    pass\n",
        gates=gates(),
        violations=[],
    )

    assert "FLAKY" in text
    assert "No PR was opened" in text
    assert "comment-only" in text
    assert "```diff" in text


def test_suggest_comment_explains_a_failed_proof() -> None:
    text = render_suggest_comment(
        decision=code_bug(),
        suspects=[FakeSuspect("janus/core/foo.py", 10)],
        diff=None,
        gates=gates(suite_passed=False),
        violations=[],
    )

    assert "No PR was opened" in text
    assert "proof gates did not all pass" in text
    assert "suite_green" in text
    assert "No diff was produced." in text


def test_suggest_comment_explains_a_policy_rejection() -> None:
    text = render_suggest_comment(
        decision=code_bug(),
        suspects=[],
        diff=None,
        gates=gates(),
        violations=[PolicyViolation("skips_tests", "added line skips a test")],
    )

    assert "static policy gate rejected" in text
    assert "skips_tests" in text


def test_pr_body_is_bounded_for_a_huge_diff() -> None:
    text = render_pr_body(
        decision=code_bug(),
        reproduction=reproduction(),
        change=change(),
        suspects=[FakeSuspect("janus/core/foo.py", 10)],
        diff="+x" * (MAX_PR_BODY_CHARS * 2),
        gates=gates(),
        violations=[],
    )

    assert len(text) <= MAX_PR_BODY_CHARS
    assert "Truncated" in text


def test_suggest_comment_is_bounded_for_a_huge_diff() -> None:
    text = render_suggest_comment(
        decision=code_bug(),
        suspects=[FakeSuspect("janus/core/foo.py", 10)],
        diff="+x" * (MAX_PR_BODY_CHARS * 2),
        gates=gates(),
        violations=[],
    )

    assert len(text) <= MAX_PR_BODY_CHARS
    assert "Truncated" in text


def test_renderers_are_total_on_empty_inputs() -> None:
    body = render_pr_body(
        decision=code_bug(),
        reproduction=Reproduction(profile=Profile.CLEAN, attempts=(), full_suite=None),
        change=ChangeSummary(base_sha=None, head_sha="def"),
        suspects=(),
        diff="",
        gates=(),
        violations=(),
    )
    comment = render_suggest_comment(
        decision=code_bug(),
        suspects=(),
        diff=None,
        gates=(),
        violations=(),
    )
    title = render_pr_title(decision=code_bug(), suspects=())

    assert isinstance(body, str) and body
    assert isinstance(comment, str) and comment
    assert "no gates recorded" in body
    assert "no suspects recorded" in body
    assert "no proof gates ran" in comment
    assert "\n" not in title


def test_pr_title_names_the_route_and_top_suspect() -> None:
    title = render_pr_title(
        decision=code_bug(),
        suspects=[FakeSuspect("janus/core/foo.py", 10)],
    )

    assert "CODE_BUG" in title
    assert "janus/core/foo.py:10" in title
    assert len(title.splitlines()) == 1


def test_gate_constants_used_by_the_renderer_are_the_four_real_gates() -> None:
    assert [gate.gate for gate in gates()] == [
        GATE_SUITE,
        GATE_MUTATION,
        GATE_COVERAGE,
        GATE_TEST_HASHES,
    ]
