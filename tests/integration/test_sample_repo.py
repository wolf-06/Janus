"""Docker-gated integration test: the S1 sample repo runs in the sandbox.

Skipped automatically when no Docker daemon is reachable (same pattern as the other
sandbox integration tests). It copies ``tests/fixtures/sample_repo`` into a generated image
and runs two of its tests through the Python adapter: the deterministic logic bug (which
must fail) and the always-green control (which must pass).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import docker
import pytest

from janus.adapters.python.adapter import PythonAdapter
from janus.core.adapter import TestTarget as _TestTarget
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
SAMPLE_REPO = Path(__file__).resolve().parents[1] / "fixtures" / "sample_repo"
FAILING = "tests/test_logic.py::test_sum_to_includes_the_last_term"
CONTROL = "tests/test_passing.py::test_always_passes"


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


def test_sample_repo_logic_bug_fails_and_control_passes(settings: SandboxSettings) -> None:
    adapter = PythonAdapter()
    targets = [
        _TestTarget(id=FAILING, node_ids=(FAILING,)),
        _TestTarget(id=CONTROL, node_ids=(CONTROL,)),
    ]
    command = adapter.test_command(targets)
    spec = build_spec(settings, command=command.argv, artifacts=command.artifacts)

    result = SandboxRunner(settings=settings).run(spec, source_dir=SAMPLE_REPO)

    assert result.error is None, result.error
    assert result.artifacts, result.artifact_errors

    outcomes = {outcome.target_id: outcome for outcome in adapter.parse_results(result)}
    assert outcomes[FAILING].passed is False
    assert outcomes[FAILING].sandbox_error is None
    assert outcomes[CONTROL].passed is True
