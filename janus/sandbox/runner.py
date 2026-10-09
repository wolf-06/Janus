"""Run one command inside an ephemeral, locked-down container.

Mechanism, in order:

0. Resolve the image for this run and, when it is not ``prebuilt``, build it fresh (ADR
   D-056). The image provides the environment only; the source is still copied in at step 3.
1. Ensure the image exists — for ``prebuilt`` pull it when allowed; a built image is already
   local.
2. Create the container **without starting it**: no network, dropped capabilities,
   ``no-new-privileges``, size-limited ``tmpfs`` mounts for the work dir and ``/tmp``, and
   CPU/memory/PID limits. It is never given the Docker socket. When the spec carries a host
   allowlist, the container instead joins an internal network whose only route out is the
   allowlisting proxy (see ``janus.sandbox.proxy``).
3. Start it, make the work dir writable (capabilities are dropped, so the copy is written
   with the run user's uid/gid and the dir is chmod-ed by root), and copy the source in with
   ``put_archive`` (the API behind ``docker cp``), so nothing is bind-mounted from the host.
4. Run the command on a worker thread, streaming its output and bounding each stream at
   ``max_output_bytes`` while sampling resource usage.
5. Enforce the wall-clock cap by killing the container if the command overruns.
6. Read back any requested artifacts (absolute container paths) as raw bytes, then redact the
   bounded output, remove the container, remove the per-run image when this call built it, and
   return a ``SandboxResult``. Artifacts are read before removal because the container is the
   only place a structured result (a JSON report, coverage data) exists.

Every failure — no daemon, missing image, build failure, overrun — is returned as a result,
never raised, so a bad sandbox run cannot crash the worker loop.

The root filesystem is left writable on purpose: ``docker cp`` refuses to write into a
read-only rootfs, and a read-only rootfs would force the source into a named volume. The
container is still isolated — it has no host bind mounts, no network, no capabilities, a
non-root user, resource limits, and its writable layer is discarded on removal.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import shutil
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, BinaryIO, cast

import docker
from docker.errors import DockerException, ImageNotFound, NotFound
from docker.models.containers import Container

from janus.sandbox.image import (
    ImageBuildError,
    ImageResolutionError,
    ResolvedImage,
    build_image,
    remove_image,
    resolve_image,
)
from janus.sandbox.proxy import ProxyError, ProxySession
from janus.sandbox.redaction import redact_bytes
from janus.sandbox.settings import SandboxSettings
from janus.sandbox.spec import SandboxResult, SandboxSpec
from janus.sandbox.telemetry import ResourceSample, StatsSampler

DEFAULT_EXCLUDES = (
    ".git",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
)

# The container is kept alive by a no-op so the real command can be run with exec_run,
# which is what lets the source be copied in after startup.
_KEEPALIVE: tuple[str, ...] = ("sleep", "infinity")

_EXEC_KILL_GRACE_SECONDS = 5.0
_TAR_SPOOL_BYTES = 8 * 1024 * 1024


class SandboxPreparationError(RuntimeError):
    """Raised when preparing the container before the run fails."""


def _parse_user(user: str) -> tuple[int, int] | None:
    """``"1000:1000"`` → ``(1000, 1000)``; named users return ``None``."""
    parts = user.split(":", 1)
    try:
        uid = int(parts[0])
        gid = int(parts[1]) if len(parts) > 1 else uid
    except ValueError:
        return None
    return uid, gid


def _entry_filter(
    excluded: set[str], owner: tuple[int, int] | None
) -> Callable[[tarfile.TarInfo], tarfile.TarInfo | None]:
    def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if any(part in excluded for part in Path(info.name).parts):
            return None
        if owner is not None:
            info.uid, info.gid = owner
        return info

    return _filter


def build_tar(
    src: Path,
    dest: BinaryIO,
    *,
    arcname: str = ".",
    exclude: Sequence[str] = DEFAULT_EXCLUDES,
    owner: tuple[int, int] | None = None,
) -> None:
    """Write a tar of ``src`` into ``dest`` for ``docker cp``.

    Excludes VCS and cache directories that are never needed inside the sandbox and can be
    large. ``owner`` rewrites entry ownership so the run user can read and write the copy.
    """
    with tarfile.open(fileobj=dest, mode="w") as tar:
        tar.add(src, arcname=arcname, filter=_entry_filter(set(exclude), owner))


def tar_directory(
    src: Path,
    *,
    arcname: str = ".",
    exclude: Sequence[str] = DEFAULT_EXCLUDES,
    owner: tuple[int, int] | None = None,
) -> bytes:
    """In-memory variant of :func:`build_tar`, used by tests."""
    buffer = io.BytesIO()
    build_tar(src, buffer, arcname=arcname, exclude=exclude, owner=owner)
    return buffer.getvalue()


def _first_regular_member(data: bytes, max_bytes: int) -> tuple[bytes | None, str | None]:
    """Return the first regular file in a tar ``data``, or an error reason.

    Returns ``(content, None)`` on success, ``(None, reason)`` otherwise. A tar with no regular
    member (only directories, or empty) is an error, not an empty file.
    """
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        member = next((candidate for candidate in tar.getmembers() if candidate.isfile()), None)
        if member is None:
            return None, "unreadable: no regular file in archive"
        if member.size > max_bytes:
            return None, "too_large"
        handle = tar.extractfile(member)
        if handle is None:
            return None, "unreadable: no regular file in archive"
        return handle.read(), None


def extract_artifacts(
    container: Container,
    paths: Sequence[str],
    *,
    max_bytes: int,
) -> tuple[dict[str, bytes], dict[str, str]]:
    """Read requested files back out of ``container`` before it is removed.

    For each absolute ``path``, ``get_archive`` returns a tar stream whose first regular member
    is the file's bytes. A missing path, a member larger than ``max_bytes``, a directory, an
    empty archive, or any daemon/OS error is recorded in the errors mapping instead of raised —
    extraction must never break a run. An oversized artifact is reported, never truncated.
    """
    artifacts: dict[str, bytes] = {}
    errors: dict[str, str] = {}
    for path in paths:
        try:
            chunks, _stat = container.get_archive(path)
            content, reason = _first_regular_member(b"".join(chunks), max_bytes)
        except NotFound:
            errors[path] = "not_found"
            continue
        except (DockerException, OSError, tarfile.TarError) as exc:
            errors[path] = f"unreadable: {exc}"
            continue
        if reason is not None:
            errors[path] = reason
        elif content is not None:
            artifacts[path] = content
        else:  # pragma: no cover - defensive; a None/None pair cannot occur
            errors[path] = "unreadable: no regular file in archive"
    return artifacts, errors


def _error(spec: SandboxSpec, message: str) -> SandboxResult:
    return SandboxResult(image=spec.image, phase=spec.phase, error=message, command=spec.command)


def _remove(container: Container) -> None:
    with contextlib.suppress(DockerException):
        container.remove(force=True)


def is_rootless(client: docker.DockerClient) -> bool:
    """Whether ``client`` talks to a rootless Docker daemon.

    Rootless Docker reports a ``rootless`` entry in the daemon's ``SecurityOptions``. Any
    missing or unexpected data (no daemon, no such key, a non-iterable value) is treated as
    *not* rootless, so a caller that requires rootless fails closed rather than guessing.
    """
    try:
        info = client.info()
    except Exception:  # noqa: BLE001 - a probe must never break a run
        return False
    if not isinstance(info, dict):
        return False
    try:
        options = info.get("SecurityOptions") or []
        return any("rootless" in str(option).lower() for option in options)
    except TypeError:
        return False


def container_options(
    spec: SandboxSpec,
    *,
    network_mode: str | None = None,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The create-time options that implement the isolation and resource limits.

    ``network_mode`` and ``environment`` default to the spec's own values; the allowlist
    proxy path overrides them so the container joins the internal network carrying the
    proxy environment. Pure and separate from Docker so the limits can be asserted in a
    unit test.
    """
    options: dict[str, Any] = {
        "working_dir": spec.workdir,
        "network_mode": spec.network_mode if network_mode is None else network_mode,
        "tmpfs": {"/tmp": "rw,size=256m,mode=1777"},
        "mem_limit": f"{spec.memory_mb}m",
        "nano_cpus": int(spec.cpus * 1_000_000_000),
        "pids_limit": spec.pids_limit,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
    }
    if environment is not None:
        options["environment"] = environment
    return options


