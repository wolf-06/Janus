"""Network policy derived from sandbox settings."""

from __future__ import annotations

from janus.sandbox.allowlist import NetworkPolicy, host_allowed, policy_from_settings
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


def test_enforced_only_with_enabled_and_hosts() -> None:
    assert NetworkPolicy(enabled=False).enforced is False
    assert NetworkPolicy(enabled=True).enforced is False
    assert NetworkPolicy(enabled=True, hosts=("pypi.org",)).enforced is True


def test_host_allowed_exact_match_is_case_insensitive() -> None:
    assert host_allowed("pypi.org", ["pypi.org"]) is True
    assert host_allowed("PyPI.ORG", ["pypi.org"]) is True
    assert host_allowed("other.org", ["pypi.org"]) is False


def test_host_allowed_ignores_a_trailing_dot() -> None:
    assert host_allowed("pypi.org.", ["pypi.org"]) is True
    assert host_allowed("pypi.org", ["pypi.org."]) is True


def test_host_allowed_leading_dot_is_a_wildcard() -> None:
    assert host_allowed("api.github.com", [".github.com"]) is True
    assert host_allowed("github.com", [".github.com"]) is True
    assert host_allowed("evilgithub.com", [".github.com"]) is False
    assert host_allowed("github.com.evil", [".github.com"]) is False


def test_host_allowed_empty_list_allows_any_host() -> None:
    assert host_allowed("anything.example", []) is True


def test_host_allowed_empty_host_is_never_allowed() -> None:
    assert host_allowed("", ["pypi.org"]) is False
    assert host_allowed("   ", ["pypi.org"]) is False


def test_network_policy_allows_delegates_to_host_allowed() -> None:
    policy = NetworkPolicy(enabled=True, hosts=(".github.com",))
    assert policy.allows("api.github.com") is True
    assert policy.allows("github.com") is True
    assert policy.allows("example.com") is False


def test_policy_from_settings_off() -> None:
    policy = policy_from_settings(SandboxSettings(network=False, allow_hosts=["pypi.org"]))
    assert policy.enabled is False
    assert policy.allows("pypi.org") is False


def test_policy_from_settings_on() -> None:
    policy = policy_from_settings(SandboxSettings(network=True))
    assert policy.enabled is True
    assert policy.allows("anything.example") is True
