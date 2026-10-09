"""Docker-gated behavioural test for the Python adapter.

Skipped automatically when no Docker daemon is reachable. It builds a generated image with
pytest and pytest-json-report, runs the adapter's whole-suite ``test_command`` over a tiny
source tree, and checks the JSON report artifact comes back parsed into the right outcomes.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import docker
import pytest

from janus.adapters.python.adapter import PythonAdapter
from janus.sandbox.runner import SandboxRunner
from janus.sandbox.settings import ImageSource, SandboxSettings
from janus.sandbox.spec import build_spec


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        docker.from_env().ping()
    except Exception:  # noqa: BLE001 - any failure means "no usable daemon"
        return False
    return True


pytestmark = pytest.mark.skipif(not _docker_available(), reason="docker daemon not available")

IMAGE = "python:3.12-slim"
PASSING = "tests/test_ok.py::test_ok"
FAILING = "tests/test_bad.py::test_bad"


@pytest.fixture
def settings() -> SandboxSettings:
    return SandboxSettings(
        image=IMAGE,
        image_source=ImageSource.GENERATED,
        tooling_packages=["pytest", "pytest-json-report"],
        network=False,
        timeout_seconds=180.0,
        memory_mb=1024,
        cpus=1.0,
        pids_limit=256,
    )


def test_python_adapter_parses_a_real_sandbox_run(
    settings: SandboxSettings, tmp_path: Path
) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    (tests_dir / "test_bad.py").write_text("def test_bad():\n    assert 1 + 1 == 3\n")
    (tmp_path / "requirements.txt").write_text("")

    adapter = PythonAdapter()
    command = adapter.test_command(())
    spec = build_spec(settings, command=command.argv, artifacts=command.artifacts)

    result = SandboxRunner(settings=settings).run(spec, source_dir=tmp_path)

    assert result.error is None, result.error
    assert result.artifacts, result.artifact_errors

    outcomes = adapter.parse_results(result)
    by_id = {outcome.target_id: outcome for outcome in outcomes}
    assert by_id[PASSING].passed is True
    assert by_id[FAILING].passed is False
    assert by_id[FAILING].sandbox_error is None
