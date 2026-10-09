"""A canned, in-memory ``Adapter``.

It exists for two reasons: it proves the conformance suite is not vacuous (the checks
really do exercise a full adapter), and it is the template a future Node adapter copies —
the shape of every method, the argv discipline, the declared artifacts, and the totality of
every parser.

The adapter is a pure translator over fixed data; it never runs anything.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from janus.core.adapter import (
    Command,
    CoverageContext,
    MutationResult,
    TestOutcome,
    TestTarget,
)
from janus.sandbox.spec import SandboxPhase, SandboxResult

NAME = "fake"

NODE_ADD = "tests/test_math.py::test_add"
NODE_SUB = "tests/test_math.py::test_sub"

REPORT_PATH = "/work/pytest-report.json"
COVERAGE_PATH = "/work/coverage.json"
MUTMUT_PATH = "/work/mutmut.json"

FAILING_LOG = (
    "============================= test session starts ==============================\n"
    "collected 2 items\n"
    "\n"
    "tests/test_math.py FF                                                      [100%]\n"
    "\n"
    "=========================== short test summary info ============================\n"
    f"FAILED {NODE_ADD} - AssertionError: assert 3 == 4\n"
    f"FAILED {NODE_SUB} - AssertionError\n"
    "============================== 2 failed in 0.12s ===============================\n"
)

EXPECTED_TARGET_IDS: tuple[str, ...] = (NODE_ADD, NODE_SUB)


def _report(*outcomes: tuple[str, str]) -> bytes:
    tests = [{"nodeid": node, "outcome": outcome, "duration": 0.01} for node, outcome in outcomes]
    return json.dumps({"tests": tests}).encode("utf-8")


PASSING_REPORT = _report((NODE_ADD, "passed"), (NODE_SUB, "passed"))
FAILING_REPORT = _report((NODE_ADD, "failed"), (NODE_SUB, "failed"))

PASSING_RESULT = SandboxResult(
    image="fake:latest",
    phase=SandboxPhase.TEST,
    exit_code=0,
    duration_s=1.0,
    command=("python", "-m", "pytest", NODE_ADD, NODE_SUB),
    artifacts={REPORT_PATH: PASSING_REPORT},
)

FAILING_RESULT = SandboxResult(
    image="fake:latest",
    phase=SandboxPhase.TEST,
    exit_code=1,
    duration_s=1.0,
    command=("python", "-m", "pytest", NODE_ADD, NODE_SUB),
    artifacts={REPORT_PATH: FAILING_REPORT},
)

COVERAGE_RESULT = SandboxResult(
    image="fake:latest",
    phase=SandboxPhase.TEST,
    exit_code=0,
    artifacts={
        COVERAGE_PATH: json.dumps(
            {
                "files": {
                    "src/math.py": {
                        "executed_lines": [1, 2, 3, 4],
                        "contexts": {
                            NODE_ADD: [1, 2, 3],
                            NODE_SUB: [1, 4],
                        },
                    }
                }
            }
        ).encode("utf-8")
    },
)

MUTATION_RESULT = SandboxResult(
    image="fake:latest",
    phase=SandboxPhase.TEST,
    exit_code=0,
    artifacts={MUTMUT_PATH: json.dumps({"total": 10, "killed": 8, "survived": 2}).encode("utf-8")},
)


class FakeAdapter:
    """An ``Adapter`` backed entirely by the canned data above."""

    name = NAME

    def parse_failing_tests(self, log: str) -> list[TestTarget]:
        targets: list[TestTarget] = []
        seen: set[str] = set()
        for line in log.splitlines():
            marker = "FAILED "
            index = line.find(marker)
            if index < 0:
                continue
            node = line[index + len(marker) :].strip().split(" ", 1)[0].strip()
            if not node or node in seen:
                continue
            seen.add(node)
            targets.append(TestTarget(id=node, node_ids=(node,)))
        return targets

    def setup_commands(self) -> list[Command]:
        return [Command(argv=("python", "-m", "pip", "install", "-r", "requirements.txt"))]

    def test_command(self, targets: Sequence[TestTarget]) -> Command:
        node_ids = tuple(node for target in targets for node in target.node_ids)
        base = ("python", "-m", "pytest", "-q")
        argv = (*base, "tests") if not node_ids else (*base, *node_ids)
        return Command(argv=argv, artifacts=(REPORT_PATH,))

    def parse_results(self, result: SandboxResult) -> list[TestOutcome]:
        if result.error is not None:
            return []
        raw = result.artifacts.get(REPORT_PATH)
        if not isinstance(raw, bytes):
            return []
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return []
        if not isinstance(payload, dict):
            return []
        tests = payload.get("tests")
        if not isinstance(tests, list):
            return []

        exit_code = result.exit_code if result.exit_code is not None else 0
        outcomes: list[TestOutcome] = []
        for item in tests:
            if not isinstance(item, dict):
                continue
            node = item.get("nodeid")
            if not isinstance(node, str) or not node:
                continue
            duration = item.get("duration", 0.0)
            if not isinstance(duration, int | float):
                duration = 0.0
            outcomes.append(
                TestOutcome(
                    target_id=node,
                    passed=item.get("outcome") == "passed",
                    duration_s=float(duration),
                    exit_code=exit_code,
                    stdout="",
                    stderr="",
                )
            )
        return outcomes

    def coverage_command(self, targets: Sequence[TestTarget]) -> Command:
        node_ids = tuple(node for target in targets for node in target.node_ids)
        base = (
            "python",
            "-m",
            "pytest",
            "--cov=.",
            "--cov-context=test",
            "--cov-report=json:coverage.json",
        )
        argv = (*base, "tests") if not node_ids else (*base, *node_ids)
        return Command(argv=argv, artifacts=(COVERAGE_PATH,))

    def parse_coverage(self, result: SandboxResult) -> list[CoverageContext]:
        raw = result.artifacts.get(COVERAGE_PATH)
        if not isinstance(raw, bytes):
            return []
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return []
        if not isinstance(payload, dict):
            return []
        files = payload.get("files")
        if not isinstance(files, dict):
            return []

        by_test: dict[str, dict[str, set[int]]] = {}
        for path, entry in files.items():
            if not isinstance(path, str) or not path or not isinstance(entry, dict):
                continue
            contexts = entry.get("contexts")
            if not isinstance(contexts, dict):
                continue
            for test_id, lines in contexts.items():
                if not isinstance(test_id, str) or not test_id or not isinstance(lines, list):
                    continue
                bucket = by_test.setdefault(test_id, {})
                target = bucket.setdefault(path, set())
                for line in lines:
                    if isinstance(line, int) and line > 0:
                        target.add(line)
        return [
            CoverageContext(test_id=test_id, files=files_map)
            for test_id, files_map in by_test.items()
        ]

    def mutation_command(self, files: Sequence[str]) -> Command:
        base = ("mutmut", "run", "--paths-to-mutate")
        argv = (*base, "src") if not files else (*base, *files)
        return Command(argv=argv, artifacts=(MUTMUT_PATH,))

    def parse_mutation(self, result: SandboxResult) -> MutationResult:
        raw = result.artifacts.get(MUTMUT_PATH)
        if not isinstance(raw, bytes):
            return MutationResult(total=0, killed=0, survived=0)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return MutationResult(total=0, killed=0, survived=0)
        if not isinstance(payload, dict):
            return MutationResult(total=0, killed=0, survived=0)

        total = _count(payload.get("total"))
        killed = min(_count(payload.get("killed")), total)
        survived = min(_count(payload.get("survived")), total - killed)
        return MutationResult(total=total, killed=killed, survived=survived)


def _count(value: object) -> int:
    """Coerce an artifact value to a non-negative int; anything else is zero."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    return 0
