"""Unit tests for the Python adapter's mutmut translation.

Parses the recorded mutmut stdout and asserts the contract: an argv-only command with no
artifact (the result is read from stdout), and a total ``parse_mutation`` that clamps.
"""

from __future__ import annotations

from pathlib import Path

from janus.adapters.python import mutmut
from janus.core.adapter import MutationResult
from janus.sandbox.spec import SandboxPhase, SandboxResult
from tests.conformance.adapter_suite import assert_command

_RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "recorded"
_MUTMUT_STDOUT = (_RECORDED / "mutmut_stdout.txt").read_text(encoding="utf-8")

_ZERO = MutationResult(total=0, killed=0, survived=0)


def _result(stdout: str) -> SandboxResult:
    return SandboxResult(image="python:3.12-slim", phase=SandboxPhase.TEST, stdout=stdout)


def test_mutation_command_is_argv_only_with_no_artifact() -> None:
    command = mutmut.mutation_command(("src/math.py",))

    assert_command(command)
    assert command.artifacts == ()
    assert command.argv[:4] == ("python", "-m", "mutmut", "run")
    assert "--paths-to-mutate" in command.argv
    assert "src/math.py" in command.argv


def test_parse_mutation_reads_the_summary() -> None:
    assert mutmut.parse_mutation(_result(_MUTMUT_STDOUT)) == MutationResult(
        total=10, killed=8, survived=2
    )


def test_parse_mutation_clamps_counts_to_the_total() -> None:
    parsed = mutmut.parse_mutation(_result("Killed: 9\nSurvived: 9\nTotal: 10\n"))

    assert parsed.total == 10
    assert parsed.killed + parsed.survived <= parsed.total


def test_parse_mutation_derives_the_total_from_killed_and_survived() -> None:
    assert mutmut.parse_mutation(_result("Killed: 3\nSurvived: 2\n")) == MutationResult(
        total=5, killed=3, survived=2
    )


def test_parse_mutation_is_total() -> None:
    assert mutmut.parse_mutation(SandboxResult(image="i", phase=SandboxPhase.TEST)) == _ZERO
    for junk in ("", "   ", "{not json", "\xff\xfe"):
        assert mutmut.parse_mutation(_result(junk)) == _ZERO
