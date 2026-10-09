"""Unit tests for the Python adapter's coverage translation.

Parses the recorded coverage fixture and asserts the contract: a single
``--cov-context=test`` command with a declared absolute artifact, and a total
``parse_coverage`` that drops non-positive line numbers.
"""

from __future__ import annotations

import json
from pathlib import Path

from janus.adapters.python import coverage
from janus.adapters.python.adapter import COVERAGE_REPORT_PATH
from janus.core.adapter import TestTarget as Target
from janus.sandbox.spec import SandboxPhase, SandboxResult
from tests.conformance.adapter_suite import assert_command

_RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "recorded"
_COVERAGE_JSON = (_RECORDED / "coverage.json").read_bytes()

_CONTEXT_A = "tests/test_math.py::test_subtract"
_CONTEXT_B = "tests/test_ops.py::test_divide"


def _result(raw: bytes) -> SandboxResult:
    return SandboxResult(
        image="python:3.12-slim",
        phase=SandboxPhase.TEST,
        artifacts={COVERAGE_REPORT_PATH: raw},
    )


def test_coverage_command_declares_the_report_artifact() -> None:
    target = Target(id=_CONTEXT_A, node_ids=(_CONTEXT_A,))

    command = coverage.coverage_command((target,))

    assert_command(command)
    assert command.artifacts == (COVERAGE_REPORT_PATH,)
    assert COVERAGE_REPORT_PATH.startswith("/")
    assert _CONTEXT_A in command.argv
    assert "--cov-context=test" in command.argv
    assert f"--cov-report=json:{COVERAGE_REPORT_PATH}" in command.argv


def test_coverage_command_whole_suite_has_no_node_ids() -> None:
    command = coverage.coverage_command(())

    assert_command(command)
    assert not any("::" in element for element in command.argv)


def test_parse_coverage_reads_the_contexts_mapping() -> None:
    contexts = coverage.parse_coverage(_result(_COVERAGE_JSON))

    by_id = {context.test_id: context for context in contexts}
    assert set(by_id) == {_CONTEXT_A, _CONTEXT_B}
    assert by_id[_CONTEXT_A].files["src/math.py"] == {1, 2, 4, 5}
    assert by_id[_CONTEXT_A].files["src/ops.py"] == {2}
    assert by_id[_CONTEXT_B].files == {"src/math.py": {1, 2}, "src/ops.py": {3, 4}}


def test_parse_coverage_drops_non_positive_lines() -> None:
    raw = json.dumps({"contexts": {"t": [["f.py", [0, -3, 2]]]}}).encode("utf-8")

    contexts = coverage.parse_coverage(_result(raw))

    assert contexts[0].files == {"f.py": {2}}


def test_parse_coverage_is_total() -> None:
    empty = SandboxResult(image="python:3.12-slim", phase=SandboxPhase.TEST)

    assert coverage.parse_coverage(empty) == []
    assert coverage.parse_coverage(_result(b"")) == []
    assert coverage.parse_coverage(_result(b"   ")) == []
    assert coverage.parse_coverage(_result(b"{not json")) == []
    assert coverage.parse_coverage(_result(_COVERAGE_JSON[: len(_COVERAGE_JSON) // 2])) == []
    assert coverage.parse_coverage(_result(b"\xff\xfe")) == []
    assert coverage.parse_coverage(_result(json.dumps({"contexts": []}).encode())) == []
