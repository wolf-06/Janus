"""The language-agnostic adapter interface.

An adapter is a **pure translator**. It converts CI logs, sandbox results and tool
artifacts into the small, language-neutral data types the core understands, and it
converts the core's intent (which tests to run, which files to mutate) back into command
lines. It never runs anything itself: the core owns the sandbox, the K=5 reproduction
loop, retries and cost caps.

Three contract rules bind every adapter:

1. **Commands are argv lists, never shell strings.** ``setup_commands``,
   ``test_command``, ``coverage_command`` and ``mutation_command`` return ``Command``
   values whose ``argv`` the sandbox runner execs *without a shell*. No element may
   contain a shell metacharacter (``&&``, ``||``, ``;``, ``|``, ``$(``, a backtick,
   ``>`` or ``<``); building a command by string-joining is a bug.

2. **Every parser is total.** Given malformed, empty, truncated or unexpected input a
   parser returns an empty list or a zeroed result — it never raises. The sandbox output
   and the logs it feeds are untrusted; a parse failure must degrade, not crash the
   worker.

3. **The adapter holds no policy.** It never decides *whether* to run, *how often* to
   run, which profile to use, or what a result means for triage. Those are the core's
   calls. An adapter only translates.

Structured tool output (pytest JSON, coverage, mutation reports) leaves the container by
**artifact extraction**, not stdout scraping: a ``Command`` declares the absolute
container paths it writes, the runner reads them back into ``SandboxResult.artifacts``
(``path -> raw bytes``), and ``parse_results`` / ``parse_coverage`` / ``parse_mutation``
read them from there. The Python adapter lives in ``janus/adapters/python/``; the sample
fixture repo is later Phase 4 work; see ``docs/specs/ADAPTER.md``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from janus.sandbox.spec import SandboxResult


@dataclass(frozen=True)
class Command:
    """One command the sandbox runs, plus the artifacts it produces.

    ``argv`` is an argv list executed *without a shell* (see contract rule 1). ``artifacts``
    are absolute container paths the command writes that the runner reads back before the
    container is removed; each is unique, non-blank, and starts with ``/``. The core reads
    this field to know which paths to request from the sandbox, so a structured report the
    core needs is declared here rather than scraped from stdout.
    """

    argv: tuple[str, ...]
    artifacts: tuple[str, ...] = ()


@dataclass(frozen=True)
class TestTarget:
    """One failing test.

    ``id`` is how the test appears in CI logs (the string the adapter parsed out);
    ``node_ids`` is what the runner is given, one or more runner-specific selectors that
    identify the same test (a pytest node id, a parametrised case, …).
    """

    id: str
    node_ids: tuple[str, ...]


@dataclass(frozen=True)
class TestOutcome:
    """What one target did in one sandbox run."""

    target_id: str
    passed: bool
    duration_s: float
    exit_code: int
    stdout: str
    stderr: str
    sandbox_error: str | None = None


@dataclass(frozen=True)
class CoverageContext:
    """The lines one test executed, per file. The SBFL input for Phase 5."""

    test_id: str
    files: dict[str, set[int]]


@dataclass(frozen=True)
class MutationResult:
    """The mutation gate input for S2: how many mutants were killed or survived."""

    total: int
    killed: int
    survived: int


class Adapter(Protocol):
    """Translate between a language's tooling and the language-agnostic core."""

    name: str

    def parse_failing_tests(self, log: str) -> list[TestTarget]:
        """Extract the failing tests from a CI log.

        The log is the raw CI job log; the adapter owns whatever format it uses. Total:
        unparseable input yields ``[]``.
        """
        ...

    def setup_commands(self) -> list[Command]:
        """The commands that install dependencies, in order.

        May be empty when nothing needs installing. Each command is run in the sandbox's
        setup phase (network allowed).
        """
        ...

    def test_command(self, targets: Sequence[TestTarget]) -> Command:
        """The command that runs ``targets``.

        An empty ``targets`` means "the whole suite"; the argv is never empty.
        """
        ...

    def parse_results(self, result: SandboxResult) -> list[TestOutcome]:
        """Turn one sandbox run into per-target outcomes. Total: never raises."""
        ...

    def coverage_command(self, targets: Sequence[TestTarget]) -> Command:
        """The command that produces per-test coverage for ``targets``."""
        ...

    def parse_coverage(self, result: SandboxResult) -> list[CoverageContext]:
        """Read per-test coverage out of one sandbox run. Total: never raises."""
        ...

    def mutation_command(self, files: Sequence[str]) -> Command:
        """The command that runs mutation testing over ``files``."""
        ...

    def parse_mutation(self, result: SandboxResult) -> MutationResult:
        """Read the mutation result out of one sandbox run. Total: never raises."""
        ...
