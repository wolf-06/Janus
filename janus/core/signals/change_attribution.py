"""The **change_attribution** signal: does the last-green → first-red diff touch the tests?

A failure that appears exactly when the changed files include the module a failing test lives
in is the most direct evidence for :attr:`~janus.core.triage.routes.Route.CODE_BUG`. When the
range is unavailable (no last-green baseline) the collector abstains. When the range is
available but touches nothing related to the failing targets, the change is unlikely to be the
cause and the collector weakly leans :attr:`~janus.core.triage.routes.Route.ENV`.

A target id like ``tests/test_x.py::test_y`` names a file before the ``::``; that file (and
any changed file sharing its basename) is what the diff is compared against.
"""

from __future__ import annotations

import posixpath
from typing import Any

from janus.core.signals.base import SignalContext, SignalEvidence, clamp_votes
from janus.core.triage.routes import Route


def _module_path(target_id: str) -> str:
    """The file a target id lives in: the part before ``::`` (or the whole id)."""
    return target_id.split("::", 1)[0].strip()


def _basename(path: str) -> str:
    return posixpath.basename(path.strip().rstrip("/"))


class ChangeAttributionCollector:
    """Vote ``CODE_BUG`` when the diff touches the failing tests, else weakly ``ENV``."""

    name = "change_attribution"

    def collect(self, ctx: SignalContext) -> SignalEvidence | None:
        change = ctx.change
        if not change.available:
            return None

        target_files = sorted({_module_path(target) for target in ctx.failing_targets if target})
        target_basenames = {_basename(path) for path in target_files}
        changed = list(change.files_changed)

        related = sorted(
            path for path in changed if path in target_files or _basename(path) in target_basenames
        )

        if related:
            votes = clamp_votes({Route.CODE_BUG: 0.85})
            summary = (
                f"{len(related)} changed file(s) touch the failing tests ({', '.join(related)})"
            )
        else:
            votes = clamp_votes({Route.ENV: 0.4})
            summary = (
                f"{len(changed)} changed file(s) do not touch the "
                f"{len(target_files)} failing-test file(s)"
            )

        details: dict[str, Any] = {
            "base_sha": change.base_sha,
            "head_sha": change.head_sha,
            "files_changed": changed,
            "target_files": target_files,
            "related_files": related,
        }
        return SignalEvidence(
            name=self.name,
            summary=summary,
            details=details,
            votes=votes,
        )
