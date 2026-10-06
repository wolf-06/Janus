"""Sandbox spec construction and result semantics."""

from __future__ import annotations

from janus.sandbox.settings import SandboxSettings
from janus.sandbox.spec import SandboxPhase, SandboxResult, build_spec


def test_build_spec_copies_limits_from_settings() -> None:
    settings = SandboxSettings(
        image="janus/python:3.12",
        workdir="/work",
        user="1000:1000",
        timeout_seconds=120.0,
        memory_mb=1024,
        cpus=1.5,
        pids_limit=64,
        pull_image=False,
        max_output_bytes=4096,
    )
    spec = build_spec(settings, command=["pytest", "-q"])

    assert spec.image == "janus/python:3.12"
    assert spec.command == ("pytest", "-q")
    assert spec.workdir == "/work"
    assert spec.user == "1000:1000"
    assert spec.timeout_seconds == 120.0
    assert spec.memory_mb == 1024
    assert spec.cpus == 1.5
    assert spec.pids_limit == 64
    assert spec.pull_image is False
    assert spec.max_output_bytes == 4096
    assert spec.phase is SandboxPhase.TEST


def test_network_is_forced_off_when_settings_disallow_it() -> None:
    settings = SandboxSettings(network=False)
    spec = build_spec(settings, command=["pytest"], phase=SandboxPhase.SETUP, network=True)

    assert spec.network is False
    assert spec.network_mode == "none"
    assert spec.allow_hosts == ()


def test_network_default_follows_the_phase() -> None:
    settings = SandboxSettings(network=True)

    setup = build_spec(settings, command=["pip", "install", "-e", "."], phase=SandboxPhase.SETUP)
    test = build_spec(settings, command=["pytest"], phase=SandboxPhase.TEST)

    assert setup.network is True
    assert setup.network_mode == "bridge"
    assert test.network is False
    assert test.network_mode == "none"


def test_setup_phase_may_use_the_network() -> None:
    settings = SandboxSettings(network=True)
    spec = build_spec(settings, command=["pip", "install", "-e", "."], phase=SandboxPhase.SETUP)

    assert spec.network is True
    assert spec.network_mode == "bridge"


def test_result_ok_requires_clean_exit() -> None:
    assert SandboxResult(image="i", phase=SandboxPhase.TEST, exit_code=0).ok is True
    assert SandboxResult(image="i", phase=SandboxPhase.TEST, exit_code=1).ok is False
    assert (
        SandboxResult(image="i", phase=SandboxPhase.TEST, exit_code=0, timed_out=True).ok is False
    )
    assert SandboxResult(image="i", phase=SandboxPhase.TEST, exit_code=0, error="boom").ok is False
