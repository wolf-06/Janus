"""Network policy derived from sandbox settings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from janus.sandbox.allowlist import NetworkPolicy, policy_from_settings
from janus.sandbox.settings import SandboxSettings


def test_disabled_allows_nothing() -> None:
    policy = NetworkPolicy(enabled=False)
    assert policy.allows("github.com") is False
    assert policy.docker_network_mode == "none"
    assert policy.describe() == "network: disabled"


def test_enabled_without_hosts_allows_any() -> None:
    policy = NetworkPolicy(enabled=True)
    assert policy.allows("anything.example") is True
    assert policy.docker_network_mode == "bridge"
    assert policy.describe() == "network: enabled (all hosts)"


def test_enabled_with_hosts_allows_only_those() -> None:
    policy = NetworkPolicy(enabled=True, hosts=("pypi.org", "github.com"))
    assert policy.allows("pypi.org") is True
    assert policy.allows("PyPI.org") is True
    assert policy.allows("evil.example") is False
    assert policy.describe() == "network: enabled (pypi.org, github.com)"


def test_policy_from_settings_off() -> None:
    policy = policy_from_settings(SandboxSettings(network=False, allow_hosts=["pypi.org"]))
    assert policy.enabled is False
    assert policy.allows("pypi.org") is False


def test_policy_from_settings_on() -> None:
    policy = policy_from_settings(SandboxSettings(network=True))
    assert policy.enabled is True
    assert policy.allows("anything.example") is True


def test_settings_reject_hosts_while_the_network_is_enabled() -> None:
    """Host restrictions cannot be enforced yet, so the config fails closed."""
    with pytest.raises(ValidationError):
        SandboxSettings(network=True, allow_hosts=["pypi.org"])
