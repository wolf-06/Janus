"""pytest translation for the Python adapter.

Everything pytest-specific lives here: parsing the failing test ids out of a CI log, and
building/reading the ``--json-report`` report.

The report is written to an **absolute container path** (``TEST_REPORT_PATH``) that the
``test_command`` declares as an artifact, so the runner reads it back into
``SandboxResult.artifacts`` and ``parse_results`` never has to scrape stdout. The report
is configured with ``--json-report-omit=streams,log,keywords,warnings`` so it carries no
captured output; the adapter must redact anything it later surfaces from it.

``ARTIFACT_DIR`` sits on the container's writable root filesystem (``/var/tmp``), **not**
under ``/tmp``: the runner mounts ``/tmp`` as a tmpfs, and the daemon's archive endpoint the
extractor uses cannot read files from that mount. A report written under ``/tmp`` is
silently ``not_found``; ``/var/tmp`` is sticky-writable for the run user and readable back.

This module is a pure translator: it holds no policy and never runs pytest.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from janus.core.adapter import Command, TestOutcome, TestTarget
from janus.sandbox.spec import SandboxResult

ARTIFACT_DIR = "/var/tmp/janus"
TEST_REPORT_PATH = f"{ARTIFACT_DIR}/pytest-report.json"

# A short-summary line: ``FAILED tests/test_x.py::test_a - AssertionError`` or
# ``ERROR tests/test_x.py::test_b - ZeroDivisionError``.
_SUMMARY_LINE_RE = re.compile(r"^(?:FAILED|ERROR)\s+(?P<node>\S+)")
# A bare node id on its own line (``tests/test_x.py::test_a``).
_NODE_ID_LINE_RE = re.compile(r"^(?P<node>\S+::\S+)\s*$")


def parse_failing_tests(log: str) -> list[TestTarget]:
    """Extract the failing test node ids from a pytest CI log.

    Recognises the short-summary lines pytest prints (``FAILED <nodeid>`` /
    ``ERROR <nodeid>``) and bare node ids (``path::test``). Unrelated lines are ignored,
    duplicates are dropped preserving first-seen order, and junk input yields ``[]``.
    """
    targets: list[TestTarget] = []
    seen: set[str] = set()
    for raw in log.splitlines():
        line = raw.strip()
        match = _SUMMARY_LINE_RE.match(line) or _NODE_ID_LINE_RE.match(line)
        if match is None:
            continue
        node = match.group("node").strip()
        if not node or node in seen:
            continue
        seen.add(node)
        targets.append(TestTarget(id=node, node_ids=(node,)))
    return targets


def test_command(targets: Sequence[TestTarget]) -> Command:
    """The pytest argv that runs ``targets`` (or the whole suite when empty).

    Always emits the JSON report to ``TEST_REPORT_PATH`` and declares it as the artifact.
    """
    node_ids = tuple(node for target in targets for node in target.node_ids)
    argv = (
        "python",
        "-m",
        "pytest",
        *node_ids,
        "-p",
        "no:cacheprovider",
        "-q",
        "--json-report",
        f"--json-report-file={TEST_REPORT_PATH}",
        "--json-report-omit=streams,log,keywords,warnings",
    )
    return Command(argv=argv, artifacts=(TEST_REPORT_PATH,))


def parse_results(result: SandboxResult) -> list[TestOutcome]:
    """Turn one pytest sandbox run into per-target outcomes. Total: never raises.

    An infrastructure error, a missing/unparsable report artifact, or a report with no
    ``tests`` list yields ``[]`` (the core inspects ``result.error`` itself). A requested
    target that is absent from the report gets ``passed=False`` with
    ``sandbox_error="not_in_report"``; requested node ids are read back from the run's
    ``command``.
    """
    if result.error is not None:
        return []

    raw = result.artifacts.get(TEST_REPORT_PATH)
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
    reported: set[str] = set()
    for item in tests:
        if not isinstance(item, dict):
            continue
        node = item.get("nodeid")
        if not isinstance(node, str) or not node:
            continue
        reported.add(node)
        duration = item.get("duration", 0.0)
        if not isinstance(duration, int | float) or isinstance(duration, bool):
            duration = 0.0
        stdout = item.get("stdout")
        stderr = item.get("stderr")
        outcomes.append(
            TestOutcome(
                target_id=node,
                passed=item.get("outcome") == "passed",
                duration_s=float(duration),
                exit_code=exit_code,
                stdout=stdout if isinstance(stdout, str) else "",
                stderr=stderr if isinstance(stderr, str) else "",
            )
        )

    for node in _requested_node_ids(result):
        if node not in reported:
            outcomes.append(
                TestOutcome(
                    target_id=node,
                    passed=False,
                    duration_s=0.0,
                    exit_code=exit_code,
                    stdout="",
                    stderr="",
                    sandbox_error="not_in_report",
                )
            )
    return outcomes


def _requested_node_ids(result: SandboxResult) -> list[str]:
    """The node ids a run was asked to select, recovered from its ``command``.

    ``parse_results`` only receives the result, so the request is read back out of the
    argv the adapter itself built. A node id is any argv element containing ``::``; the
    whole-suite command carries none.
    """
    requested: list[str] = []
    seen: set[str] = set()
    for element in result.command:
        if "::" in element and element not in seen:
            seen.add(element)
            requested.append(element)
    return requested
