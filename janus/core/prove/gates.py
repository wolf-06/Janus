"""The four **measured proof gates**.

A generated fix is a claim: *the suite is green, the mutation score did not regress, coverage
did not regress, and no test file changed*. Each gate here turns measurements taken by the
sandbox into a :class:`GateResult`. The processor runs the sandbox; this module only compares
before and after.

The governing rule is **no claim without a gate**. A missing measurement never passes: a
missing suite result, mutation result, coverage value or test-hash map fails closed. That is
why :func:`evaluate` always returns all four results, even when every measurement is absent.

The complementary *static* gate lives in :mod:`janus.core.prove.policy`; it rejects cheating
shapes (test edits, skipped tests, weakened CI) before the expensive measurements are trusted.
Both must pass for a fix to be provable.

Everything here is pure and total: no clock, no network, no I/O beyond reading files in
:func:`test_file_hashes`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from janus.core.adapter import CoverageContext, MutationResult

GATE_SUITE = "suite_green"
GATE_MUTATION = "mutation_non_regression"
GATE_COVERAGE = "coverage_non_regression"
GATE_TEST_HASHES = "test_files_unchanged"

COVERAGE_EPSILON = 1e-9


@dataclass(frozen=True)
class GateResult:
    """One measured gate outcome, with the values a reader can audit.

    ``measured_value`` and ``baseline_value`` are strings so a rendered table can show both a
    number and a discrete state (``"unavailable"``, ``"match"``, ``"killed 4/10"``) without
    losing the comparison. They are display values only; ``passed`` is the verdict.
    """

    gate: str
    passed: bool
    measured_value: str | None = None
    baseline_value: str | None = None


def _format_number(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _mutation_value(result: MutationResult | None) -> str | None:
    if result is None:
        return None
    return f"killed {result.killed}/{result.total}"


def _suite_result(suite_passed: bool) -> GateResult:
    return GateResult(
        gate=GATE_SUITE,
        passed=suite_passed,
        measured_value="passed" if suite_passed else "failed",
    )


def _mutation_gate(
    before: MutationResult | None,
    after: MutationResult | None,
) -> GateResult:
    if before is None or after is None:
        return GateResult(
            gate=GATE_MUTATION,
            passed=False,
            measured_value="unavailable",
            baseline_value=_mutation_value(before),
        )
    passed = after.killed >= before.killed and after.total >= before.total
    return GateResult(
        gate=GATE_MUTATION,
        passed=passed,
        measured_value=_mutation_value(after),
        baseline_value=_mutation_value(before),
    )


def _coverage_gate(
    before: float | None,
    after: float | None,
) -> GateResult:
    if before is None or after is None:
        return GateResult(
            gate=GATE_COVERAGE,
            passed=False,
            measured_value="unavailable",
            baseline_value=_format_number(before) if before is not None else None,
        )
    passed = after >= before - COVERAGE_EPSILON
    return GateResult(
        gate=GATE_COVERAGE,
        passed=passed,
        measured_value=_format_number(after),
        baseline_value=_format_number(before),
    )


def _test_hash_gate(
    before: Mapping[str, str],
    after: Mapping[str, str],
) -> GateResult:
    passed = dict(before) == dict(after)
    changed = sorted(
        path for path in set(before) | set(after) if before.get(path) != after.get(path)
    )
    measured = "match" if passed else f"{len(changed)} file(s) changed"
    return GateResult(
        gate=GATE_TEST_HASHES,
        passed=passed,
        measured_value=measured,
        baseline_value=f"{len(before)} file(s)",
    )


def evaluate(
    *,
    suite_passed: bool,
    mutation_before: MutationResult | None,
    mutation_after: MutationResult | None,
    coverage_before: float | None,
    coverage_after: float | None,
    test_hashes_before: Mapping[str, str],
    test_hashes_after: Mapping[str, str],
) -> list[GateResult]:
    """Evaluate exactly four gates, in a stable order.

    The gate passes when:

    * ``suite_green`` — ``suite_passed`` is true;
    * ``mutation_non_regression`` — killed mutants and the mutant total are both
      non-decreasing;
    * ``coverage_non_regression`` — ``coverage_after >= coverage_before - 1e-9``;
    * ``test_files_unchanged`` — the before/after test-hash maps are equal.

    A missing mutation or coverage measurement **fails closed** with
    ``measured_value="unavailable"``. The suite flag and hash maps are always supplied by the
    caller, so their absence is represented by ``False`` and ``{}`` respectively.
    """
    return [
        _suite_result(suite_passed),
        _mutation_gate(mutation_before, mutation_after),
        _coverage_gate(coverage_before, coverage_after),
        _test_hash_gate(test_hashes_before, test_hashes_after),
    ]


def all_passed(results: Sequence[GateResult]) -> bool:
    """True only when there is at least one result and every result passed.

    An empty sequence is **not** a proof: with no gate there is no claim, so it returns
    ``False``.
    """
    return bool(results) and all(result.passed for result in results)


def test_file_hashes(
    root: Path,
    *,
    patterns: Sequence[str] = ("test_*.py", "*_test.py"),
) -> dict[str, str]:
    """Hash every test file under ``root/tests``.

    Each matching file is keyed by its path relative to ``root`` (POSIX separators) and
    mapped to the SHA-256 hex digest of its bytes. A missing ``root/tests`` directory yields
    ``{}``, and an unreadable file is skipped. Output order is deterministic when iterated.
    """
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        return {}

    found: set[Path] = set()
    for pattern in patterns:
        for path in tests_dir.rglob(pattern):
            if path.is_file():
                found.add(path)

    hashes: dict[str, str] = {}
    for path in sorted(found, key=lambda item: item.relative_to(root).as_posix()):
        try:
            digest = sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        hashes[path.relative_to(root).as_posix()] = digest
    return hashes


def coverage_percent(contexts: Sequence[CoverageContext]) -> float | None:
    """Count the distinct ``(file, line)`` pairs covered across ``contexts``.

    The ``CoverageContext`` type records which lines each test executed, not the universe of
    executable lines, so a true percentage is not knowable here. This function therefore
    returns the number of distinct covered pairs, and the coverage gate compares that value
    before and after a fix: consistency matters more than the absolute scale. An empty
    sequence returns ``None``; contexts that exist but cover nothing return ``0.0``.
    """
    if not contexts:
        return None
    covered: set[tuple[str, int]] = set()
    for context in contexts:
        for path, lines in context.files.items():
            for line in lines:
                covered.add((path, line))
    return float(len(covered))
