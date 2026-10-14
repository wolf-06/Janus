"""SBFL ranking: the faulty line wins, passing-only lines vanish, and the maths is total."""

from __future__ import annotations

import math

import pytest

from janus.core.adapter import CoverageContext
from janus.core.localize.sbfl import SbflFormula, SuspectLine, localize


def _ctx(test_id: str, files: dict[str, set[int]]) -> CoverageContext:
    return CoverageContext(test_id=test_id, files=files)


def _hand_built() -> list[CoverageContext]:
    return [
        _ctx("fail_one", {"mod.py": {10, 20}}),
        _ctx("fail_two", {"mod.py": {10}}),
        _ctx("pass_one", {"mod.py": {10, 20, 30}}),
        _ctx("pass_two", {"mod.py": {30}}),
    ]


@pytest.mark.parametrize("formula", list(SbflFormula))
def test_true_faulty_line_ranks_first(formula: SbflFormula) -> None:
    suspects = localize(_hand_built(), {"fail_one", "fail_two"}, formula=formula)

    assert suspects[0] == SuspectLine(file="mod.py", line=10, score=suspects[0].score, rank=1)
    assert (suspects[0].file, suspects[0].line, suspects[0].rank) == ("mod.py", 10, 1)


def test_ochiai_score_matches_the_formula() -> None:
    suspects = localize(_hand_built(), {"fail_one", "fail_two"}, formula=SbflFormula.OCHIAI)

    by_line = {suspect.line: suspect for suspect in suspects}
    assert math.isclose(by_line[10].score, 2 / math.sqrt((2 + 0) * (2 + 1)), rel_tol=1e-12)
    assert math.isclose(by_line[20].score, 1 / math.sqrt((1 + 1) * (1 + 1)), rel_tol=1e-12)


def test_line_covered_only_by_passing_tests_is_absent() -> None:
    suspects = localize(_hand_built(), {"fail_one", "fail_two"})

    assert all(not (suspect.file == "mod.py" and suspect.line == 30) for suspect in suspects)


@pytest.mark.parametrize("formula", list(SbflFormula))
def test_scores_are_finite_and_non_negative(formula: SbflFormula) -> None:
    suspects = localize(_hand_built(), {"fail_one", "fail_two"}, formula=formula)

    assert suspects
    assert all(math.isfinite(suspect.score) and suspect.score >= 0.0 for suspect in suspects)


def test_ties_break_by_file_then_line() -> None:
    contexts = [
        _ctx("failing", {"b.py": {2}, "a.py": {9}}),
        _ctx("passing", {"c.py": {1}}),
    ]

    suspects = localize(contexts, {"failing"})

    assert [(s.file, s.line, s.rank) for s in suspects] == [
        ("a.py", 9, 1),
        ("b.py", 2, 2),
    ]
    assert suspects[0].score == suspects[1].score


def test_max_suspects_is_respected() -> None:
    contexts = [_ctx("failing", {"big.py": set(range(1, 11))})]

    assert len(localize(contexts, {"failing"}, max_suspects=3)) == 3
    assert localize(contexts, {"failing"}, max_suspects=0) == []


def test_empty_inputs_and_unknown_failing_ids_yield_nothing() -> None:
    passing = [_ctx("pass_one", {"a.py": {1}})]

    assert localize([], {"anything"}) == []
    assert localize(passing, set()) == []
    assert localize(passing, {"never_ran"}) == []
    assert localize(passing, {"pass_one"}) != []


def test_duplicate_contexts_do_not_double_count() -> None:
    contexts = [
        _ctx("failing", {"a.py": {1}}),
        _ctx("failing", {"a.py": {1}}),
        _ctx("passing", {"a.py": {1}}),
    ]

    suspects = localize(contexts, {"failing"})

    assert len(suspects) == 1
    assert math.isclose(suspects[0].score, 1 / math.sqrt(2), rel_tol=1e-12)


@pytest.mark.parametrize("formula", list(SbflFormula))
def test_degenerate_denominators_never_raise(formula: SbflFormula) -> None:
    only_failing = [_ctx("failing", {"a.py": {1}})]

    suspects = localize(only_failing, {"failing"}, formula=formula)

    assert len(suspects) == 1
    assert math.isfinite(suspects[0].score)
    assert suspects[0].score >= 0.0
