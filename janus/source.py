"""Acquire a repository's source on disk before the sandbox runs anything.

The worker needs a checkout of the exact revision a failing run points at. Two providers
implement that: :class:`LocalSourceProvider` wraps an existing directory (tests and local
demos), and :class:`GitSourceProvider` clones over HTTPS from a GitHub App installation.

Security note (``PLAN.md`` §4): installation tokens are **never** placed in a clone URL or
in any argv. :class:`GitSourceProvider` authenticates with an ``http.extraHeader`` git config
supplied through the environment, so the token never appears in a process listing, in the
remote URL, or in an error message.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

__all__ = [
    "GitRunner",
    "GitSourceProvider",
    "LocalSourceProvider",
    "SourceError",
    "SourceProvider",
    "SourceSpec",
    "TokenProvider",
    "credential_header",
]


class SourceError(RuntimeError):
    """Raised when source cannot be acquired (bad ref, bad token, missing git)."""


@dataclass(frozen=True)
class SourceSpec:
    """What to check out: a repository, a revision, and the installation that may read it."""

    owner: str
    name: str
    ref: str
    installation_id: int
    base_url: str = "https://github.com"


# ``installation_id -> installation token``. Kept as a callable so the GitHub App auth
# module can be wired in without this module importing it.
TokenProvider = Callable[[int], str]

# ``argv, env, cwd -> returncode``. Injectable so tests need neither network nor real git.
GitRunner = Callable[[Sequence[str], Mapping[str, str], "Path | None"], int]


def credential_header(token: str) -> str:
    """Return the ``Authorization: Basic ...`` value for a GitHub installation token.

    GitHub accepts an installation token as the password with the fixed username
    ``x-access-token``. The value is used as an ``http.extraHeader`` git config so the token
    never touches the clone URL.
    """
    raw = f"x-access-token:{token}".encode()
    encoded = base64.b64encode(raw).decode("ascii")
    return f"Authorization: Basic {encoded}"


class SourceProvider(Protocol):
    """A context manager yielding a checkout of ``spec.ref`` for the duration of the block.

    The yielded directory is removed when the context exits; callers must finish all work
    inside the block.
    """

    def acquire(self, spec: SourceSpec) -> AbstractContextManager[Path]:
        """Return a context manager yielding a path holding a checkout of ``spec.ref``.

        Implementations are ``@contextmanager`` factories: entering the block yields the
        checkout, and exiting it removes the checkout.
        """
        ...


class LocalSourceProvider:
    """Wrap an existing local directory; yields it and removes nothing.

    Used by tests and local runs where the source is already on disk.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    @contextmanager
    def acquire(self, spec: SourceSpec) -> Iterator[Path]:
        del spec  # A local directory already exists at the requested revision.
        yield self._root


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


class GitSourceProvider:
    """Clone ``spec.ref`` over HTTPS using an installation token.

    The token is passed to git as an ``http.extraHeader`` value through ``GIT_CONFIG_*``
    environment variables, so it is absent from the clone URL and from every argv. A bad
    token fails fast instead of prompting (``GIT_TERMINAL_PROMPT=0``), and the environment is
    minimal and deterministic (``GIT_CONFIG_NOSYSTEM=1``, a temp ``HOME``) so the host's git
    config cannot leak in or change behaviour.
    """

    def __init__(
        self,
        *,
        token_provider: TokenProvider,
        workdir: Path | None = None,
        git: GitRunner | None = None,
        timeout_s: float = 300.0,
        base_url: str = "https://github.com",
    ) -> None:
        self._token_provider = token_provider
        self._workdir = workdir
        self._timeout_s = timeout_s
        self._git = git if git is not None else _subprocess_runner(timeout_s)
        self._base_url = base_url

    def _env(self, token: str, home: Path) -> dict[str, str]:
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

    def _run(self, argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> None:
        """Run one git command; a non-zero code (or missing git) becomes a ``SourceError``.

        The message names the failing argv and the return code, and never includes the
        token, which is only ever in ``env``.
        """
        try:
            code = self._git(argv, env, cwd)
        except FileNotFoundError as exc:
            raise SourceError(f"git executable not found: {argv[0]!r}") from exc
        except subprocess.TimeoutExpired as exc:
            raise SourceError(f"git command timed out: {list(argv)!r}") from exc
        if code != 0:
            raise SourceError(f"git command failed with exit code {code}: {' '.join(argv)}")

    @contextmanager
    def acquire(self, spec: SourceSpec) -> Iterator[Path]:
        token = self._token_provider(spec.installation_id)
        url = f"{self._base_url}/{spec.owner}/{spec.name}.git"

        if self._workdir is not None:
            self._workdir.mkdir(parents=True, exist_ok=True)
        checkout = Path(tempfile.mkdtemp(prefix="janus-src-", dir=self._workdir))
        home = Path(tempfile.mkdtemp(prefix="janus-git-home-", dir=self._workdir))
        try:
            env = self._env(token, home)
            commands = (
                ["git", "init", "--quiet"],
                ["git", "remote", "add", "origin", url],
                ["git", "fetch", "--depth", "1", "origin", spec.ref],
                ["git", "checkout", "--detach", "FETCH_HEAD"],
            )
            for argv in commands:
                self._run(argv, env, checkout)
            yield checkout
        finally:
            shutil.rmtree(checkout, ignore_errors=True)
            shutil.rmtree(home, ignore_errors=True)
