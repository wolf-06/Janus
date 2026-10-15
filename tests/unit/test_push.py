"""Commit and push: argv order, token isolation, safety refusals, and no force-push.

No repository and no real git: the helper is driven by a fake ``GitRunner`` that records
every ``(argv, env, cwd)`` it is handed.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from janus.github.push import PushError, commit_and_push
from janus.source import GitRunner

TOKEN = "ghs_supersecret_installation_token"
MESSAGE = "Fix the parser"


def _runner(
    *,
    fail_on: str | None = None,
    returncode: int = 1,
) -> tuple[GitRunner, list[tuple[tuple[str, ...], dict[str, str], Path | None]]]:
    """A fake runner that records calls and fails only when ``argv[1] == fail_on``."""
    calls: list[tuple[tuple[str, ...], dict[str, str], Path | None]] = []

    def run(argv: Sequence[str], env: Mapping[str, str], cwd: Path | None) -> int:
        calls.append((tuple(argv), dict(env), cwd))
        if fail_on is not None and len(argv) > 1 and argv[1] == fail_on:
            return returncode
        return 0

    return run, calls


def _push(git: GitRunner, checkout: Path, *, branch: str = "janus/fix") -> None:
    commit_and_push(
        checkout=checkout,
        branch=branch,
        message=MESSAGE,
        token=TOKEN,
        git=git,
    )


def test_push_runs_checkout_add_commit_push_in_order(tmp_path: Path) -> None:
    runner, calls = _runner()
    _push(runner, tmp_path)

    argvs = [list(argv) for argv, _env, _cwd in calls]
    assert argvs == [
        ["git", "checkout", "-B", "janus/fix"],
        ["git", "add", "-A"],
        ["git", "commit", "-m", MESSAGE],
        ["git", "push", "origin", "janus/fix"],
    ]
    assert all(cwd == tmp_path for _argv, _env, cwd in calls)


def test_push_puts_token_only_in_env_header(tmp_path: Path) -> None:
    runner, calls = _runner()
    _push(runner, tmp_path)

    encoded = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode("ascii")
    assert calls
    for argv, env, _cwd in calls:
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
        assert encoded in env["GIT_CONFIG_VALUE_0"]
        assert TOKEN not in env["GIT_CONFIG_VALUE_0"]
        assert all(TOKEN not in element for element in argv)

    push_argv = next(argv for argv, _env, _cwd in calls if argv[1] == "push")
    assert push_argv[-2] == "origin"
    assert TOKEN not in " ".join(push_argv)


def test_push_disables_terminal_prompts(tmp_path: Path) -> None:
    runner, calls = _runner()
    _push(runner, tmp_path)

    assert calls
    for _argv, env, _cwd in calls:
        assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_push_failure_names_argv_and_code_but_not_token(tmp_path: Path) -> None:
    runner, calls = _runner(fail_on="push", returncode=128)
    with pytest.raises(PushError) as excinfo:
        _push(runner, tmp_path)

    message = str(excinfo.value)
    assert "128" in message
    for element in calls[-1][0]:
        assert element in message
    assert TOKEN not in message


@pytest.mark.parametrize("branch", ["", "-force-push", "bad branch", "main..evil"])
def test_push_rejects_unsafe_branch_names(tmp_path: Path, branch: str) -> None:
    runner, calls = _runner()
    with pytest.raises(PushError, match="unsafe branch"):
        _push(runner, tmp_path, branch=branch)
    assert calls == []


def test_push_raises_when_nothing_to_commit(tmp_path: Path) -> None:
    runner, calls = _runner(fail_on="commit", returncode=1)
    with pytest.raises(PushError, match="nothing to commit"):
        _push(runner, tmp_path)

    assert [argv[1] for argv, _env, _cwd in calls] == ["checkout", "add", "commit"]
    assert all(argv[1] != "push" for argv, _env, _cwd in calls)


def test_push_never_force_pushes(tmp_path: Path) -> None:
    runner, calls = _runner()
    _push(runner, tmp_path)

    for argv, _env, _cwd in calls:
        assert "--force" not in " ".join(argv)
        assert "-f" not in " ".join(argv)
        assert "--force-with-lease" not in argv
