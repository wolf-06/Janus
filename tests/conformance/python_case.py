"""The Python adapter's conformance case.

``python_case()`` builds a :class:`~tests.conformance.cases.ConformanceCase` for
:class:`~janus.adapters.python.adapter.PythonAdapter` from the recorded fixtures in
``tests/fixtures/recorded/``. ``available_cases()`` imports this module lazily and calls the
factory, so a problem here degrades to "case skipped" rather than breaking collection.
"""

from __future__ import annotations

from pathlib import Path

from janus.adapters.python.adapter import (
    COVERAGE_REPORT_PATH,
    TEST_REPORT_PATH,
    PythonAdapter,
)
from janus.sandbox.spec import SandboxPhase, SandboxResult

from .cases import ConformanceCase

_RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "recorded"

_IMAGE = "python:3.12-slim"


def _read_bytes(name: str) -> bytes:
    return (_RECORDED / name).read_bytes()


def _read_text(name: str) -> str:
    return (_RECORDED / name).read_text(encoding="utf-8")


def python_case() -> ConformanceCase:
    """A conformance case over the Python adapter and the recorded fixtures."""
    adapter = PythonAdapter()
    failing_log = _read_text("ci_log_failure.txt")
    targets = adapter.parse_failing_tests(failing_log)
    command = adapter.test_command(targets).argv

    passing = SandboxResult(
        image=_IMAGE,
        phase=SandboxPhase.TEST,
        exit_code=0,
        command=command,
        artifacts={TEST_REPORT_PATH: _read_bytes("pytest_report_pass.json")},
    )
    failing = SandboxResult(
        image=_IMAGE,
        phase=SandboxPhase.TEST,
        exit_code=1,
        command=command,
        artifacts={TEST_REPORT_PATH: _read_bytes("pytest_report_fail.json")},
    )
    coverage_result = SandboxResult(
        image=_IMAGE,
        phase=SandboxPhase.TEST,
        exit_code=0,
        artifacts={COVERAGE_REPORT_PATH: _read_bytes("coverage.json")},
    )
    mutation_result = SandboxResult(
        image=_IMAGE,
        phase=SandboxPhase.TEST,
        exit_code=0,
        stdout=_read_text("mutmut_stdout.txt"),
    )

    return ConformanceCase(
        adapter=adapter,
        failing_log=failing_log,
        expected_target_ids=tuple(target.id for target in targets),
        passing_result=passing,
        failing_result=failing,
        coverage_result=coverage_result,
        mutation_result=mutation_result,
    )
