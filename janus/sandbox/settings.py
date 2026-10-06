"""Sandbox configuration.

These values are read from the TOML config file (see ``janus.app.config``) so the sandbox
can be tuned without touching code: image, limits, and whether the container is allowed any
network at all. Environment variables override the file (``JANUS_SANDBOX__IMAGE``, using a
double underscore for nesting).
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator


class SandboxSettings(BaseModel):
    """How one sandbox container is created and run."""

    # Master switch; when disabled the worker runs the processor without a sandbox.
    enabled: bool = True

    # Janus-owned base image (pytest/coverage/mutmut preinstalled) so runs are repeatable.
    image: str = "janus/python:3.12"
    # Pull the image when it is not present locally. Set False for locally built images.
    pull_image: bool = True

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
    # ``allow_hosts`` cannot be enforced yet (the egress proxy is not built), so enabling
    # the network together with a host list is rejected rather than silently ignored.
    network: bool = False
    allow_hosts: list[str] = Field(default_factory=list)

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

    @field_validator("workdir")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("workdir must be an absolute container path")
        return value

    @field_validator("timeout_seconds", "cpus")
    @classmethod
    def _positive_float(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be greater than zero")
        return value

    @field_validator("memory_mb", "pids_limit", "max_output_bytes")
    @classmethod
    def _positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be greater than zero")
        return value

    @field_validator("allow_hosts")
    @classmethod
    def _clean_hosts(cls, value: list[str]) -> list[str]:
        return [host.strip().lower() for host in value if host.strip()]

    @model_validator(mode="after")
    def _hosts_require_enforcement(self) -> SandboxSettings:
        """Fail closed: host restrictions are not implemented, so do not pretend they are.

        Allowing the network with a host list would give the container unrestricted egress
        while the configuration reads as if it were restricted.
        """
        if self.network and self.allow_hosts:
            raise ValueError(
                "allow_hosts cannot be enforced yet: set network = false, or clear "
                "allow_hosts to accept unrestricted network access"
            )
        return self
