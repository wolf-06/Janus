"""Run every conformance check against every available adapter case."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from .adapter_suite import (
    check_commands,
    check_coverage_shape,
    check_mutation_shape,
    check_parse_failing_tests,
    check_parse_results,
    check_parsers_are_total,
)
from .cases import ConformanceCase, available_cases

_CHECKS: tuple[Callable[[ConformanceCase], None], ...] = (
    check_commands,
    check_parsers_are_total,
    check_parse_failing_tests,
    check_parse_results,
    check_coverage_shape,
    check_mutation_shape,
)


@pytest.mark.parametrize("case", available_cases(), ids=lambda case: case.adapter.name)
@pytest.mark.parametrize("check", _CHECKS, ids=lambda check: check.__name__)
def test_adapter_conformance(
    case: ConformanceCase,
    check: Callable[[ConformanceCase], None],
) -> None:
    check(case)
