"""Sandbox settings: TOML config file, ``[tool.janus]`` fallback and env overrides."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from janus.app.config import ConfigError, Settings, config_file_path, load_toml_config
from janus.sandbox.settings import ImageSource, SandboxSettings


def test_shipped_janus_toml_is_loaded() -> None:
    """The repository's janus.toml is picked up when running from the project root."""
    assert Path("janus.toml").is_file()
    settings = Settings(_env_file=None)
    assert settings.sandbox.image == "python:3.12-slim"
    assert settings.sandbox.image_source == ImageSource.AUTO
    assert settings.sandbox.network is False
    assert settings.sandbox.timeout_seconds == 900.0


def test_values_are_read_from_janus_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "janus.toml").write_text(
        '[sandbox]\nimage = "custom/img:1"\nnetwork = true\nmemory_mb = 512\n'
    )
    monkeypatch.chdir(tmp_path)

    settings = Settings(_env_file=None)

    assert settings.sandbox.image == "custom/img:1"
    assert settings.sandbox.network is True
    assert settings.sandbox.memory_mb == 512


def test_pyproject_tool_janus_is_the_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.janus.sandbox]\nimage = "py/img:2"\n')
    monkeypatch.chdir(tmp_path)

    assert Settings(_env_file=None).sandbox.image == "py/img:2"


def test_janus_toml_wins_over_pyproject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "janus.toml").write_text('[sandbox]\nimage = "a:1"\n')
    (tmp_path / "pyproject.toml").write_text('[tool.janus.sandbox]\nimage = "b:2"\n')
    monkeypatch.chdir(tmp_path)

    assert Settings(_env_file=None).sandbox.image == "a:1"


def test_env_overrides_the_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "janus.toml").write_text('[sandbox]\nimage = "file:1"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JANUS_SANDBOX__IMAGE", "env:9")

    assert Settings(_env_file=None).sandbox.image == "env:9"


def test_config_file_env_points_at_another_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = tmp_path / "other.toml"
    custom.write_text('[sandbox]\nimage = "other:1"\n')
    monkeypatch.setenv("JANUS_CONFIG_FILE", str(custom))

    assert Settings(_env_file=None).sandbox.image == "other:1"


def test_config_file_env_pointing_at_a_missing_file_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JANUS_CONFIG_FILE", str(tmp_path / "nope.toml"))

    with pytest.raises(ConfigError):
        Settings(_env_file=None)


def test_malformed_toml_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "janus.toml").write_text("this is not = = toml")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError):
        Settings(_env_file=None)


def test_config_is_found_from_a_subdirectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "janus.toml").write_text('[sandbox]\nimage = "root:1"\n')
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert Settings(_env_file=None).sandbox.image == "root:1"


def test_no_config_found_uses_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JANUS_CONFIG_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    if config_file_path() is not None:
        pytest.skip("an ancestor directory provides a config file")

    assert Settings(_env_file=None).sandbox == SandboxSettings()


def test_load_toml_config_returns_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "janus.toml").write_text('[sandbox]\nimage = "x:1"\n')
    monkeypatch.chdir(tmp_path)

    assert load_toml_config() == {"sandbox": {"image": "x:1"}}
    assert config_file_path() == tmp_path / "janus.toml"


def test_relative_workdir_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SandboxSettings(workdir="work")


def test_non_positive_limits_are_rejected() -> None:
    with pytest.raises(ValidationError):
        SandboxSettings(memory_mb=0)
    with pytest.raises(ValidationError):
        SandboxSettings(timeout_seconds=0)


def test_allow_hosts_are_normalised() -> None:
    settings = SandboxSettings(allow_hosts=["  PyPI.org ", "", "github.com"])
    assert settings.allow_hosts == ["pypi.org", "github.com"]


def test_network_with_allow_hosts_is_accepted() -> None:
    """A host list is now enforced by the egress proxy, so it is valid config."""
    settings = SandboxSettings(network=True, allow_hosts=["pypi.org"])
    assert settings.network is True
    assert settings.allow_hosts == ["pypi.org"]


def test_max_output_bytes_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        SandboxSettings(max_output_bytes=0)
