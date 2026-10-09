"""Pure helpers of the sandbox runner (no Docker daemon required)."""

from __future__ import annotations

import io
import tarfile
import threading
import types
from collections.abc import Sequence
from pathlib import Path

import pytest
from docker.errors import DockerException, NotFound

from janus.sandbox.image import ImageBuildError, ResolvedImage
from janus.sandbox.proxy import ProxyError
from janus.sandbox.runner import (
    _TRUNCATED_SUFFIX,
    SandboxRunner,
    _truncate,
    container_options,
    extract_artifacts,
    is_rootless,
    tar_directory,
)
from janus.sandbox.settings import ImageSource, SandboxSettings
from janus.sandbox.spec import SandboxPhase, SandboxResult, SandboxSpec, build_spec
from janus.sandbox.telemetry import ResourceSample


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


def test_container_options_accept_network_and_environment_overrides() -> None:
    spec = build_spec(SandboxSettings(), command=["pytest"])
    env = {"HTTP_PROXY": "http://proxy:8888"}

    options = container_options(spec, network_mode="janus-sbx-abc", environment=env)

    assert options["network_mode"] == "janus-sbx-abc"
    assert options["environment"] == env


def test_container_options_omit_environment_by_default() -> None:
    options = container_options(build_spec(SandboxSettings(), command=["pytest"]))
    assert "environment" not in options
    assert options["network_mode"] == "none"


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


class _FakeArchiveContainer:
    """A container whose ``get_archive`` returns canned tar bytes (or raises)."""

    def __init__(self, responses: dict[str, bytes | Exception]) -> None:
        self._responses = responses
        self.requested: list[str] = []

    def get_archive(self, path: str) -> tuple[list[bytes], dict[str, object]]:
        self.requested.append(path)
        response = self._responses[path]
        if isinstance(response, Exception):
            raise response
        return [response], {"name": path}


