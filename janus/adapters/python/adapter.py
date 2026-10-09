"""The Python adapter: pytest, coverage.py contexts and mutmut behind the ``Adapter``.

``PythonAdapter`` is a pure translator that composes the three modules beside it. It holds
no sandbox, no configuration and no policy — it only builds argv commands (with the
container artifact paths they produce) and parses sandbox results and CI logs back into the
core's language-neutral types. See ``janus/core/adapter.py`` for the contract and
``docs/specs/ADAPTER.md`` for the interface.

The container artifact paths are module constants here (and mirrored in the modules that
write them): a command declares these absolute paths so the runner reads the structured
reports back before the container is removed. ``ARTIFACT_DIR`` is on the writable root
filesystem rather than ``/tmp`` because the runner mounts ``/tmp`` as a tmpfs the artifact
extractor cannot read.
"""

from __future__ import annotations

from collections.abc import Sequence

from janus.adapters.python import coverage as coverage_module
from janus.adapters.python import mutmut as mutmut_module
from janus.adapters.python import pytest_runner
from janus.core.adapter import (
    Command,
    CoverageContext,
    MutationResult,
    TestOutcome,
    TestTarget,
)
from janus.sandbox.spec import SandboxResult

ARTIFACT_DIR = "/var/tmp/janus"
TEST_REPORT_PATH = pytest_runner.TEST_REPORT_PATH
COVERAGE_REPORT_PATH = coverage_module.COVERAGE_REPORT_PATH


class PythonAdapter:
    """An ``Adapter`` for Python repositories using pytest, coverage.py and mutmut."""

    name = "python"

    def parse_failing_tests(self, log: str) -> list[TestTarget]:
        return pytest_runner.parse_failing_tests(log)

    def setup_commands(self) -> list[Command]:
        """No setup commands: tooling is baked into the generated sandbox image."""
        return []

    def test_command(self, targets: Sequence[TestTarget]) -> Command:
        return pytest_runner.test_command(targets)

    def parse_results(self, result: SandboxResult) -> list[TestOutcome]:
        return pytest_runner.parse_results(result)

    def coverage_command(self, targets: Sequence[TestTarget]) -> Command:
        return coverage_module.coverage_command(targets)

    def parse_coverage(self, result: SandboxResult) -> list[CoverageContext]:
        return coverage_module.parse_coverage(result)

    def mutation_command(self, files: Sequence[str]) -> Command:
        return mutmut_module.mutation_command(files)

    def parse_mutation(self, result: SandboxResult) -> MutationResult:
        return mutmut_module.parse_mutation(result)
