"""``.janus.yml`` parsing: defaults, every field, JSON, fallback parser and validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import janus.config.janus_yml as janus_yml
from janus.config.janus_yml import RepoConfig, RepoConfigError, load_repo_config, parse_repo_config

FULL_YAML = """
mode: pr
k_reruns: 7
tau: 0.75
epsilon: 0.1
max_sandbox_minutes: 12.5
max_llm_tokens: 4096
setup_command:
  - pip
  - install
  - -r
  - requirements.txt
test_command:
  - python
  - -m
  - pytest
unknown_future_key: keep me
"""


def test_missing_file_yields_defaults(tmp_path: Path) -> None:
    config = load_repo_config(tmp_path / "does_not_exist.yml")

    assert config == RepoConfig()
    assert config.mode == "suggest"
    assert config.k_reruns is None
    assert config.tau is None
    assert config.epsilon is None
    assert config.max_sandbox_minutes is None
    assert config.max_llm_tokens is None
    assert config.setup_command == ()
    assert config.test_command == ()
    assert dict(config.raw) == {}


def test_parses_every_field() -> None:
    config = parse_repo_config(FULL_YAML)

    assert config.mode == "pr"
    assert config.k_reruns == 7
    assert config.tau == 0.75
    assert config.epsilon == 0.1
    assert config.max_sandbox_minutes == 12.5
    assert config.max_llm_tokens == 4096
    assert config.setup_command == ("pip", "install", "-r", "requirements.txt")
    assert config.test_command == ("python", "-m", "pytest")


def test_unknown_keys_are_preserved_in_raw() -> None:
    config = parse_repo_config(FULL_YAML)

    assert config.raw["unknown_future_key"] == "keep me"
    assert config.raw["mode"] == "pr"


def test_json_is_accepted() -> None:
    text = json.dumps(
        {
            "mode": "pr",
            "k_reruns": 2,
            "tau": 0.9,
            "test_command": ["python", "-m", "pytest"],
        }
    )

    config = parse_repo_config(text)

    assert config.mode == "pr"
    assert config.k_reruns == 2
    assert config.tau == 0.9
    assert config.test_command == ("python", "-m", "pytest")


def test_inline_list_form_is_accepted() -> None:
    config = parse_repo_config("test_command: [python, -m, pytest]\n")

    assert config.test_command == ("python", "-m", "pytest")


def test_none_means_fall_back() -> None:
    config = parse_repo_config(
        "mode: null\nk_reruns: null\ntau: ~\nepsilon: ~\nmax_llm_tokens: null\n"
    )

    assert config.mode == "suggest"
    assert config.k_reruns is None
    assert config.tau is None
    assert config.epsilon is None
    assert config.max_llm_tokens is None


def test_empty_mapping_is_defaults() -> None:
    assert parse_repo_config("") == RepoConfig()
    assert parse_repo_config("# only a comment\n") == RepoConfig()


@pytest.mark.parametrize(
    "text",
    [
        "mode: [unterminated",
        "key without a colon",
        "- just\n- a list",
        "not a mapping at all",
    ],
)
def test_malformed_file_raises(text: str) -> None:
    with pytest.raises(RepoConfigError):
        parse_repo_config(text)


def test_malformed_file_error_is_catchable_by_the_caller(tmp_path: Path) -> None:
    path = tmp_path / ".janus.yml"
    path.write_text("mode: [unterminated\n")

    with pytest.raises(RepoConfigError):
        load_repo_config(path)


@pytest.mark.parametrize("mode", ["auto", "PR", "comment", 1])
def test_bad_mode_is_rejected(mode: object) -> None:
    with pytest.raises(RepoConfigError):
        parse_repo_config(json.dumps({"mode": mode}))


@pytest.mark.parametrize("tau", [-0.1, 1.01, 2])
def test_tau_out_of_range_is_rejected(tau: float) -> None:
    with pytest.raises(RepoConfigError):
        parse_repo_config(json.dumps({"tau": tau}))


@pytest.mark.parametrize("epsilon", [-0.5, 1.5])
def test_epsilon_out_of_range_is_rejected(epsilon: float) -> None:
    with pytest.raises(RepoConfigError):
        parse_repo_config(json.dumps({"epsilon": epsilon}))


@pytest.mark.parametrize("k_reruns", [0, -1])
def test_k_reruns_below_one_is_rejected(k_reruns: int) -> None:
    with pytest.raises(RepoConfigError):
        parse_repo_config(json.dumps({"k_reruns": k_reruns}))


@pytest.mark.parametrize(
    ("key", "value"),
    [("max_sandbox_minutes", 0), ("max_sandbox_minutes", -3.0), ("max_llm_tokens", 0)],
)
def test_non_positive_caps_are_rejected(key: str, value: float) -> None:
    with pytest.raises(RepoConfigError):
        parse_repo_config(json.dumps({key: value}))


@pytest.mark.parametrize(
    ("key", "value"),
    [("k_reruns", "three"), ("tau", "high"), ("max_llm_tokens", 1.5)],
)
def test_wrongly_typed_values_are_rejected(key: str, value: object) -> None:
    with pytest.raises(RepoConfigError):
        parse_repo_config(json.dumps({key: value}))


def test_command_entries_must_be_strings() -> None:
    with pytest.raises(RepoConfigError):
        parse_repo_config(json.dumps({"test_command": ["python", 3]}))


def test_boundary_values_are_accepted() -> None:
    config = parse_repo_config(json.dumps({"tau": 0.0, "epsilon": 1.0, "k_reruns": 1}))

    assert config.tau == 0.0
    assert config.epsilon == 1.0
    assert config.k_reruns == 1


# --- fallback parser (no PyYAML) ----------------------------------------------------------


@pytest.fixture
def without_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(janus_yml, "_yaml", None)


def test_fallback_parser_handles_the_simple_forms(without_yaml: None) -> None:
    config = parse_repo_config(FULL_YAML)

    assert config.mode == "pr"
    assert config.k_reruns == 7
    assert config.tau == 0.75
    assert config.epsilon == 0.1
    assert config.max_sandbox_minutes == 12.5
    assert config.setup_command == ("pip", "install", "-r", "requirements.txt")
    assert config.test_command == ("python", "-m", "pytest")
    assert config.raw["unknown_future_key"] == "keep me"


def test_fallback_parser_handles_comments_and_inline_lists(without_yaml: None) -> None:
    text = "# a comment\nmode: suggest  # trailing\ntest_command: [python, -m, pytest]\n"

    config = parse_repo_config(text)

    assert config.mode == "suggest"
    assert config.test_command == ("python", "-m", "pytest")


def test_fallback_parser_still_accepts_json(without_yaml: None) -> None:
    config = parse_repo_config(json.dumps({"k_reruns": 4}))

    assert config.k_reruns == 4


def test_fallback_parser_rejects_junk(without_yaml: None) -> None:
    with pytest.raises(RepoConfigError):
        parse_repo_config("this line has no colon\n")


def test_fallback_parser_still_validates(without_yaml: None) -> None:
    with pytest.raises(RepoConfigError):
        parse_repo_config("k_reruns: 0\n")


# --- the shipped sample repo config --------------------------------------------------------


def test_sample_repo_config_is_valid() -> None:
    path = Path(__file__).resolve().parents[1] / "fixtures" / "sample_repo" / ".janus.yml"

    config = load_repo_config(path)

    assert config.mode == "suggest"
    assert config.k_reruns == 3
    assert config.tau == 0.8
    assert config.epsilon == 0.05
    assert config.max_sandbox_minutes == 30
    assert config.max_llm_tokens == 200000
    assert config.test_command == ("python", "-m", "pytest", "-q")
