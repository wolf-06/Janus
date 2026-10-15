"""Commit a proven fix and push it to a branch without exposing the token.

Security note (``PLAN.md`` §4): installation tokens are **never** placed in a remote URL or
in any argv. The token is supplied to git as an ``http.extraHeader`` value through
``GIT_CONFIG_*`` environment variables, exactly like :class:`janus.source.GitSourceProvider`,
so it cannot appear in a process listing or in an error message.

This helper only pushes a normal branch: it never force-pushes and never merges.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from janus.source import GitRunner, credential_header

__all__ = ["PushError", "commit_and_push"]


class PushError(RuntimeError):
    """Raised when a fix cannot be committed or pushed."""


def _validate_branch(branch: str) -> None:
    """Reject refs git would reject, or that could be mistaken for an option."""
    if (
        not branch
        or branch.startswith("-")
        or ".." in branch
        or any(character.isspace() for character in branch)
    ):
        raise PushError(f"unsafe branch name: {branch!r}")


def _subprocess_runner(timeout_s: float) -> GitRunner:
    """Build the default runner: run ``argv`` with a bounded wall-clock timeout."""

    def run(argv: Sequence[str], env: Mapping[str, str], cwd: Path | None) -> int:
        completed = subprocess.run(
            list(argv),
            env=dict(env),
            cwd=cwd,
            timeout=timeout_s,
            check=False,
            capture_output=True,
        )
        return completed.returncode

    return run


def _env(token: str, home: Path) -> dict[str, str]:
    """Minimal deterministic git environment carrying the token out-of-band."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": credential_header(token),
    }


def commit_and_push(
    *,
    checkout: Path,
    branch: str,
    message: str,
    token: str,
    remote: str = "origin",
    git: GitRunner | None = None,
    timeout_s: float = 120.0,
) -> None:
    """Create ``branch`` from the checkout's worktree, commit everything, and push it.

    Raises :class:`PushError` on an unsafe branch, any non-zero git exit, or a commit with
    nothing staged. The token only ever travels in the child environment.
    """
    _validate_branch(branch)
    runner = git if git is not None else _subprocess_runner(timeout_s)

    home = Path(tempfile.mkdtemp(prefix="janus-git-home-"))
    try:
        env = _env(token, home)
        commands = (
            ["git", "checkout", "-B", branch],
            ["git", "add", "-A"],
            ["git", "commit", "-m", message],
            ["git", "push", remote, branch],
        )
        for argv in commands:
            try:
                code = runner(argv, env, checkout)
            except FileNotFoundError as exc:
                raise PushError(f"git executable not found: {argv[0]!r}") from exc
            except subprocess.TimeoutExpired as exc:
                raise PushError(f"git command timed out: {list(argv)!r}") from exc

            if code != 0:
                joined = " ".join(argv)
                if argv[1] == "commit":
                    raise PushError(
                        f"nothing to commit (git command failed with exit code {code}: {joined})"
                    )
                raise PushError(f"git command failed with exit code {code}: {joined}")
    finally:
        shutil.rmtree(home, ignore_errors=True)
