"""Unit tests for the Python adapter's pytest translation.

Parses the recorded fixtures and asserts the contract: argv-only commands with declared
absolute artifacts, exact failing ids from the CI log, and total parsers.
"""

from __future__ import annotations

import json
from pathlib import Path

from janus.adapters.python import pytest_runner
from janus.adapters.python.adapter import TEST_REPORT_PATH
from janus.core.adapter import TestTarget as Target
from janus.sandbox.spec import SandboxPhase, SandboxResult
from tests.conformance.adapter_suite import assert_command

_RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "recorded"
_CI_LOG = (_RECORDED / "ci_log_failure.txt").read_text(encoding="utf-8")
_PASS_REPORT = (_RECORDED / "pytest_report_pass.json").read_bytes()
_FAIL_REPORT = (_RECORDED / "pytest_report_fail.json").read_bytes()

_EXPECTED_IDS = ("tests/test_math.py::test_subtract", "tests/test_ops.py::test_divide")


def _targets() -> tuple[Target, ...]:
    return tuple(Target(id=node, node_ids=(node,)) for node in _EXPECTED_IDS)


def _result(report: bytes, *, command: tuple[str, ...] = (), exit_code: int = 0) -> SandboxResult:
    return SandboxResult(
        image="python:3.12-slim",
        phase=SandboxPhase.TEST,
        exit_code=exit_code,
        command=command,
        artifacts={TEST_REPORT_PATH: report},
    )


def test_parse_failing_tests_reads_the_ci_log() -> None:
    targets = pytest_runner.parse_failing_tests(_CI_LOG)

    assert tuple(target.id for target in targets) == _EXPECTED_IDS
    assert all(target.node_ids == (target.id,) for target in targets)


def test_parse_failing_tests_dedupes_preserving_order() -> None:
    log = "FAILED a.py::t1 - x\nERROR a.py::t2 - y\nFAILED a.py::t1 - z\n"

    targets = pytest_runner.parse_failing_tests(log)

    assert tuple(target.id for target in targets) == ("a.py::t1", "a.py::t2")


def test_parse_failing_tests_accepts_bare_node_ids() -> None:
    targets = pytest_runner.parse_failing_tests("tests/test_a.py::test_b\n")

    assert tuple(target.id for target in targets) == ("tests/test_a.py::test_b",)


def test_parse_failing_tests_is_total() -> None:
    for garbage in ("", "   ", "   \n\t ", "{not json", "not a log at all !@#$%^&*()"):
        assert pytest_runner.parse_failing_tests(garbage) == []
    assert isinstance(pytest_runner.parse_failing_tests(_CI_LOG[: len(_CI_LOG) // 3]), list)


def test_test_command_declares_the_report_artifact() -> None:
    command = pytest_runner.test_command(_targets())

    assert_command(command)
    assert command.artifacts == (TEST_REPORT_PATH,)
    assert TEST_REPORT_PATH.startswith("/")
    for node in _EXPECTED_IDS:
        assert node in command.argv
    assert "--json-report" in command.argv
    assert f"--json-report-file={TEST_REPORT_PATH}" in command.argv


def test_test_command_whole_suite_has_no_node_ids() -> None:
    command = pytest_runner.test_command(())

    assert_command(command)
    assert command.argv[:3] == ("python", "-m", "pytest")
    assert not any("::" in element for element in command.argv)


def test_parse_results_reports_a_passing_run() -> None:
    command = pytest_runner.test_command(_targets()).argv

    outcomes = pytest_runner.parse_results(_result(_PASS_REPORT, command=command))

    assert len(outcomes) == len(_EXPECTED_IDS)
    assert {outcome.target_id for outcome in outcomes} == set(_EXPECTED_IDS)
    assert all(outcome.passed and outcome.sandbox_error is None for outcome in outcomes)


def test_parse_results_reports_a_failing_run() -> None:
    command = pytest_runner.test_command(_targets()).argv

    outcomes = pytest_runner.parse_results(_result(_FAIL_REPORT, command=command, exit_code=1))

    by_id = {outcome.target_id: outcome for outcome in outcomes}
    assert by_id["tests/test_math.py::test_subtract"].passed is False
    assert by_id["tests/test_ops.py::test_divide"].passed is False
    assert all(outcome.exit_code == 1 for outcome in outcomes)


def test_parse_results_marks_a_missing_requested_target() -> None:
    command = ("python", "-m", "pytest", *_EXPECTED_IDS)
    report = json.dumps(
        {"tests": [{"nodeid": _EXPECTED_IDS[0], "outcome": "passed", "duration": 0.0}]}
    ).encode("utf-8")

    outcomes = pytest_runner.parse_results(_result(report, command=command))

    missing = [outcome for outcome in outcomes if outcome.sandbox_error == "not_in_report"]
    assert [outcome.target_id for outcome in missing] == [_EXPECTED_IDS[1]]
    assert missing[0].passed is False


def test_parse_results_returns_empty_on_infrastructure_error() -> None:
    error = SandboxResult(
        image="python:3.12-slim",
        phase=SandboxPhase.TEST,
        error="daemon_unreachable",
        artifacts={TEST_REPORT_PATH: _PASS_REPORT},
    )

    assert pytest_runner.parse_results(error) == []


def test_parse_results_is_total() -> None:
    empty = SandboxResult(image="python:3.12-slim", phase=SandboxPhase.TEST)

    assert pytest_runner.parse_results(empty) == []
    assert pytest_runner.parse_results(_result(b"")) == []
    assert pytest_runner.parse_results(_result(b"   ")) == []
    assert pytest_runner.parse_results(_result(b"{not json")) == []
    assert pytest_runner.parse_results(_result(_PASS_REPORT[: len(_PASS_REPORT) // 2])) == []
    assert pytest_runner.parse_results(_result(b"\xff\xfe")) == []
