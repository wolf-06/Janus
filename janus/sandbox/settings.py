"""Sandbox configuration.

These values are read from the TOML config file (see ``janus.app.config``) so the sandbox
can be tuned without touching code: image, limits, and whether the container is allowed any
network at all. Environment variables override the file (``JANUS_SANDBOX__IMAGE``, using a
double underscore for nesting).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class ImageSource(StrEnum):
    """Where the per-run sandbox image comes from.

    Lives here rather than in ``image.py`` so ``image.py`` can import it without an import
    cycle: the resolver needs the settings types, not the other way round.
    """

    AUTO = "auto"
    REPO = "repo"
    GENERATED = "generated"
    PREBUILT = "prebuilt"


class SandboxSettings(BaseModel):
    """How one sandbox container is created and run."""

    # Master switch; when disabled the worker runs the processor without a sandbox.
    enabled: bool = True

    # Where the per-run image comes from. ``auto`` uses a Dockerfile at the repository root
    # when one exists, otherwise a Janus-generated one. Every built image serves one run only.
    image_source: ImageSource = ImageSource.AUTO
    # The ``prebuilt`` image, and the base for a generated Dockerfile when ``base_image`` is
    # unset. There is no published Janus image any more (see ADR D-056).
    image: str = "python:3.12-slim"
    # Override the base image for generated Dockerfiles; ``None`` means use ``image``.
    base_image: str | None = None
    # Pull the ``prebuilt`` image when it is not present locally. Set False for local builds.
    pull_image: bool = True
    # Tooling installed into a generated image, on top of the base.
    tooling_packages: list[str] = Field(
        default_factory=lambda: ["pytest", "pytest-json-report", "coverage", "mutmut"]
    )
    # Pull the base image while building; False assumes it is already present locally.
    pull_base_image: bool = True
    # Allow the build to reach the network (needed to install packages). False builds with
    # ``network_mode=none``, which only works for a base that needs no downloads.
    build_network: bool = True
    # Wall-clock cap for one image build; best-effort (the daemon may finish in the background).
    build_timeout_seconds: float = 900.0
    # Tag prefix for per-run images; the suffix is content-addressed from the build inputs.
    image_tag_prefix: str = "janus-sandbox"
    # Remove the per-run image after the run. A ``prebuilt`` image is never removed.
    remove_image: bool = True
    # How often resource usage is sampled while a command runs.
    telemetry_sample_interval_seconds: float = 0.2

    # Docker endpoint URL. ``None`` means use ``docker.from_env()`` (DOCKER_HOST, the default
    # socket, etc.). Set this to point at a rootless daemon socket.
    docker_host: str | None = None
    # Fail closed unless the daemon is rootless: a run returns ``error="not_rootless"`` rather
    # than using a rootful daemon. Off by default so local (plain) Docker still works.
    require_rootless: bool = False

    # Where the copied source lives inside the container, and the user it runs as.
    workdir: str = "/work"
    user: str = "1000:1000"

    # Wall-clock cap for a single command; the container is killed when it is exceeded.
    timeout_seconds: float = 900.0

    # Resource limits (cgroup v2).
    memory_mb: int = 2048
    cpus: float = 2.0
    pids_limit: int = 256

    # Upper bound on captured stdout/stderr kept per command, so a chatty (or hostile)
    # test suite cannot blow up memory or redaction time.
    max_output_bytes: int = 1_048_576

    # Network policy. ``network = false`` means no network at all *and* the setup phase is
    # skipped entirely, so only the offline test phase runs.
    #
    # ``allow_hosts`` is enforced by an allowlisting egress proxy on an internal network
    # (ADR D-059): the sandbox has no route out except through the proxy, which refuses any
    # host not listed. A leading-dot entry (``.github.com``) is a wildcard.
    network: bool = False
    allow_hosts: list[str] = Field(default_factory=list)

    # Allowlisting egress proxy, used only when ``network = true`` and ``allow_hosts`` is
    # non-empty. The port is what the sandbox's proxy environment variables point at.
    proxy_port: int = 8888
    proxy_memory_mb: int = 256
    proxy_pids_limit: int = 128

    # Phase A (setup) runs with network on; phase B (test) always runs offline. Empty
    # commands mean the phase is skipped.
    setup_command: list[str] = Field(default_factory=list)
    test_command: list[str] = Field(default_factory=list)

    @field_validator("image", "workdir", "user")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("base_image", "docker_host")
    @classmethod
    def _optional_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("image_tag_prefix")
    @classmethod
    def _valid_tag_prefix(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        if any(char.isspace() for char in value):
            raise ValueError("must not contain whitespace")
        return value

    @field_validator("workdir")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("workdir must be an absolute container path")
        return value

    @field_validator(
        "timeout_seconds", "cpus", "build_timeout_seconds", "telemetry_sample_interval_seconds"
    )
    @classmethod
    def _positive_float(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be greater than zero")
        return value

    @field_validator(
        "memory_mb",
        "pids_limit",
        "max_output_bytes",
        "proxy_memory_mb",
        "proxy_pids_limit",
    )
    @classmethod
    def _positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be greater than zero")
        return value

    @field_validator("proxy_port")
    @classmethod
    def _valid_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("must be between 1 and 65535")
        return value

    @field_validator("allow_hosts")
    @classmethod
    def _clean_hosts(cls, value: list[str]) -> list[str]:
        return [host.strip().lower() for host in value if host.strip()]
