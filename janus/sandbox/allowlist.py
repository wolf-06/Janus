"""Network policy for the sandbox.

The default is *no network at all*. When the configuration allows network access the
container is attached to the bridge network, and ``hosts`` records which hosts a repository
asked for. Enforcing that host list is the setup proxy's job (later phase); this module
owns the policy as data so it can be asserted on without Docker.
"""

from __future__ import annotations

from dataclasses import dataclass

from janus.sandbox.settings import SandboxSettings

NETWORK_MODE_BRIDGE = "bridge"
NETWORK_MODE_NONE = "none"


def docker_network_mode(enabled: bool) -> str:
    """The one authoritative mapping from "network allowed" to a Docker network mode."""
    return NETWORK_MODE_BRIDGE if enabled else NETWORK_MODE_NONE


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
        if not self.hosts:
            return True
        return host.strip().lower() in self.hosts

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
