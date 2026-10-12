"""The evidence renderer: measured-facts Markdown, the Check-run payload, and its bounds.

Every fact the comment claims must be traceable to the evidence it is handed — the route, the
confidence, each attempt, the full suite, each signal summary and its votes, the reasons, and
the per-route next step. The baseline note and the length bound are asserted explicitly.
"""

from __future__ import annotations

from janus.core.evidence import ChangeSummary, Reproduction, RunAttempt
from janus.core.signals.base import SignalEvidence
from janus.core.triage.routes import Route, TriageDecision, decide
from janus.report.comment import (
    MAX_COMMENT_CHARS,
    check_conclusion,
    render_check_output,
    render_evidence,
)
from janus.sandbox.profiles import Profile


def attempt(
    index: int,
    *,
    passed: bool,
    target_id: str | None = "tests/test_x.py::test_a",
    duration_s: float = 1.0,
    exit_code: int | None = None,
    timed_out: bool = False,
) -> RunAttempt:
    return RunAttempt(
        index=index,
        target_id=target_id,
        passed=passed,
        duration_s=duration_s,
        exit_code=exit_code,
        timed_out=timed_out,
        oom_killed=False,
        cpu_seconds=0.5,
        peak_mem_mb=64.0,
        stdout_excerpt="",
        stderr_excerpt="",
    )


def reproduction(*attempts: RunAttempt, full_suite: RunAttempt | None = None) -> Reproduction:
    return Reproduction(profile=Profile.CLEAN, attempts=attempts, full_suite=full_suite)


def signal(
    name: str,
    summary: str,
    votes: dict[Route, float] | None = None,
) -> SignalEvidence:
    return SignalEvidence(name=name, summary=summary, details={}, votes=votes or {})


def code_bug_decision() -> TriageDecision:
    return decide({Route.CODE_BUG: 0.87, Route.FLAKY: 0.75, Route.ENV: 0.1})


def test_render_evidence_contains_the_route_and_confidence() -> None:
    text = render_evidence(
        decision=code_bug_decision(),
        reproduction=reproduction(),
        change=ChangeSummary(base_sha="abc", head_sha="def"),
        evidence=[],
        failing_targets=(),
    )

    assert "CODE_BUG" in text
    assert "87%" in text
    assert "0.12" in text


def test_render_evidence_contains_every_attempt_row() -> None:
    text = render_evidence(
        decision=code_bug_decision(),
        reproduction=reproduction(
            attempt(0, passed=False, target_id="tests/test_a.py::test_one", exit_code=1),
            attempt(1, passed=True, target_id="tests/test_b.py::test_two", exit_code=0),
        ),
        change=ChangeSummary(base_sha="abc", head_sha="def"),
        evidence=[],
        failing_targets=("tests/test_a.py::test_one",),
    )

    assert "tests/test_a.py::test_one" in text
    assert "tests/test_b.py::test_two" in text
    assert "failed" in text
    assert "passed" in text
    assert "| 0 |" in text
    assert "| 1 |" in text


def test_render_evidence_contains_the_full_suite_row() -> None:
    text = render_evidence(
        decision=code_bug_decision(),
        reproduction=reproduction(
            attempt(0, passed=False, exit_code=1),
            full_suite=attempt(0, passed=True, target_id=None, exit_code=0),
        ),
        change=ChangeSummary(base_sha="abc", head_sha="def"),
        evidence=[],
        failing_targets=(),
    )

    assert "Full suite" in text
    assert "Full suite passed" in text


def test_render_evidence_reports_a_missing_full_suite() -> None:
    text = render_evidence(
        decision=code_bug_decision(),
        reproduction=reproduction(attempt(0, passed=False)),
        change=ChangeSummary(base_sha="abc", head_sha="def"),
        evidence=[],
        failing_targets=(),
    )

    assert "Full suite: not run." in text


def test_render_evidence_contains_each_signal_summary() -> None:
    text = render_evidence(
        decision=code_bug_decision(),
        reproduction=reproduction(attempt(0, passed=False)),
        change=ChangeSummary(base_sha="abc", head_sha="def"),
        evidence=[
            signal("determinism", "3/3 reruns failed", {Route.CODE_BUG: 0.9}),
            signal("signatures", "no infra signature matched", {Route.ENV: 0.1}),
        ],
        failing_targets=(),
    )

    assert "3/3 reruns failed" in text
    assert "no infra signature matched" in text
    assert "determinism" in text
    assert "signatures" in text
    assert "0.90" in text


