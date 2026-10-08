"""Docker-gated integration tests for the sandbox runner.

Skipped automatically when no Docker daemon is reachable, so a CI machine without Docker
still passes the suite.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import docker
import pytest
from docker.errors import ImageNotFound

from janus.sandbox.runner import SandboxRunner
from janus.sandbox.settings import ImageSource, SandboxSettings
from janus.sandbox.spec import SandboxPhase, build_spec


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


@pytest.fixture
def settings() -> SandboxSettings:
    return SandboxSettings(
        image=IMAGE,
        image_source=ImageSource.PREBUILT,
        timeout_seconds=60.0,
        memory_mb=512,
        cpus=1.0,
        pids_limit=128,
    )


@pytest.fixture
def runner(settings: SandboxSettings) -> SandboxRunner:
    return SandboxRunner(settings=settings)


def test_runs_a_command_and_captures_output(
    runner: SandboxRunner, settings: SandboxSettings
) -> None:
    spec = build_spec(settings, command=["python", "-c", "print('hello from the box')"])

    result = runner.run(spec)

    assert result.error is None
    assert result.ok is True
    assert "hello from the box" in result.stdout


def test_copies_source_into_the_workdir(
    runner: SandboxRunner, settings: SandboxSettings, tmp_path: Path
) -> None:
    (tmp_path / "hello.txt").write_text("copied ok\n")
    spec = build_spec(settings, command=["cat", "/work/hello.txt"])

    result = runner.run(spec, source_dir=tmp_path)

    assert result.ok is True
    assert "copied ok" in result.stdout


def test_run_user_can_write_in_the_workdir(
    runner: SandboxRunner, settings: SandboxSettings, tmp_path: Path
) -> None:
    """Regression: capabilities are dropped, so the work dir must be made writable."""
    (tmp_path / "hello.txt").write_text("copied ok\n")
    spec = build_spec(settings, command=["sh", "-c", "touch /work/written && ls /work"])

    result = runner.run(spec, source_dir=tmp_path)

    assert result.ok is True, result.stderr
    assert "written" in result.stdout


def test_copied_files_are_owned_by_the_run_user(
    runner: SandboxRunner, settings: SandboxSettings, tmp_path: Path
) -> None:
    (tmp_path / "hello.txt").write_text("x")
    spec = build_spec(settings, command=["stat", "-c", "%u", "/work/hello.txt"])

    result = runner.run(spec, source_dir=tmp_path)

    assert result.ok is True
    assert result.stdout.strip() == "1000"


def test_no_docker_socket_inside_the_box(runner: SandboxRunner, settings: SandboxSettings) -> None:
    spec = build_spec(settings, command=["sh", "-c", "test -e /var/run/docker.sock"])

    result = runner.run(spec)

    assert result.exit_code != 0


def test_network_is_off_by_default(runner: SandboxRunner, settings: SandboxSettings) -> None:
    spec = build_spec(
        settings,
        command=["python", "-c", "import socket; socket.create_connection(('1.1.1.1', 80), 2)"],
    )

    result = runner.run(spec)

    assert result.exit_code != 0


def test_timeout_kills_the_container(runner: SandboxRunner, settings: SandboxSettings) -> None:
    tight = settings.model_copy(update={"timeout_seconds": 2.0})
    spec = build_spec(tight, command=["sleep", "60"])

    result = runner.run(spec)

    assert result.timed_out is True
    assert result.ok is False


def test_memory_limit_reaches_the_container(
    runner: SandboxRunner, settings: SandboxSettings
) -> None:
    """The requested memory cap must be visible in the container's cgroup.

    Whether the kernel actually OOM-kills on overrun is not asserted: it is host-dependent
    and has been observed to be non-deterministic. What we own is the configured limit.
    """
    tight = settings.model_copy(update={"memory_mb": 128})
    spec = build_spec(
        tight,
        command=[
            "sh",
            "-c",
            "cat /sys/fs/cgroup/memory.max 2>/dev/null "
            "|| cat /sys/fs/cgroup/memory/memory.limit_in_bytes",
        ],
    )

    result = runner.run(spec)

    assert result.ok is True
    assert result.stdout.strip() == str(128 * 1024 * 1024)


def test_output_is_redacted(runner: SandboxRunner, settings: SandboxSettings) -> None:
    spec = build_spec(
        settings,
        command=["python", "-c", "print('ghp_' + 'a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8')"],
    )

    result = runner.run(spec)

    assert "ghp_" not in result.stdout


def test_generated_image_is_built_then_removed(tmp_path: Path) -> None:
    """A generated image serves exactly one run and is gone afterwards."""
    (tmp_path / "requirements.txt").write_text("")
    settings = SandboxSettings(
        image=IMAGE,
        image_source=ImageSource.GENERATED,
        tooling_packages=[],
        timeout_seconds=60.0,
        memory_mb=512,
        cpus=1.0,
        pids_limit=128,
    )
    runner = SandboxRunner(settings=settings)
    spec = build_spec(settings, command=["python", "-c", "print('built ok')"])

    result = runner.run(spec, source_dir=tmp_path)

    assert result.ok is True, result.stderr
    assert "built ok" in result.stdout
    with pytest.raises(ImageNotFound):
        docker.from_env().images.get(result.image)


# --- Host-allowlist enforcement (network topology + allowlisting proxy) ------------------

_DIRECT_EGRESS_SCRIPT = (
    "import socket\n"
    "try:\n"
    "    socket.create_connection(('1.1.1.1', 80), 3)\n"
    "    print('connected')\n"
    "except OSError:\n"
    "    print('blocked')\n"
)

_CONNECT_SCRIPT = (
    "import os, socket\n"
    "url = os.environ['HTTP_PROXY']\n"
    "authority = url.split('//', 1)[1]\n"
    "host, port = authority.rsplit(':', 1)\n"
    "s = socket.create_connection((host, int(port)), 5)\n"
    "s.sendall(b'CONNECT {target} HTTP/1.1\\r\\nHost: {target}\\r\\n\\r\\n')\n"
    "print(s.recv(128).decode('latin-1'))\n"
)


def _enforced(settings: SandboxSettings) -> SandboxSettings:
    return settings.model_copy(update={"network": True, "allow_hosts": ["pypi.org"]})


def _run_in_box(settings: SandboxSettings, script: str):
    spec = build_spec(
        settings,
        command=["python", "-c", script],
        phase=SandboxPhase.SETUP,
        network=True,
    )
    return SandboxRunner(settings=settings).run(spec)


def test_enforced_hosts_block_direct_egress(settings: SandboxSettings) -> None:
    """The sandbox is on an internal network, so it cannot reach the internet directly."""
    result = _run_in_box(_enforced(settings), _DIRECT_EGRESS_SCRIPT)

    assert result.ok is True, result.stderr
    assert "blocked" in result.stdout
    assert "connected" not in result.stdout


def test_proxy_refuses_a_disallowed_connect(settings: SandboxSettings) -> None:
    script = _CONNECT_SCRIPT.format(target="example.com:443")

    result = _run_in_box(_enforced(settings), script)

    assert result.ok is True, result.stderr
    assert "403" in result.stdout


def test_proxy_does_not_refuse_a_listed_connect(settings: SandboxSettings) -> None:
    """The allowed host is not 403; a lack of internet (502) is tolerated."""
    script = _CONNECT_SCRIPT.format(target="pypi.org:443")

    result = _run_in_box(_enforced(settings), script)

    assert result.ok is True, result.stderr
    assert "403" not in result.stdout