def _tar_file(name: str, content: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _tar_directory_member(name: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.type = tarfile.DIRTYPE
        tar.addfile(info)
    return buffer.getvalue()


def _tar_empty() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w"):
        pass
    return buffer.getvalue()


def test_extract_artifacts_returns_a_regular_file_byte_for_byte() -> None:
    container = _FakeArchiveContainer(
        {"/work/report.json": _tar_file("report.json", b'{"ok":true}')}
    )

    artifacts, errors = extract_artifacts(container, ["/work/report.json"], max_bytes=1024)

    assert artifacts == {"/work/report.json": b'{"ok":true}'}
    assert errors == {}


def test_extract_artifacts_reports_a_missing_path() -> None:
    container = _FakeArchiveContainer({"/work/gone.json": NotFound("no such file")})

    artifacts, errors = extract_artifacts(container, ["/work/gone.json"], max_bytes=1024)

    assert artifacts == {}
    assert errors == {"/work/gone.json": "not_found"}


def test_extract_artifacts_reports_an_oversized_member_without_truncating() -> None:
    container = _FakeArchiveContainer({"/work/big.json": _tar_file("big.json", b"x" * 50)})

    artifacts, errors = extract_artifacts(container, ["/work/big.json"], max_bytes=10)

    assert artifacts == {}
    assert errors == {"/work/big.json": "too_large"}


def test_extract_artifacts_reports_a_directory_member() -> None:
    container = _FakeArchiveContainer({"/work/dir": _tar_directory_member("dir")})

    artifacts, errors = extract_artifacts(container, ["/work/dir"], max_bytes=1024)

    assert artifacts == {}
    assert errors["/work/dir"].startswith("unreadable")


def test_extract_artifacts_reports_an_empty_archive() -> None:
    container = _FakeArchiveContainer({"/work/empty": _tar_empty()})

    artifacts, errors = extract_artifacts(container, ["/work/empty"], max_bytes=1024)

    assert artifacts == {}
    assert errors["/work/empty"].startswith("unreadable")


def test_extract_artifacts_reports_a_docker_error() -> None:
    container = _FakeArchiveContainer({"/work/boom": DockerException("daemon said no")})

    artifacts, errors = extract_artifacts(container, ["/work/boom"], max_bytes=1024)

    assert artifacts == {}
    assert errors["/work/boom"].startswith("unreadable:")


def test_extract_artifacts_never_raises_on_garbage() -> None:
    container = _FakeArchiveContainer({"/work/junk": b"not a tar at all"})

    artifacts, errors = extract_artifacts(container, ["/work/junk"], max_bytes=1024)

    assert artifacts == {}
    assert errors["/work/junk"].startswith("unreadable:")


def test_extract_artifacts_mixes_successes_and_failures() -> None:
    container = _FakeArchiveContainer(
        {"/work/a": _tar_file("a", b"a"), "/work/missing": NotFound("nope")}
    )

    artifacts, errors = extract_artifacts(container, ["/work/a", "/work/missing"], max_bytes=1024)

    assert artifacts == {"/work/a": b"a"}
    assert errors == {"/work/missing": "not_found"}


async def test_run_in_thread_returns_the_result() -> None:
    class _StubRunner(SandboxRunner):
        def run(self, spec: SandboxSpec, *, source_dir: Path | None = None) -> SandboxResult:
            return SandboxResult(image=spec.image, phase=spec.phase, exit_code=0)

    result = await _StubRunner().run_in_thread(build_spec(SandboxSettings(), command=["pytest"]))

    assert result.ok is True


class _StubExecResult:
    def __init__(self, exit_code: int, output: bytes) -> None:
        self.exit_code = exit_code
        self.output = output


class _FakeApi:
    """Minimal stand-in for the low-level ``client.api`` exec endpoints.

    ``exec_start`` returns a generator of ``(stdout, stderr)`` chunks and records how many it
    handed out, so a test can prove the runner drained the whole stream.
    """

    def __init__(
        self,
        chunks: list[tuple[bytes | None, bytes | None]] | None = None,
        *,
        exit_code: int | None = 0,
    ) -> None:
        self.chunks = list(chunks) if chunks is not None else [(b"out", b"err")]
        self.exit_code = exit_code
        self.consumed = 0
        self.created: list[list[str]] = []

    def exec_create(self, container: str, cmd: list[str], **kwargs: object) -> dict[str, str]:
        self.created.append(list(cmd))
        return {"Id": "exec-1"}

    def exec_start(self, exec_id: str, **kwargs: object) -> object:
        def _gen() -> object:
            for chunk in self.chunks:
                self.consumed += 1
                yield chunk

        return _gen()

    def exec_inspect(self, exec_id: str) -> dict[str, object]:
        return {"ExitCode": self.exit_code}


class _StubContainer:
    def __init__(
        self,
        exit_code: int,
        chunks: list[tuple[bytes | None, bytes | None]] | None = None,
    ) -> None:
        self._exit_code = exit_code
        self.attrs: dict[str, object] = {"State": {"OOMKilled": False}}
        self.removed = False
        self.id = "container-1"
        self.api = _FakeApi(chunks, exit_code=exit_code)
        self.client = types.SimpleNamespace(api=self.api)

    def start(self) -> None:
        return None

    def exec_run(self, command: list[str], **kwargs: object) -> object:
        # Only the work-dir preparation still goes through exec_run.
        return _StubExecResult(0, b"")

    def stats(self, stream: bool = False) -> dict[str, object]:
        return {"cpu_stats": {"cpu_usage": {"total_usage": 0}}, "memory_stats": {}}

    def put_archive(self, path: str, archive: object) -> bool:
        return True

    def kill(self) -> None:
        return None

    def remove(self, force: bool = False) -> bool:
        self.removed = True
        return True


class _StubContainers:
    def __init__(self, container: _StubContainer) -> None:
        self._container = container
        self.created_kwargs: list[dict[str, object]] = []

    def create(self, image: str, **kwargs: object) -> _StubContainer:
        self.created_kwargs.append(kwargs)
        return self._container


class _StubImages:
    def get(self, image: str) -> object:
        return object()

    def pull(self, image: str) -> object:
        return object()

    def remove(self, image: str, force: bool = False) -> bool:
        return True


class _StubClient:
    def __init__(
        self,
        *,
        exit_code: int = 0,
        security_options: list[str] | None = None,
    ) -> None:
        self.container = _StubContainer(exit_code)
        self.containers = _StubContainers(self.container)
        self.images = _StubImages()
        self._security_options = security_options if security_options is not None else []

    def info(self) -> dict[str, object]:
        return {"SecurityOptions": self._security_options}


def _patch_image(
    monkeypatch: pytest.MonkeyPatch, *, build: bool, tag: str = "janus-sandbox:testtag"
) -> tuple[list[str], list[str]]:
    built: list[str] = []
    removed: list[str] = []

    def _resolve(settings: SandboxSettings, source_dir: Path | None) -> ResolvedImage:
        return ResolvedImage(tag=tag, build=build, dockerfile="FROM x\n", context=None)

    def _build(client: object, resolved: ResolvedImage, settings: SandboxSettings) -> None:
        built.append(resolved.tag)

    def _remove(client: object, tag_: str) -> None:
        removed.append(tag_)

    monkeypatch.setattr("janus.sandbox.runner.resolve_image", _resolve)
    monkeypatch.setattr("janus.sandbox.runner.build_image", _build)
    monkeypatch.setattr("janus.sandbox.runner.remove_image", _remove)
    return built, removed


def test_a_built_image_is_removed_after_a_successful_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _StubClient(exit_code=0)
    settings = SandboxSettings(image_source=ImageSource.GENERATED)
    runner = SandboxRunner(client=client, settings=settings)
    built, removed = _patch_image(monkeypatch, build=True, tag="janus-sandbox:abc")

    result = runner.run(build_spec(settings, command=["pytest"]))

    assert result.ok is True
    assert result.image == "janus-sandbox:abc"
    assert built == ["janus-sandbox:abc"]
    assert removed == ["janus-sandbox:abc"]


def test_a_built_image_is_removed_after_a_failing_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _StubClient(exit_code=1)
    settings = SandboxSettings(image_source=ImageSource.GENERATED)
    runner = SandboxRunner(client=client, settings=settings)
    built, removed = _patch_image(monkeypatch, build=True, tag="janus-sandbox:def")

    result = runner.run(build_spec(settings, command=["pytest"]))

    assert result.ok is False
    assert built == ["janus-sandbox:def"]
    assert removed == ["janus-sandbox:def"]


def test_a_built_image_is_kept_when_remove_image_is_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _StubClient(exit_code=0)
    settings = SandboxSettings(image_source=ImageSource.GENERATED, remove_image=False)
    runner = SandboxRunner(client=client, settings=settings)
    built, removed = _patch_image(monkeypatch, build=True, tag="janus-sandbox:keep")

    runner.run(build_spec(settings, command=["pytest"]))

    assert built == ["janus-sandbox:keep"]
    assert removed == []


def test_a_prebuilt_image_is_never_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _StubClient(exit_code=0)
    settings = SandboxSettings(image_source=ImageSource.PREBUILT, image="python:3.12-slim")
    runner = SandboxRunner(client=client, settings=settings)
    built, removed = _patch_image(monkeypatch, build=False, tag="python:3.12-slim")

    result = runner.run(build_spec(settings, command=["pytest"]))

    assert result.ok is True
    assert built == []
    assert removed == []


def test_a_build_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _StubClient(exit_code=0)
    settings = SandboxSettings(image_source=ImageSource.GENERATED)
    runner = SandboxRunner(client=client, settings=settings)
    monkeypatch.setattr(
        "janus.sandbox.runner.resolve_image",
        lambda settings, source_dir: ResolvedImage(tag="t", build=True, context=None),
    )

    def _boom(client: object, resolved: ResolvedImage, settings: SandboxSettings) -> None:
        raise ImageBuildError("boom")

    monkeypatch.setattr("janus.sandbox.runner.build_image", _boom)

    result = runner.run(build_spec(settings, command=["pytest"]))

    assert result.error is not None
    assert result.error.startswith("image_build_failed:")
    assert client.container.removed is False


def test_run_passes_the_spec_artifacts_to_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _StubClient(exit_code=0)
    settings = SandboxSettings(image_source=ImageSource.PREBUILT, image="python:3.12-slim")
    runner = SandboxRunner(client=client, settings=settings)
    _patch_image(monkeypatch, build=False, tag="python:3.12-slim")
    calls: list[tuple[object, tuple[str, ...], int]] = []

    def _fake_extract(
        container: object, paths: Sequence[str], *, max_bytes: int
    ) -> tuple[dict[str, bytes], dict[str, str]]:
        calls.append((container, tuple(paths), max_bytes))
        return {"/work/report.json": b"report"}, {"/work/missing": "not_found"}

    monkeypatch.setattr("janus.sandbox.runner.extract_artifacts", _fake_extract)
    spec = build_spec(
        settings, command=["pytest"], artifacts=["/work/report.json", "/work/missing"]
    )

    result = runner.run(spec)

    assert len(calls) == 1
    container, paths, max_bytes = calls[0]
    assert container is client.container
    assert paths == ("/work/report.json", "/work/missing")
    assert max_bytes == settings.max_artifact_bytes
    assert result.artifacts == {"/work/report.json": b"report"}
    assert result.artifact_errors == {"/work/missing": "not_found"}


def test_run_skips_extraction_without_artifacts(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _StubClient(exit_code=0)
    settings = SandboxSettings(image_source=ImageSource.PREBUILT, image="python:3.12-slim")
    runner = SandboxRunner(client=client, settings=settings)
    _patch_image(monkeypatch, build=False, tag="python:3.12-slim")
    called: list[int] = []

    def _never(*args: object, **kwargs: object) -> tuple[dict[str, bytes], dict[str, str]]:
        called.append(1)
        return {}, {}

    monkeypatch.setattr("janus.sandbox.runner.extract_artifacts", _never)

    result = runner.run(build_spec(settings, command=["pytest"]))

    assert called == []
    assert result.artifacts == {}
    assert result.artifact_errors == {}


def test_exec_bounds_streamed_output_and_drains_the_whole_stream() -> None:
    limit = 16
    chunks: list[tuple[bytes | None, bytes | None]] = [(b"a" * 20, b"b" * 20)] * 5
    container = _StubContainer(exit_code=0, chunks=chunks)
    settings = SandboxSettings(max_output_bytes=limit)
    runner = SandboxRunner(settings=settings)
    spec = build_spec(settings, command=["pytest"])

    exit_code, stdout, stderr, timed_out, error = runner._exec(container, spec)

    assert exit_code == 0
    assert timed_out is False
    assert error is None
    assert stdout.startswith(b"a" * limit)
    assert len(stdout) == limit + len(_TRUNCATED_SUFFIX)
    assert _TRUNCATED_SUFFIX in stdout
    assert stderr.startswith(b"b" * limit)
    assert len(stderr) == limit + len(_TRUNCATED_SUFFIX)
    assert _TRUNCATED_SUFFIX in stderr
    # Every chunk was pulled, so a chatty command cannot block on a full pipe.
    assert container.api.consumed == len(chunks)


class _HangingApi:
    """An exec whose stream blocks until the container is killed."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = False
        self.exit_code = 0

    def exec_create(self, container: str, cmd: list[str], **kwargs: object) -> dict[str, str]:
        return {"Id": "exec-1"}

    def exec_start(self, exec_id: str, **kwargs: object) -> object:
        def _gen() -> object:
            self.started = True
            self.release.wait(30)
            return
            yield  # pragma: no cover - unreachable; marks this a generator

        return _gen()

    def exec_inspect(self, exec_id: str) -> dict[str, object]:
        return {"ExitCode": self.exit_code}


class _HangingContainer:
    def __init__(self, api: _HangingApi) -> None:
        self.api = api
        self.client = types.SimpleNamespace(api=api)
        self.id = "container-1"
        self.attrs: dict[str, object] = {"State": {"OOMKilled": False}}
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.api.release.set()


def test_exec_times_out_and_kills_a_command_that_never_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("janus.sandbox.runner._EXEC_KILL_GRACE_SECONDS", 0.5)
    container = _HangingContainer(_HangingApi())
    settings = SandboxSettings(timeout_seconds=0.05)
    runner = SandboxRunner(settings=settings)
    spec = build_spec(settings, command=["sleep", "60"])

    exit_code, _stdout, _stderr, timed_out, error = runner._exec(container, spec)

    assert timed_out is True
    assert exit_code is None
    assert error is None
    assert container.killed is True


def _capture_sample_interval(
    monkeypatch: pytest.MonkeyPatch, runner: SandboxRunner, spec: SandboxSpec
) -> float:
    captured: dict[str, float] = {}

    class _FakeSampler:
        def __init__(self, sample: object, *, interval: float) -> None:
            captured["interval"] = interval

        def start(self) -> _FakeSampler:
            return self

        def stop(self) -> ResourceSample:
            return ResourceSample()

    monkeypatch.setattr("janus.sandbox.runner.StatsSampler", _FakeSampler)
    runner.run(spec)
    return captured["interval"]


def test_runner_passes_the_configured_sample_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _StubClient(exit_code=0)
    settings = SandboxSettings(
        image_source=ImageSource.PREBUILT, telemetry_sample_interval_seconds=0.75
    )
    runner = SandboxRunner(client=client, settings=settings)
    _patch_image(monkeypatch, build=False, tag="python:3.12-slim")

    interval = _capture_sample_interval(
        monkeypatch, runner, build_spec(settings, command=["pytest"])
    )

    assert interval == 0.75


def test_explicit_sample_interval_wins_over_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _StubClient(exit_code=0)
    settings = SandboxSettings(
        image_source=ImageSource.PREBUILT, telemetry_sample_interval_seconds=0.75
    )
    runner = SandboxRunner(client=client, settings=settings, sample_interval=0.01)
    _patch_image(monkeypatch, build=False, tag="python:3.12-slim")

    interval = _capture_sample_interval(
        monkeypatch, runner, build_spec(settings, command=["pytest"])
    )

    assert interval == 0.01


def test_is_rootless_true_when_the_daemon_reports_it() -> None:
    class _Client:
        def info(self) -> dict[str, object]:
            return {"SecurityOptions": ["name=seccomp", "name=rootless"]}

    assert is_rootless(_Client()) is True  # type: ignore[arg-type]


def test_is_rootless_false_for_a_rootful_daemon() -> None:
    class _Client:
        def info(self) -> dict[str, object]:
            return {"SecurityOptions": ["name=seccomp,profile=builtin"]}

    assert is_rootless(_Client()) is False  # type: ignore[arg-type]


def test_is_rootless_is_defensive() -> None:
    class _NoOptions:
        def info(self) -> dict[str, object]:
            return {}

    class _NoneOptions:
        def info(self) -> dict[str, object]:
            return {"SecurityOptions": None}

    class _BadOptions:
        def info(self) -> dict[str, object]:
            return {"SecurityOptions": 5}

    class _NoInfo:
        def info(self) -> dict[str, object]:
            raise DockerException("no daemon")

    assert is_rootless(_NoOptions()) is False  # type: ignore[arg-type]
    assert is_rootless(_NoneOptions()) is False  # type: ignore[arg-type]
    assert is_rootless(_BadOptions()) is False  # type: ignore[arg-type]
    assert is_rootless(_NoInfo()) is False  # type: ignore[arg-type]


def test_run_returns_not_rootless_when_required_but_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _StubClient(exit_code=0, security_options=["name=seccomp"])
    settings = SandboxSettings(require_rootless=True)
    runner = SandboxRunner(client=client, settings=settings)
    _patch_image(monkeypatch, build=False, tag="python:3.12-slim")

    result = runner.run(build_spec(settings, command=["pytest"]))

    assert result.error == "not_rootless"
    assert result.exit_code is None


def test_run_proceeds_when_rootless_is_required_and_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _StubClient(exit_code=0, security_options=["name=rootless"])
    settings = SandboxSettings(require_rootless=True)
    runner = SandboxRunner(client=client, settings=settings)
    _patch_image(monkeypatch, build=False, tag="python:3.12-slim")

    result = runner.run(build_spec(settings, command=["pytest"]))

    assert result.error is None
    assert result.ok is True


def test_docker_uses_base_url_when_docker_host_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    def _client(base_url: str | None = None) -> object:
        calls["base_url"] = base_url
        return object()

    def _from_env(*args: object, **kwargs: object) -> object:
        calls["from_env"] = True
        return object()

    monkeypatch.setattr("janus.sandbox.runner.docker.DockerClient", _client)
    monkeypatch.setattr("janus.sandbox.runner.docker.from_env", _from_env)
    runner = SandboxRunner(
        settings=SandboxSettings(docker_host="unix:///run/user/1000/docker.sock")
    )

    runner._docker()

    assert calls["base_url"] == "unix:///run/user/1000/docker.sock"
    assert "from_env" not in calls


def test_docker_falls_back_to_from_env_without_a_docker_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    def _client(base_url: str | None = None) -> object:
        calls["base_url"] = base_url
        return object()

    def _from_env(*args: object, **kwargs: object) -> object:
        calls["from_env"] = True
        return object()

    monkeypatch.setattr("janus.sandbox.runner.docker.DockerClient", _client)
    monkeypatch.setattr("janus.sandbox.runner.docker.from_env", _from_env)
    runner = SandboxRunner(settings=SandboxSettings())

    runner._docker()

    assert calls.get("from_env") is True
    assert "base_url" not in calls


class _FakeProxySession:
    """Records enter/exit and exposes the network + env the runner must pass on."""

    instances: list[_FakeProxySession] = []

    def __init__(
        self,
        client: object,
        settings: SandboxSettings,
        *,
        allow_hosts: tuple[str, ...],
        run_id: str | None = None,
    ) -> None:
        self.allow_hosts = tuple(allow_hosts)
        self.internal_network = "janus-sbx-fake"
        self.upstream_network = "janus-sbx-up-fake"
        self.proxy_host = "janus-sbx-proxy-fake"
        self.entered = False
        self.exited = False
        _FakeProxySession.instances.append(self)

    def __enter__(self) -> _FakeProxySession:
        self.entered = True
        return self

    def __exit__(self, *exc: object) -> bool:
        self.exited = True
        return False

    def env(self) -> dict[str, str]:
        url = "http://janus-sbx-proxy-fake:8888"
        return {"HTTP_PROXY": url, "HTTPS_PROXY": url, "http_proxy": url, "https_proxy": url}


def _enforced_spec() -> SandboxSpec:
    settings = SandboxSettings(network=True, allow_hosts=["pypi.org"])
    return build_spec(settings, command=["pytest"], phase=SandboxPhase.SETUP, network=True)


def test_run_with_enforcement_opens_and_closes_a_proxy_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _StubClient(exit_code=0)
    settings = SandboxSettings(network=True, allow_hosts=["pypi.org"])
    runner = SandboxRunner(client=client, settings=settings)
    _patch_image(monkeypatch, build=False, tag="python:3.12-slim")
    _FakeProxySession.instances = []
    monkeypatch.setattr("janus.sandbox.runner.ProxySession", _FakeProxySession)

    result = runner.run(_enforced_spec())

    assert result.ok is True
    assert len(_FakeProxySession.instances) == 1
    session = _FakeProxySession.instances[0]
    assert session.entered is True
    assert session.exited is True
    assert session.allow_hosts == ("pypi.org",)
    kwargs = client.containers.created_kwargs[0]
    assert kwargs["network_mode"] == session.internal_network
    assert kwargs["environment"] == session.env()


class _FailingProxySession:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def __enter__(self) -> object:
        raise ProxyError("boom")

    def __exit__(self, *exc: object) -> bool:
        return False


def test_proxy_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _StubClient(exit_code=0)
    settings = SandboxSettings(network=True, allow_hosts=["pypi.org"])
    runner = SandboxRunner(client=client, settings=settings)
    _patch_image(monkeypatch, build=False, tag="python:3.12-slim")
    monkeypatch.setattr("janus.sandbox.runner.ProxySession", _FailingProxySession)

    result = runner.run(_enforced_spec())

    assert result.error is not None
    assert result.error.startswith("proxy_failed:")
    assert client.container.removed is False