def _cpu_seconds(stats: dict[str, Any]) -> float:
    total = stats.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0)
    return float(total) / 1e9


def _memory_mb(stats: dict[str, Any]) -> float:
    memory = stats.get("memory_stats", {})
    inner = memory.get("stats", {})
    # cgroup v1 reports ``cache``; v2 reports ``inactive_file``. Either way the reclaimable
    # page cache should not count against the peak working set.
    inactive = float(inner.get("inactive_file", inner.get("cache", 0)))
    usage = float(memory.get("usage", 0)) - inactive
    return max(usage, 0.0) / (1024 * 1024)


_TRUNCATED_SUFFIX = b"\n[... output truncated ...]\n"


def _truncate(data: bytes, limit: int) -> bytes:
    """Bound captured output so a chatty command cannot blow up redaction or storage."""
    if len(data) <= limit:
        return data
    return data[:limit] + _TRUNCATED_SUFFIX


def _append_bounded(buffer: bytearray, chunk: bytes, limit: int) -> bool:
    """Append ``chunk`` up to ``limit`` total bytes; return True when bytes were dropped."""
    remaining = limit - len(buffer)
    if remaining <= 0:
        return True
    if len(chunk) > remaining:
        buffer.extend(chunk[:remaining])
        return True
    buffer.extend(chunk)
    return False


