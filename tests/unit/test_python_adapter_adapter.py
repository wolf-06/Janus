"""Unit tests for ``PythonAdapter`` itself: it composes the three modules and declares
absolute artifact paths, without holding any policy.
"""

from __future__ import annotations

from pathlib import Path

from janus.adapters.python.adapter import (
    ARTIFACT_DIR,
    COVERAGE_REPORT_PATH,
    TEST_REPORT_PATH,
    PythonAdapter,
)
from janus.core.adapter import Command, MutationResult
from janus.core.adapter import TestTarget as Target
from janus.sandbox.spec import SandboxPhase, SandboxResult
from tests.conformance.adapter_suite import assert_command

_RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "recorded"


def _read_bytes(name: str) -> bytes:
    return (_RECORDED / name).read_bytes()


def _read_text(name: str) -> str:
    return (_RECORDED / name).read_text(encoding="utf-8")


def test_paths_are_absolute_under_the_artifact_dir() -> None:
    for path in (TEST_REPORT_PATH, COVERAGE_REPORT_PATH):
        assert path.strip()
        assert path.startswith(f"{ARTIFACT_DIR}/")


def test_name_and_setup_commands() -> None:
    adapter = PythonAdapter()

    assert adapter.name == "python"
    assert adapter.setup_commands() == []


def test_every_command_is_a_valid_command() -> None:
    adapter = PythonAdapter()
    target = Target(id="tests/test_a.py::test_b", node_ids=("tests/test_a.py::test_b",))

    commands = [
        adapter.test_command(()),
        adapter.test_command((target,)),
        adapter.coverage_command((target,)),
        adapter.mutation_command(("src/math.py",)),
    ]

    for command in commands:
        assert isinstance(command, Command)
        assert_command(command)


def test_parse_failing_tests_delegates() -> None:
    targets = PythonAdapter().parse_failing_tests("FAILED tests/test_a.py::test_b - boom\n")

    assert [target.id for target in targets] == ["tests/test_a.py::test_b"]


def test_parse_results_delegates() -> None:
    adapter = PythonAdapter()
    command = adapter.test_command(()).argv
    result = SandboxResult(
        image="python:3.12-slim",
        phase=SandboxPhase.TEST,
        exit_code=1,
        command=command,
        artifacts={TEST_REPORT_PATH: _read_bytes("pytest_report_fail.json")},
    )

    outcomes = adapter.parse_results(result)

    assert {outcome.target_id for outcome in outcomes} == {
        "tests/test_math.py::test_subtract",
        "tests/test_ops.py::test_divide",
    }


def test_parse_coverage_delegates() -> None:
    result = SandboxResult(
        image="python:3.12-slim",
        phase=SandboxPhase.TEST,
        artifacts={COVERAGE_REPORT_PATH: _read_bytes("coverage.json")},
    )

    contexts = PythonAdapter().parse_coverage(result)

    assert {context.test_id for context in contexts} == {
        "tests/test_math.py::test_subtract",
        "tests/test_ops.py::test_divide",
    }


def test_parse_mutation_delegates() -> None:
    result = SandboxResult(
        image="python:3.12-slim",
        phase=SandboxPhase.TEST,
        stdout=_read_text("mutmut_stdout.txt"),
    )

    assert PythonAdapter().parse_mutation(result) == MutationResult(total=10, killed=8, survived=2)
