"""The reusable adapter conformance checks.

Each check is a plain function taking a :class:`~tests.conformance.cases.ConformanceCase`,
so it can be called from pytest or directly from a script. They encode the adapter
contract: argv-only commands with declared absolute artifacts, total parsers, and the exact
shapes the core relies on.
"""

from __future__ import annotations

from janus.core.adapter import Command, MutationResult
from janus.sandbox.spec import SandboxPhase, SandboxResult

from .cases import ConformanceCase

_SHELL_METACHARACTERS = ("&&", "||", ";", "|", "$(", "`", ">", "<")

_GARBAGE_LOGS = (
    "",
    "   \n\t ",
    "not a log at all !@#$%^&*()",
)


def assert_argv(command: tuple[str, ...]) -> None:
    """A valid argv list: a non-empty tuple of strings with no shell metacharacters."""
    assert isinstance(command, tuple), f"expected a tuple, got {type(command).__name__}"
    assert command, "command must not be empty"
    for element in command:
        assert isinstance(element, str), f"non-string argv element: {element!r}"
        assert element, "argv elements must not be empty"
        for meta in _SHELL_METACHARACTERS:
            assert meta not in element, f"shell metacharacter {meta!r} in argv element {element!r}"


def assert_artifacts(artifacts: tuple[str, ...]) -> None:
    """Every declared artifact is a unique, non-blank, absolute container path."""
    assert isinstance(artifacts, tuple), f"expected a tuple, got {type(artifacts).__name__}"
    seen: set[str] = set()
    for path in artifacts:
        assert isinstance(path, str), f"non-string artifact path: {path!r}"
        assert path.strip(), "artifact paths must not be blank"
        assert path.startswith("/"), f"artifact path must be absolute: {path!r}"
        assert path not in seen, f"duplicate artifact path: {path!r}"
        seen.add(path)


def assert_command(command: Command) -> None:
    """A ``Command``: a valid argv list plus valid artifact declarations."""
    assert isinstance(command, Command), f"expected a Command, got {type(command).__name__}"
    assert_argv(command.argv)
    assert_artifacts(command.artifacts)


def check_commands(case: ConformanceCase) -> None:
    """Every command the adapter emits is a valid argv list; test commands name targets."""
    adapter = case.adapter

    for command in adapter.setup_commands():
        assert_command(command)

    whole_suite = adapter.test_command(())
    assert_command(whole_suite)

    targets = adapter.parse_failing_tests(case.failing_log)
    assert targets, "the failing log must yield at least one target"

    assert_command(adapter.test_command(targets))
    for target in targets:
        single = adapter.test_command((target,))
        assert_command(single)
        for node_id in target.node_ids:
            assert node_id in single.argv, f"test_command dropped node id {node_id!r}"

    assert_command(adapter.coverage_command(targets))
    assert_command(adapter.mutation_command(("src/math.py",)))


def check_parsers_are_total(case: ConformanceCase) -> None:
    """Malformed, empty and truncated input yields empty/zeroed values, never a raise."""
    adapter = case.adapter

    for garbage in _GARBAGE_LOGS:
        assert adapter.parse_failing_tests(garbage) == []
    truncated = case.failing_log[: len(case.failing_log) // 2]
    assert isinstance(adapter.parse_failing_tests(truncated), list)

    empty = SandboxResult(image="x", phase=SandboxPhase.TEST, stdout="", stderr="", exit_code=None)
    assert isinstance(adapter.parse_results(empty), list)
    assert adapter.parse_results(empty) == []

    error = SandboxResult(image="x", phase=SandboxPhase.TEST, error="daemon_unreachable")
    outcomes = adapter.parse_results(error)
    assert isinstance(outcomes, list)
    for outcome in outcomes:
        assert outcome.sandbox_error is not None

    no_artifacts = SandboxResult(image="x", phase=SandboxPhase.TEST)
    coverage_key = _artifact_key(case.coverage_result, "coverage.json")
    assert adapter.parse_coverage(no_artifacts) == []
    assert adapter.parse_coverage(_with_artifact(coverage_key, b"not json")) == []
    assert adapter.parse_coverage(_with_artifact(coverage_key, b"")) == []

    zero = MutationResult(total=0, killed=0, survived=0)
    mutation_key = _artifact_key(case.mutation_result, "mutmut.json")
    assert adapter.parse_mutation(no_artifacts) == zero
    assert adapter.parse_mutation(_with_artifact(mutation_key, b"{not json")) == zero


def check_parse_failing_tests(case: ConformanceCase) -> None:
    """The failing log parses to exactly the expected target ids."""
    targets = case.adapter.parse_failing_tests(case.failing_log)
    assert {target.id for target in targets} == set(case.expected_target_ids)
    for target in targets:
        assert target.id, "target id must not be empty"
        assert target.node_ids, "target must carry at least one node id"
        assert all(isinstance(node, str) and node for node in target.node_ids)


def check_parse_results(case: ConformanceCase) -> None:
    """A passing run is all-pass; a failing run has a failure; ids stay in the request."""
    adapter = case.adapter
    requested = set(case.expected_target_ids)

    passing = adapter.parse_results(case.passing_result)
    assert passing, "a passing result must yield at least one outcome"
    for outcome in passing:
        assert outcome.passed is True
        assert outcome.sandbox_error is None
        assert outcome.target_id in requested

    failing = adapter.parse_results(case.failing_result)
    assert failing, "a failing result must yield at least one outcome"
    assert any(not outcome.passed for outcome in failing)
    for outcome in failing:
        assert outcome.target_id in requested


def check_coverage_shape(case: ConformanceCase) -> None:
    """Coverage contexts carry a non-empty test id and positive line numbers."""
    contexts = case.adapter.parse_coverage(case.coverage_result)
    assert contexts, "the coverage result must yield at least one context"
    for context in contexts:
        assert context.test_id, "coverage test id must not be empty"
        assert context.files, "a coverage context must name at least one file"
        for path, lines in context.files.items():
            assert path, "coverage file paths must not be empty"
            for line in lines:
                assert line > 0, "coverage line numbers must be positive"


def check_mutation_shape(case: ConformanceCase) -> None:
    """The mutation result is non-negative and never claims more than the total."""
    result = case.adapter.parse_mutation(case.mutation_result)
    assert isinstance(result, MutationResult)
    assert result.total >= 0
    assert result.killed >= 0
    assert result.survived >= 0
    assert result.killed + result.survived <= result.total


def _artifact_key(result: SandboxResult, fallback: str) -> str:
    """The first artifact path a canned result carries, or ``fallback``."""
    return next(iter(result.artifacts), fallback)


def _with_artifact(key: str, data: bytes) -> SandboxResult:
    """A sandbox result carrying one artifact ``key -> data``."""
    return SandboxResult(image="x", phase=SandboxPhase.TEST, artifacts={key: data})