class _OutputCapture:
    """Bounds a command's stdout/stderr *as it streams*.

    Each stream is capped at ``limit`` bytes; anything past the cap is dropped. The lock lets
    a caller read a partial result (after a kill) while the reader thread is still draining.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._stdout_truncated = False
        self._stderr_truncated = False

    def feed(self, stdout_chunk: bytes | None, stderr_chunk: bytes | None) -> None:
        with self._lock:
            if stdout_chunk and _append_bounded(self._stdout, stdout_chunk, self._limit):
                self._stdout_truncated = True
            if stderr_chunk and _append_bounded(self._stderr, stderr_chunk, self._limit):
                self._stderr_truncated = True

    def result(self) -> tuple[bytes, bytes]:
        with self._lock:
            stdout = bytes(self._stdout)
            stderr = bytes(self._stderr)
            if self._stdout_truncated:
                stdout += _TRUNCATED_SUFFIX
            if self._stderr_truncated:
                stderr += _TRUNCATED_SUFFIX
            return stdout, stderr


def _drain_output(
    stream: Iterator[tuple[bytes | None, bytes | None]], capture: _OutputCapture
) -> None:
    """Consume the whole exec stream, feeding the capture.

    Iteration continues after both streams reach their cap so a chatty command cannot block on
    a full pipe; the excess is discarded.
    """
    for stdout_chunk, stderr_chunk in stream:
        capture.feed(stdout_chunk, stderr_chunk)


def _stream_exec(container: Container, spec: SandboxSpec, capture: _OutputCapture) -> int | None:
    """Stream the command, bounding output, and return its real exit code.

    ``container.exec_run(..., stream=True)`` discards the exit code (docker-py returns ``None``
    whenever it streams), so the exec is created, started, and inspected through the low-level
    API to keep both the streamed output and the command's exit status.
    """
    api = container.client.api
    created = api.exec_create(
        container.id,
        list(spec.command),
        stdout=True,
        stderr=True,
        workdir=spec.workdir,
        user=spec.user,
    )
    exec_id = created["Id"]
    stream = api.exec_start(exec_id, stream=True, demux=True)
    _drain_output(stream, capture)
    return cast(int | None, api.exec_inspect(exec_id).get("ExitCode"))


class SandboxRunner:
    """Creates, runs, and tears down sandbox containers."""

    def __init__(
        self,
        *,
        client: docker.DockerClient | None = None,
        sample_interval: float | None = None,
        settings: SandboxSettings | None = None,
    ) -> None:
        self._client = client
        self._settings = settings if settings is not None else SandboxSettings()
        self._sample_interval = (
            sample_interval
            if sample_interval is not None
            else self._settings.telemetry_sample_interval_seconds
        )

    def _docker(self) -> docker.DockerClient:
        if self._client is None:
            if self._settings.docker_host is not None:
                self._client = docker.DockerClient(base_url=self._settings.docker_host)
            else:
                self._client = docker.from_env()
        return self._client

    def ensure_image(self, image: str, *, pull: bool) -> None:
        """Make sure ``image`` is present locally, pulling it when allowed."""
        client = self._docker()
        try:
            client.images.get(image)
            return
        except ImageNotFound:
            if not pull:
                raise
        client.images.pull(image)

    def run(
        self,
        spec: SandboxSpec,
        *,
        source_dir: Path | None = None,
    ) -> SandboxResult:
        """Run ``spec``, copying ``source_dir`` in first when given.

        Resolves the per-run image and, when it must be built, builds it before the container
        exists. A build context this call created is removed here, so the caller does not own
        it. Blocking. Callers on an event loop must use :meth:`run_in_thread`.

        Any requested ``spec.artifacts`` are read back after the command finishes and before
        the container is removed. Artifacts are returned **raw, not redacted** — a deliberate
        carve-out from the "all sandbox output is redacted" rule. They are structured data
        (a pytest JSON report, a coverage map) that the core parses, and redaction could
        corrupt them. The adapter must therefore configure its tools to exclude captured
        output from these files and must redact anything it surfaces from them.
        """
        try:
            client = self._docker()
        except DockerException as exc:
            return _error(spec, f"daemon_unreachable: {exc}")

        if self._settings.require_rootless and not is_rootless(client):
            return _error(spec, "not_rootless")

        resolved: ResolvedImage | None = None
        built = False
        try:
            try:
                resolved = resolve_image(self._settings, source_dir)
            except (ImageResolutionError, OSError) as exc:
                return _error(spec, f"image_resolution_failed: {exc}")

            if resolved.build:
                try:
                    build_image(client, resolved, self._settings)
                except ImageBuildError as exc:
                    return _error(spec, f"image_build_failed: {exc}")
                built = True
            else:
                try:
                    self.ensure_image(resolved.tag, pull=spec.pull_image)
                except ImageNotFound:
                    return _error(spec, f"image_unavailable: {resolved.tag}")
                except DockerException as exc:
                    return _error(spec, f"image_unavailable: {exc}")

            run_spec = replace(spec, image=resolved.tag)
            stack = contextlib.ExitStack()
            try:
                if run_spec.network and run_spec.allow_hosts:
                    try:
                        session = stack.enter_context(
                            ProxySession(client, self._settings, allow_hosts=run_spec.allow_hosts)
                        )
                    except ProxyError as exc:
                        return _error(run_spec, f"proxy_failed: {exc}")
                    options = container_options(
                        run_spec,
                        network_mode=session.internal_network,
                        environment=session.env(),
                    )
                else:
                    options = container_options(run_spec)

                try:
                    container = client.containers.create(
                        run_spec.image,
                        command=list(_KEEPALIVE),
                        user=run_spec.user,
                        **options,
                    )
                except DockerException as exc:
                    return _error(run_spec, f"container_create_failed: {exc}")

                start = time.monotonic()
                try:
                    try:
                        container.start()
                    except DockerException as exc:
                        return _error(run_spec, f"container_start_failed: {exc}")

                    try:
                        self._prepare_workdir(container, run_spec)
                        if source_dir is not None:
                            self._copy_source(container, run_spec, source_dir)
                    except (DockerException, OSError, SandboxPreparationError) as exc:
                        return _error(run_spec, f"copy_failed: {exc}")

                    sampler = StatsSampler(
                        lambda: self._sample(container), interval=self._sample_interval
                    ).start()
                    try:
                        exit_code, stdout, stderr, timed_out, exec_error = self._exec(
                            container, run_spec
                        )
                    finally:
                        sample = sampler.stop()

                    duration = time.monotonic() - start
                    oom_killed = bool(
                        cast(dict[str, Any], container.attrs)
                        .get("State", {})
                        .get("OOMKilled", False)
                    )
                    artifacts: dict[str, bytes] = {}
                    artifact_errors: dict[str, str] = {}
                    if run_spec.artifacts:
                        # Inside the container's try/finally, so the finally still removes it.
                        # On the timeout path the container is already killed; a partially
                        # written artifact is still better than none.
                        artifacts, artifact_errors = extract_artifacts(
                            container, run_spec.artifacts, max_bytes=run_spec.max_artifact_bytes
                        )
                    return SandboxResult(
                        image=run_spec.image,
                        phase=run_spec.phase,
                        exit_code=exit_code,
                        stdout=redact_bytes(_truncate(stdout, run_spec.max_output_bytes)),
                        stderr=redact_bytes(_truncate(stderr, run_spec.max_output_bytes)),
                        duration_s=duration,
                        timed_out=timed_out,
                        oom_killed=oom_killed or exit_code == 137,
                        cpu_seconds=sample.cpu_seconds,
                        peak_mem_mb=sample.peak_mem_mb,
                        error=exec_error,
                        command=run_spec.command,
                        artifacts=artifacts,
                        artifact_errors=artifact_errors,
                    )
                finally:
                    _remove(container)
            finally:
                stack.close()
        finally:
            self._cleanup_image(client, resolved, built=built, source_dir=source_dir)

    def _cleanup_image(
        self,
        client: docker.DockerClient,
        resolved: ResolvedImage | None,
        *,
        built: bool,
        source_dir: Path | None,
    ) -> None:
        """Remove the per-run image and the build context this call created.

        The image is removed only when this call built it and ``remove_image`` is set, so a
        prebuilt image is never touched. A context that is the caller's source dir is left
        alone; a generated context (a temp dir) is removed.
        """
        if resolved is None:
            return
        if built and self._settings.remove_image:
            remove_image(client, resolved.tag)
        if resolved.context is not None and resolved.context != source_dir:
            shutil.rmtree(resolved.context, ignore_errors=True)

    async def run_in_thread(
        self,
        spec: SandboxSpec,
        *,
        source_dir: Path | None = None,
    ) -> SandboxResult:
        """Run the blocking sandbox off the event loop."""
        return await asyncio.to_thread(self.run, spec, source_dir=source_dir)

    def _prepare_workdir(self, container: Container, spec: SandboxSpec) -> None:
        """Make the work dir writable for the run user.

        Capabilities are dropped, so root inside the container cannot ``chown`` (capability-
        less root is subject to normal DAC). It can, however, ``chmod`` a directory it owns.
        Both commands run as root and take the path as an argument, so no shell is involved.
        """
        for argv in (["mkdir", "-p", spec.workdir], ["chmod", "0777", spec.workdir]):
            result = container.exec_run(argv, user="root")
            if result.exit_code != 0:
                raise SandboxPreparationError(f"{' '.join(argv)} failed: {result.output!r}")

    def _copy_source(self, container: Container, spec: SandboxSpec, source_dir: Path) -> None:
        """Stream the source into the container without holding it all in memory."""
        owner = _parse_user(spec.user)
        with tempfile.SpooledTemporaryFile(max_size=_TAR_SPOOL_BYTES) as archive:
            build_tar(source_dir, cast(BinaryIO, archive), owner=owner)
            archive.seek(0)
            container.put_archive(spec.workdir, archive)

    def _sample(self, container: Container) -> ResourceSample:
        stats = cast(dict[str, Any], container.stats(stream=False))
        return ResourceSample(cpu_seconds=_cpu_seconds(stats), peak_mem_mb=_memory_mb(stats))

    def _exec(
        self, container: Container, spec: SandboxSpec
    ) -> tuple[int | None, bytes, bytes, bool, str | None]:
        """Run the command, streaming and bounding output, killing on the time cap.

        Output is captured as it arrives and capped at ``spec.max_output_bytes`` per stream,
        while the stream keeps draining so the command cannot block on a full pipe. On an
        overrun the container is killed and whatever was already emitted is kept.
        """
        capture = _OutputCapture(spec.max_output_bytes)
        holder: dict[str, Any] = {}

        def _target() -> None:
            try:
                holder["code"] = _stream_exec(container, spec, capture)
            except DockerException as exc:  # pragma: no cover - depends on the daemon
                holder["error"] = str(exc)

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        thread.join(timeout=spec.timeout_seconds)

        if thread.is_alive():
            with contextlib.suppress(DockerException):
                container.kill()
            thread.join(timeout=_EXEC_KILL_GRACE_SECONDS)
            stdout, stderr = capture.result()
            return None, stdout, stderr, True, None

        stdout, stderr = capture.result()

        if "error" in holder:
            return None, stdout, stderr, False, f"exec_failed: {holder['error']}"

        return (
            cast(int | None, holder.get("code")),
            stdout,
            stderr,
            False,
            None,
        )
