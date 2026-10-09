"""The data every conformance check runs against.

A :class:`ConformanceCase` bundles one adapter with the fixed inputs and expected outputs
the checks assert on. ``passing_result`` and ``failing_result`` are the results of running
the tests parsed from ``failing_log`` (the same targets), so their outcomes are a subset
of ``expected_target_ids``.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass

from janus.core.adapter import Adapter
from janus.sandbox.spec import SandboxResult

from . import fake_adapter

# --- Python-adapter hook -------------------------------------------------------------
# A later agent adds ``tests/conformance/python_case.py`` exposing a ``python_case()``
# factory that returns a ``ConformanceCase``. ``available_cases`` picks it up lazily and
# only when it imports cleanly, so this suite never depends on it existing.
_PYTHON_CASE_MODULE = "tests.conformance.python_case"
_PYTHON_CASE_FACTORY = "python_case"


@dataclass
class ConformanceCase:
    """One adapter plus the canned data the conformance checks exercise."""

    adapter: Adapter
    failing_log: str
    expected_target_ids: tuple[str, ...]
    passing_result: SandboxResult
    failing_result: SandboxResult
    coverage_result: SandboxResult
    mutation_result: SandboxResult


FAKE_CASE = ConformanceCase(
    adapter=fake_adapter.FakeAdapter(),
    failing_log=fake_adapter.FAILING_LOG,
    expected_target_ids=fake_adapter.EXPECTED_TARGET_IDS,
    passing_result=fake_adapter.PASSING_RESULT,
    failing_result=fake_adapter.FAILING_RESULT,
    coverage_result=fake_adapter.COVERAGE_RESULT,
    mutation_result=fake_adapter.MUTATION_RESULT,
)


def available_cases() -> list[ConformanceCase]:
    """Every conformance case that is currently importable.

    Always includes the fake case. The Python-adapter case is included only when its
    module imports and its factory returns a ``ConformanceCase``; anything else is
    silently skipped (``pytest.importorskip``-friendly: this never raises).
    """
    cases = [FAKE_CASE]
    python_case = _load_python_case()
    if python_case is not None:
        cases.append(python_case)
    return cases


def _load_python_case() -> ConformanceCase | None:
    try:
        module = importlib.import_module(_PYTHON_CASE_MODULE)
    except ImportError:
        return None
    factory = getattr(module, _PYTHON_CASE_FACTORY, None)
    if not callable(factory):
        return None
    try:
        case = factory()
    except Exception:  # noqa: BLE001 - a missing optional case must never break collection
        return None
    if not isinstance(case, ConformanceCase):
        return None
    return case
