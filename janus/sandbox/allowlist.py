"""Network policy for the sandbox.

The default is *no network at all*. When the configuration allows network access the
container is attached to a bridge network, and ``hosts`` records which hosts a repository
asked for. A non-empty host list is *enforced* by an allowlisting egress proxy on an internal
network (see ``janus.sandbox.proxy``); this module owns the policy as data and the
host-matching rule so it can be asserted on without Docker.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from janus.sandbox.settings import SandboxSettings

NETWORK_MODE_BRIDGE = "bridge"
NETWORK_MODE_NONE = "none"


def docker_network_mode(enabled: bool) -> str:
    """The one authoritative mapping from "network allowed" to a Docker network mode."""
    return NETWORK_MODE_BRIDGE if enabled else NETWORK_MODE_NONE


def _normalise_host(host: str) -> str:
    """Lower-case a host and drop a single trailing dot (``github.com.`` → ``github.com``)."""
    host = host.strip().lower()
    if host.endswith("."):
        host = host[:-1]
    return host


def host_allowed(host: str, allow_hosts: Sequence[str]) -> bool:
    """True when ``host`` is reachable under ``allow_hosts``.

    Matching is case-insensitive and ignores a trailing dot. An entry beginning with a dot
    (``.github.com``) is a wildcard that matches both the suffix's apex (``github.com``) and
    any subdomain (``api.github.com``). An empty allow list means "any host".

    The proxy script embeds the same rule as data (see ``janus.sandbox.proxy``); keep the
    two in sync.
    """
    normalised = _normalise_host(host)
    if not normalised:
        return False
    if not allow_hosts:
        return True
    for entry in allow_hosts:
        candidate = _normalise_host(entry)
        if not candidate:
            continue
        if candidate.startswith("."):
            suffix = candidate[1:]
            if suffix and (normalised == suffix or normalised.endswith(candidate)):
                return True
        elif normalised == candidate:
            return True
    return False


@dataclass(frozen=True)
class NetworkPolicy:
    """Whether a sandbox may reach the network, and to which hosts."""

    enabled: bool = False
    hosts: tuple[str, ...] = ()

    def allows(self, host: str) -> bool:
        """True when the host is reachable under this policy.

        No network means nothing is reachable. With network on, an empty host list means
        "any host" (used for the dependency-install phase); otherwise only listed hosts.
        """
        if not self.enabled:
            return False
        return host_allowed(host, self.hosts)

    @property
    def enforced(self) -> bool:
        """True when a non-empty host list must be enforced by the egress proxy."""
        return self.enabled and bool(self.hosts)

    @property
    def docker_network_mode(self) -> str:
        """The Docker ``network_mode`` that implements this policy."""
        return docker_network_mode(self.enabled)

    def describe(self) -> str:
        """A short human-readable summary, used in logs and comments."""
        if not self.enabled:
            return "network: disabled"
        if not self.hosts:
            return "network: enabled (all hosts)"
        return f"network: enabled ({', '.join(self.hosts)})"


def policy_from_settings(settings: SandboxSettings) -> NetworkPolicy:
    """Derive the policy from sandbox settings."""
    return NetworkPolicy(enabled=settings.network, hosts=tuple(settings.allow_hosts))
