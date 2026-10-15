"""The four measured proof gates: comparisons, fail-closed behaviour, hashes and coverage."""

from __future__ import annotations

from pathlib import Path

from janus.core.adapter import CoverageContext, MutationResult
from janus.core.prove.gates import (
    GATE_COVERAGE,
    GATE_MUTATION,
    GATE_SUITE,
    GATE_TEST_HASHES,
    GateResult,
    all_passed,
    coverage_percent,
    evaluate,
)
from janus.core.prove.gates import test_file_hashes as hash_test_files

_DEFAULT_MUTATION_BEFORE = MutationResult(total=10, killed=5, survived=5)
_DEFAULT_MUTATION_AFTER = MutationResult(total=10, killed=5, survived=5)


def results(
    *,
    suite_passed: bool = True,
    mutation_before: MutationResult | None = _DEFAULT_MUTATION_BEFORE,
    mutation_after: MutationResult | None = _DEFAULT_MUTATION_AFTER,
    coverage_before: float | None = 80.0,
    coverage_after: float | None = 80.0,
    test_hashes_before: dict[str, str] | None = None,
    test_hashes_after: dict[str, str] | None = None,
) -> list[GateResult]:
    return evaluate(
        suite_passed=suite_passed,
        mutation_before=mutation_before,
        mutation_after=mutation_after,
        coverage_before=coverage_before,
        coverage_after=coverage_after,
        test_hashes_before={"tests/test_a.py": "aaa"}
        if test_hashes_before is None
        else test_hashes_before,
        test_hashes_after={"tests/test_a.py": "aaa"}
        if test_hashes_after is None
        else test_hashes_after,
    )


def by_gate(items: list[GateResult], gate: str) -> GateResult:
    return next(item for item in items if item.gate == gate)


def test_evaluate_always_returns_exactly_four_gates_in_order() -> None:
    items = results()

    assert [item.gate for item in items] == [
        GATE_SUITE,
        GATE_MUTATION,
        GATE_COVERAGE,
        GATE_TEST_HASHES,
    ]


def test_suite_gate_tracks_the_boolean() -> None:
    assert by_gate(results(suite_passed=True), GATE_SUITE).passed is True
    failed = by_gate(results(suite_passed=False), GATE_SUITE)
    assert failed.passed is False
    assert failed.measured_value == "failed"


def test_mutation_gate_passes_on_a_non_regression() -> None:
    gate = by_gate(
        results(
            mutation_before=MutationResult(total=10, killed=5, survived=5),
            mutation_after=MutationResult(total=12, killed=6, survived=6),
        ),
        GATE_MUTATION,
    )

    assert gate.passed is True


def test_mutation_gate_fails_when_killed_mutants_drop() -> None:
    gate = by_gate(
        results(
            mutation_before=MutationResult(total=10, killed=6, survived=4),
            mutation_after=MutationResult(total=10, killed=5, survived=5),
        ),
        GATE_MUTATION,
    )

    assert gate.passed is False


def test_mutation_gate_fails_when_the_total_drops() -> None:
    gate = by_gate(
        results(
            mutation_before=MutationResult(total=10, killed=5, survived=5),
            mutation_after=MutationResult(total=8, killed=5, survived=3),
        ),
        GATE_MUTATION,
    )

    assert gate.passed is False


def test_mutation_gate_fails_closed_without_both_measurements() -> None:
    for before, after in ((None, MutationResult(10, 5, 5)), (MutationResult(10, 5, 5), None)):
        gate = by_gate(results(mutation_before=before, mutation_after=after), GATE_MUTATION)
        assert gate.passed is False
        assert gate.measured_value == "unavailable"


def test_coverage_gate_passes_on_equal_or_higher_coverage() -> None:
    assert by_gate(results(coverage_before=80.0, coverage_after=80.0), GATE_COVERAGE).passed
    assert by_gate(
        results(coverage_before=80.0, coverage_after=80.0000000001), GATE_COVERAGE
    ).passed
    assert by_gate(results(coverage_before=80.0, coverage_after=90.0), GATE_COVERAGE).passed


def test_coverage_gate_fails_on_a_regression() -> None:
    gate = by_gate(results(coverage_before=80.0, coverage_after=79.9), GATE_COVERAGE)

    assert gate.passed is False


def test_coverage_gate_fails_closed_without_both_measurements() -> None:
    for before, after in ((None, 80.0), (80.0, None)):
        gate = by_gate(results(coverage_before=before, coverage_after=after), GATE_COVERAGE)
        assert gate.passed is False
        assert gate.measured_value == "unavailable"


def test_test_hash_gate_passes_when_the_maps_are_equal() -> None:
    gate = by_gate(
        results(
            test_hashes_before={"tests/test_a.py": "aaa", "tests/test_b.py": "bbb"},
            test_hashes_after={"tests/test_b.py": "bbb", "tests/test_a.py": "aaa"},
        ),
        GATE_TEST_HASHES,
    )

    assert gate.passed is True


def test_test_hash_gate_fails_when_a_test_changes_or_is_added() -> None:
    changed = by_gate(
        results(
            test_hashes_before={"tests/test_a.py": "aaa"},
            test_hashes_after={"tests/test_a.py": "zzz"},
        ),
        GATE_TEST_HASHES,
    )
    added = by_gate(
        results(
            test_hashes_before={"tests/test_a.py": "aaa"},
            test_hashes_after={"tests/test_a.py": "aaa", "tests/test_b.py": "bbb"},
        ),
        GATE_TEST_HASHES,
    )

    assert changed.passed is False
    assert added.passed is False


def test_all_passed_requires_every_gate_and_at_least_one() -> None:
    assert all_passed(results()) is True
    assert all_passed([GateResult(gate="x", passed=False)]) is False
    assert all_passed([]) is False


def test_test_file_hashes_hashes_only_test_files_and_detects_a_mutation(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    nested = tests / "nested"
    nested.mkdir(parents=True)
    (tests / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    (nested / "test_b.py").write_text("def test_b():\n    assert True\n", encoding="utf-8")
    (tests / "helper.py").write_text("HELPER = 1\n", encoding="utf-8")

    before = hash_test_files(tmp_path)

    assert set(before) == {"tests/test_a.py", "tests/nested/test_b.py"}
    assert all(len(digest) == 64 for digest in before.values())

    (tests / "test_a.py").write_text("def test_a():\n    assert False\n", encoding="utf-8")
    after = hash_test_files(tmp_path)

    assert after["tests/test_a.py"] != before["tests/test_a.py"]
    assert after["tests/nested/test_b.py"] == before["tests/nested/test_b.py"]


def test_test_file_hashes_returns_empty_for_a_missing_tests_directory(tmp_path: Path) -> None:
    assert hash_test_files(tmp_path) == {}


def test_coverage_percent_is_none_without_contexts() -> None:
    assert coverage_percent([]) is None


def test_coverage_percent_counts_distinct_file_line_pairs() -> None:
    contexts = [
        CoverageContext(test_id="t1", files={"a.py": {1, 2}, "b.py": {2}}),
        CoverageContext(test_id="t2", files={"a.py": {2, 3}}),
    ]

    assert coverage_percent(contexts) == 4.0


def test_coverage_percent_is_zero_for_present_but_empty_contexts() -> None:
    assert coverage_percent([CoverageContext(test_id="t1", files={})]) == 0.0
