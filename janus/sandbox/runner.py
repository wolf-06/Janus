"""Run one command inside an ephemeral, locked-down container.

Mechanism, in order:

1. Ensure the image exists (pull it when allowed).
2. Create the container **without starting it**: no network, dropped capabilities,
   ``no-new-privileges``, size-limited ``tmpfs`` mounts for the work dir and ``/tmp``, and
   CPU/memory/PID limits. It is never given the Docker socket.
3. Start it, make the work dir writable (capabilities are dropped, so the copy is written
   with the run user's uid/gid and the dir is chmod-ed by root), and copy the source in with
   ``put_archive`` (the API behind ``docker cp``), so nothing is bind-mounted from the host.
4. Run the command with ``exec_run`` on a worker thread while sampling resource usage.
5. Enforce the wall-clock cap by killing the container if the command overruns.
6. Read the output, redact it, remove the container, and return a ``SandboxResult``.

Every failure — no daemon, missing image, overrun — is returned as a result, never raised,
so a bad sandbox run cannot crash the worker loop.

The root filesystem is left writable on purpose: ``docker cp`` refuses to write into a
read-only rootfs, and a read-only rootfs would force the source into a named volume. The
container is still isolated — it has no host bind mounts, no network, no capabilities, a
non-root user, resource limits, and its writable layer is discarded on removal.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, BinaryIO, cast

import docker
from docker.errors import DockerException, ImageNotFound
from docker.models.containers import Container

from janus.sandbox.redaction import redact_bytes
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


def _error(spec: SandboxSpec, message: str) -> SandboxResult:
    return SandboxResult(image=spec.image, phase=spec.phase, error=message, command=spec.command)


def _remove(container: Container) -> None:
    with contextlib.suppress(DockerException):
        container.remove(force=True)


def container_options(spec: SandboxSpec) -> dict[str, Any]:
    """The create-time options that implement the isolation and resource limits.

    Pure and separate from Docker so the limits can be asserted in a unit test.
    """
    return {
        "working_dir": spec.workdir,
        "network_mode": spec.network_mode,
        "tmpfs": {"/tmp": "rw,size=256m,mode=1777"},
        "mem_limit": f"{spec.memory_mb}m",
        "nano_cpus": int(spec.cpus * 1_000_000_000),
        "pids_limit": spec.pids_limit,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
    }


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


class SandboxRunner:
    """Creates, runs, and tears down sandbox containers."""

    def __init__(
        self,
        *,
        client: docker.DockerClient | None = None,
        sample_interval: float = 0.5,
    ) -> None:
        self._client = client
        self._sample_interval = sample_interval

    def _docker(self) -> docker.DockerClient:
        if self._client is None:
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

        Blocking. Callers on an event loop must use :meth:`run_in_thread`.
        """
        try:
            client = self._docker()
        except DockerException as exc:
            return _error(spec, f"daemon_unreachable: {exc}")

        try:
            self.ensure_image(spec.image, pull=spec.pull_image)
        except ImageNotFound:
            return _error(spec, f"image_unavailable: {spec.image}")
        except DockerException as exc:
            return _error(spec, f"image_unavailable: {exc}")

        try:
            container = client.containers.create(
                spec.image,
                command=list(_KEEPALIVE),
                user=spec.user,
                **container_options(spec),
            )
        except DockerException as exc:
            return _error(spec, f"container_create_failed: {exc}")

        start = time.monotonic()
        try:
            try:
                container.start()
            except DockerException as exc:
                return _error(spec, f"container_start_failed: {exc}")

            try:
                self._prepare_workdir(container, spec)
                if source_dir is not None:
                    self._copy_source(container, spec, source_dir)
            except (DockerException, OSError, SandboxPreparationError) as exc:
                return _error(spec, f"copy_failed: {exc}")

            sampler = StatsSampler(
                lambda: self._sample(container), interval=self._sample_interval
            ).start()
            try:
                exit_code, stdout, stderr, timed_out, exec_error = self._exec(container, spec)
            finally:
                sample = sampler.stop()

            duration = time.monotonic() - start
            oom_killed = bool(
                cast(dict[str, Any], container.attrs).get("State", {}).get("OOMKilled", False)
            )
            return SandboxResult(
                image=spec.image,
                phase=spec.phase,
                exit_code=exit_code,
                stdout=redact_bytes(_truncate(stdout, spec.max_output_bytes)),
                stderr=redact_bytes(_truncate(stderr, spec.max_output_bytes)),
                duration_s=duration,
                timed_out=timed_out,
                oom_killed=oom_killed or exit_code == 137,
                cpu_seconds=sample.cpu_seconds,
                peak_mem_mb=sample.peak_mem_mb,
                error=exec_error,
                command=spec.command,
            )
        finally:
            _remove(container)

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
        """Run the command, killing the container if it exceeds the time cap."""
        holder: dict[str, Any] = {}

        def _target() -> None:
            try:
                code, output = container.exec_run(
                    list(spec.command),
                    workdir=spec.workdir,
                    user=spec.user,
                    demux=True,
                )
                holder["code"] = code
                holder["output"] = output
            except DockerException as exc:  # pragma: no cover - depends on the daemon
                holder["error"] = str(exc)

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        thread.join(timeout=spec.timeout_seconds)

        if thread.is_alive():
            with contextlib.suppress(DockerException):
                container.kill()
            thread.join(timeout=_EXEC_KILL_GRACE_SECONDS)
            return None, b"", b"", True, None

        if "error" in holder:
            return None, b"", b"", False, f"exec_failed: {holder['error']}"

        output = holder.get("output") or (b"", b"")
        if isinstance(output, tuple):
            out_bytes, err_bytes = output
        else:
            out_bytes, err_bytes = output, b""
        return (
            cast(int | None, holder.get("code")),
            cast(bytes, out_bytes) or b"",
            cast(bytes, err_bytes) or b"",
            False,
            None,
        )
