"""Source acquisition: credential encoding, local passthrough, and the git provider.

No network and no real git: the git provider is driven by a fake ``GitRunner`` that records
every ``(argv, env, cwd)`` it is handed.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from janus.source import (
    GitRunner,
    GitSourceProvider,
    LocalSourceProvider,
    SourceError,
    SourceSpec,
    credential_header,
)

TOKEN = "ghs_supersecret_installation_token"


def _spec() -> SourceSpec:
    return SourceSpec(owner="octo", name="hello", ref="abc123", installation_id=7)


def _runner(
    returncode: int = 0,
) -> tuple[GitRunner, list[tuple[tuple[str, ...], dict[str, str], Path | None]]]:
    """A fake runner that records every call and always returns ``returncode``."""
    calls: list[tuple[tuple[str, ...], dict[str, str], Path | None]] = []

    def run(argv: Sequence[str], env: Mapping[str, str], cwd: Path | None) -> int:
        calls.append((tuple(argv), dict(env), cwd))
        return returncode

    return run, calls


def _provider(git: GitRunner, workdir: Path) -> GitSourceProvider:
    return GitSourceProvider(
        token_provider=lambda _installation_id: TOKEN, workdir=workdir, git=git
    )


def test_credential_header_encodes_x_access_token() -> None:
    header = credential_header(TOKEN)

    prefix = "Authorization: Basic "
    assert header.startswith(prefix)
    decoded = base64.b64decode(header[len(prefix) :]).decode("ascii")
    assert decoded == f"x-access-token:{TOKEN}"


def test_local_provider_yields_root_and_deletes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "file.txt").write_text("hi", encoding="utf-8")

    with LocalSourceProvider(root).acquire(_spec()) as path:
        assert path == root
        assert (path / "file.txt").read_text(encoding="utf-8") == "hi"

    assert root.is_dir()
    assert (root / "file.txt").exists()


def test_git_provider_url_has_owner_name_and_no_token(tmp_path: Path) -> None:
    runner, calls = _runner()
    with _provider(runner, tmp_path).acquire(_spec()):
        pass

    argvs = [argv for argv, _env, _cwd in calls]
    remote_add = next(argv for argv in argvs if list(argv[:2]) == ["git", "remote"])
    url = remote_add[-1]
    assert url == "https://github.com/octo/hello.git"
    assert TOKEN not in url


def test_git_provider_runs_the_expected_sequence(tmp_path: Path) -> None:
    runner, calls = _runner()
    with _provider(runner, tmp_path).acquire(_spec()):
        pass

    argvs = [list(argv) for argv, _env, _cwd in calls]
    assert argvs == [
        ["git", "init", "--quiet"],
        ["git", "remote", "add", "origin", "https://github.com/octo/hello.git"],
        ["git", "fetch", "--depth", "1", "origin", "abc123"],
        ["git", "checkout", "--detach", "FETCH_HEAD"],
    ]


def test_git_provider_token_never_appears_in_argv(tmp_path: Path) -> None:
    runner, calls = _runner()
    with _provider(runner, tmp_path).acquire(_spec()):
        pass

    for argv, _env, _cwd in calls:
        assert all(TOKEN not in element for element in argv)


def test_git_provider_token_is_in_env_header_base64_encoded(tmp_path: Path) -> None:
    runner, calls = _runner()
    with _provider(runner, tmp_path).acquire(_spec()):
        pass

    assert calls
    encoded = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode("ascii")
    for _argv, env, _cwd in calls:
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
        assert encoded in env["GIT_CONFIG_VALUE_0"]
        assert TOKEN not in env["GIT_CONFIG_VALUE_0"]


def test_git_provider_uses_minimal_deterministic_env(tmp_path: Path) -> None:
    runner, calls = _runner()
    with _provider(runner, tmp_path).acquire(_spec()):
        _argv, env, _cwd = calls[0]
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert Path(env["HOME"]).is_dir()
        assert Path(env["HOME"]) != Path.home()


def test_git_provider_nonzero_returncode_raises_without_token(tmp_path: Path) -> None:
    runner, calls = _runner(returncode=128)
    with pytest.raises(SourceError) as excinfo:
        with _provider(runner, tmp_path).acquire(_spec()):
            pass

    message = str(excinfo.value)
    assert "128" in message
    for element in calls[0][0]:
        assert element in message
    assert TOKEN not in message


def test_git_provider_removes_checkout_after_success(tmp_path: Path) -> None:
    runner, _calls = _runner()
    with _provider(runner, tmp_path).acquire(_spec()) as checkout:
        assert checkout.is_dir()

    assert not checkout.exists()


def test_git_provider_removes_checkout_after_failure(tmp_path: Path) -> None:
    runner, calls = _runner(returncode=1)
    with pytest.raises(SourceError):
        with _provider(runner, tmp_path).acquire(_spec()):
            pass

    checkout = calls[0][2]
    assert checkout is not None
    assert not checkout.exists()


def test_git_provider_missing_git_binary_raises_clear_error(tmp_path: Path) -> None:
    def run(argv: Sequence[str], env: Mapping[str, str], cwd: Path | None) -> int:
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    with pytest.raises(SourceError, match="git"):
        with _provider(run, tmp_path).acquire(_spec()):
            pass
