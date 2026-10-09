"""Per-test coverage translation for the Python adapter.

pytest-cov runs once with ``--cov-context=test`` (not once per test) and writes a JSON
report to ``COVERAGE_REPORT_PATH``. The report carries a ``contexts`` mapping: each
context (the test id) maps to a list of ``[file, [lines]]`` pairs, which ``parse_coverage``
turns into ``CoverageContext`` objects for the SBFL localiser (Phase 5).

The report is declared as an artifact by ``coverage_command``, so it leaves the container
through the artifact channel. ``ARTIFACT_DIR`` is on the writable root filesystem
(``/var/tmp``) rather than ``/tmp``: the runner mounts ``/tmp`` as a tmpfs, which the
extractor's archive endpoint cannot read back. This module is a pure translator: it holds no
policy and never runs coverage.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from janus.core.adapter import Command, CoverageContext, TestTarget
from janus.sandbox.spec import SandboxResult

ARTIFACT_DIR = "/var/tmp/janus"
COVERAGE_REPORT_PATH = f"{ARTIFACT_DIR}/coverage.json"


def coverage_command(targets: Sequence[TestTarget]) -> Command:
    """The pytest argv that produces per-test coverage for ``targets``.

    One ``--cov-context=test`` run covers every target; the JSON report goes to
    ``COVERAGE_REPORT_PATH`` and is declared as the artifact.
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
        "--cov",
        "--cov-context=test",
        f"--cov-report=json:{COVERAGE_REPORT_PATH}",
    )
    return Command(argv=argv, artifacts=(COVERAGE_REPORT_PATH,))


def parse_coverage(result: SandboxResult) -> list[CoverageContext]:
    """Read per-test coverage from one sandbox run. Total: never raises.

    A missing/unreadable/unparsable artifact, a payload without a ``contexts`` mapping, or
    a context whose entries carry no positive line numbers yields no context for that
    entry (or ``[]``). Non-positive line numbers are dropped.
    """
    raw = result.artifacts.get(COVERAGE_REPORT_PATH)
    if not isinstance(raw, bytes):
        return []
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    contexts = payload.get("contexts")
    if not isinstance(contexts, dict):
        return []

    parsed: list[CoverageContext] = []
    for test_id, entries in contexts.items():
        if not isinstance(test_id, str) or not test_id or not isinstance(entries, list):
            continue
        files: dict[str, set[int]] = {}
        for entry in entries:
            if not isinstance(entry, list) or len(entry) != 2:
                continue
            path, lines = entry
            if not isinstance(path, str) or not path or not isinstance(lines, list):
                continue
            bucket = files.setdefault(path, set())
            for line in lines:
                if isinstance(line, int) and not isinstance(line, bool) and line > 0:
                    bucket.add(line)
        if files:
            parsed.append(CoverageContext(test_id=test_id, files=files))
    return parsed
