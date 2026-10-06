"""Pure helpers of the sandbox runner (no Docker daemon required)."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

from janus.sandbox.runner import SandboxRunner, _truncate, container_options, tar_directory
from janus.sandbox.settings import SandboxSettings
from janus.sandbox.spec import SandboxResult, SandboxSpec, build_spec


def _names(payload: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
        return tar.getnames()


def test_tar_directory_includes_files(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hi")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")

    names = _names(tar_directory(tmp_path))

    assert any(name.endswith("hello.txt") for name in names)
    assert any(name.endswith("mod.py") for name in names)


def test_tar_directory_excludes_vcs_and_caches(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("keep")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_bytes(b"\x00")

    names = _names(tar_directory(tmp_path))

    assert any(name.endswith("keep.txt") for name in names)
    assert not any(".git" in name for name in names)
    assert not any("__pycache__" in name for name in names)


def test_tar_directory_uses_dot_arcname_by_default(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    names = _names(tar_directory(tmp_path))
    assert "." in names


def test_container_options_carry_the_limits() -> None:
    settings = SandboxSettings(
        workdir="/work",
        memory_mb=128,
        cpus=1.5,
        pids_limit=64,
        network=False,
    )
    spec = build_spec(settings, command=["pytest"])

    options = container_options(spec)

    assert options["working_dir"] == "/work"
    assert options["network_mode"] == "none"
    assert options["mem_limit"] == "128m"
    assert options["nano_cpus"] == 1_500_000_000
    assert options["pids_limit"] == 64
    assert options["cap_drop"] == ["ALL"]
    assert options["security_opt"] == ["no-new-privileges"]
    assert "/tmp" in options["tmpfs"]


def test_container_options_never_mount_the_docker_socket() -> None:
    options = container_options(build_spec(SandboxSettings(), command=["pytest"]))
    assert "volumes" not in options
    assert "binds" not in options
    assert "docker.sock" not in str(options)


def test_container_options_pass_the_workdir_to_the_keepalive() -> None:
    options = container_options(
        build_spec(SandboxSettings(workdir="/workspace"), command=["pytest"])
    )
    assert options["working_dir"] == "/workspace"


def test_tar_directory_sets_owner(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    payload = tar_directory(tmp_path, owner=(1000, 1001))

    with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
        members = tar.getmembers()

    assert members
    assert all(member.uid == 1000 and member.gid == 1001 for member in members)


def test_truncate_bounds_output() -> None:
    assert _truncate(b"short", 1024) == b"short"

    bounded = _truncate(b"x" * 100, 10)

    assert bounded.startswith(b"x" * 10)
    assert b"truncated" in bounded
    assert len(bounded) < 100


async def test_run_in_thread_returns_the_result() -> None:
    class _StubRunner(SandboxRunner):
        def run(self, spec: SandboxSpec, *, source_dir: Path | None = None) -> SandboxResult:
            return SandboxResult(image=spec.image, phase=spec.phase, exit_code=0)

    result = await _StubRunner().run_in_thread(build_spec(SandboxSettings(), command=["pytest"]))

    assert result.ok is True