def test_render_evidence_next_step_is_route_specific() -> None:
    code_bug = render_evidence(
        decision=decide({Route.CODE_BUG: 0.9, Route.FLAKY: 0.1}),
        reproduction=reproduction(),
        change=ChangeSummary(base_sha="abc", head_sha="def"),
        evidence=[],
        failing_targets=(),
    )
    flaky = render_evidence(
        decision=decide({Route.FLAKY: 0.9, Route.CODE_BUG: 0.1, Route.ENV: 0.05}),
        reproduction=reproduction(),
        change=ChangeSummary(base_sha="abc", head_sha="def"),
        evidence=[],
        failing_targets=(),
    )
    env = render_evidence(
        decision=decide({Route.ENV: 0.9, Route.CODE_BUG: 0.1, Route.FLAKY: 0.05}),
        reproduction=reproduction(),
        change=ChangeSummary(base_sha="abc", head_sha="def"),
        evidence=[],
        failing_targets=(),
    )
    unsure = render_evidence(
        decision=decide({Route.CODE_BUG: 0.5, Route.FLAKY: 0.5}),
        reproduction=reproduction(),
        change=ChangeSummary(base_sha="abc", head_sha="def"),
        evidence=[],
        failing_targets=(),
    )

    assert "code fix" in code_bug
    for text in (flaky, env, unsure):
        assert "comment only" in text.lower()


def test_render_evidence_notes_an_unresolved_baseline() -> None:
    unavailable = render_evidence(
        decision=code_bug_decision(),
        reproduction=reproduction(),
        change=ChangeSummary(base_sha=None, head_sha="def"),
        evidence=[],
        failing_targets=(),
    )
    available = render_evidence(
        decision=code_bug_decision(),
        reproduction=reproduction(),
        change=ChangeSummary(base_sha="abc", head_sha="def"),
        evidence=[],
        failing_targets=(),
    )

    assert "baseline could not be resolved" in unavailable
    assert "baseline could not be resolved" not in available


def test_render_evidence_is_bounded_by_max_comment_chars() -> None:
    attempts = tuple(
        attempt(i, passed=False, target_id=f"tests/test_{i}.py::test_{'z' * 40}")
        for i in range(400)
    )
    text = render_evidence(
        decision=code_bug_decision(),
        reproduction=reproduction(*attempts),
        change=ChangeSummary(base_sha="abc", head_sha="def"),
        evidence=[signal("determinism", "x" * 2000, {Route.CODE_BUG: 0.9})],
        failing_targets=(),
    )

    assert len(text) <= MAX_COMMENT_CHARS
    assert "truncated" in text


def test_render_check_output_has_the_three_keys() -> None:
    output = render_check_output(
        decision=code_bug_decision(),
        reproduction=reproduction(attempt(0, passed=False)),
        evidence=[signal("determinism", "3/3 reruns failed", {Route.CODE_BUG: 0.9})],
    )

    assert set(output) == {"title", "summary", "text"}
    assert output["title"] == "Janus triage: CODE_BUG"
    assert "\n" not in output["summary"]
    assert "CODE_BUG" in output["summary"]
    assert len(output["text"]) <= MAX_COMMENT_CHARS


def test_check_conclusion_maps_each_route() -> None:
    assert check_conclusion(Route.CODE_BUG) == "action_required"
    assert check_conclusion(Route.FLAKY) == "neutral"
    assert check_conclusion(Route.ENV) == "neutral"
    assert check_conclusion(Route.UNSURE) == "neutral"


def test_renderers_are_total_on_empty_inputs() -> None:
    empty = reproduction()
    change = ChangeSummary(base_sha=None, head_sha="def")

    text = render_evidence(
        decision=decide({}),
        reproduction=empty,
        change=change,
        evidence=[],
        failing_targets=(),
    )
    output = render_check_output(decision=decide({}), reproduction=empty, evidence=[])

    assert isinstance(text, str)
    assert text
    assert set(output) == {"title", "summary", "text"}
    assert "UNSURE" in text
    assert "no reruns recorded" in text
