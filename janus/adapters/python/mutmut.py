"""Mutation-testing translation for the Python adapter.

``mutation_command`` builds the ``mutmut run --paths-to-mutate`` argv; ``parse_mutation``
reads the killed/survived counts back out of mutmut's stdout.

**Artifact vs stdout.** mutmut stores its state in ``{workdir}/.mutmut-cache``, but the
workdir is a sandbox setting the adapter does not hold (and must not, since it holds no
policy or configuration), so an artifact path would be a guess — and mutmut offers no
portable flag to relocate the cache to the adapter's own ``/tmp/janus``. The killed and
survived counts are instead printed to stdout, which the runner always captures, so this
adapter parses **stdout** and leaves ``artifacts=()``. S2 validates this against the real
tool and can move to an artifact if a stable cache path or report flag is confirmed.

This module is a pure translator: it holds no policy and never runs mutmut.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from janus.core.adapter import Command, MutationResult
from janus.sandbox.spec import SandboxResult

# mutmut stores its state in ``{workdir}/.mutmut-cache``. The workdir is a sandbox setting
# the adapter does not hold, so this records the default-workdir location for the S2
# follow-up only; it is **not** declared as an artifact because the counts come from stdout.
MUTMUT_CACHE_PATH = "/work/.mutmut-cache"

_LABEL_RE_TEMPLATE = r"(?i)\b{label}\b\s*[:=]?\s*(\d+)"
_NUMBER_RE_TEMPLATE = r"(?i)(\d+)\s*{label}\b"


def mutation_command(files: Sequence[str]) -> Command:
    """The mutmut argv that runs mutation testing over ``files``.

    No artifact is declared: the result is read from stdout (see the module docstring).
    """
    argv = ("python", "-m", "mutmut", "run", "--paths-to-mutate", *files)
    return Command(argv=argv)


def parse_mutation(result: SandboxResult) -> MutationResult:
    """Read the mutation result from one sandbox run. Total: never raises.

    Parses the killed/survived/total summary from stdout. When nothing is parseable the
    result is zeroed; counts are clamped so ``killed + survived <= total`` and never
    negative. If only killed/survived are present, ``total`` is their sum.
    """
    zero = MutationResult(total=0, killed=0, survived=0)
    text = result.stdout or ""

    killed = _count(text, "killed")
    survived = _count(text, "survived")
    total = _count(text, "total")
    if killed is None and survived is None and total is None:
        return zero

    killed_count = max(killed or 0, 0)
    survived_count = max(survived or 0, 0)
    total_count = max(total if total is not None else killed_count + survived_count, 0)
    killed_count = min(killed_count, total_count)
    survived_count = min(survived_count, total_count - killed_count)
    return MutationResult(total=total_count, killed=killed_count, survived=survived_count)


def _count(text: str, label: str) -> int | None:
    """The last integer associated with ``label`` in ``text``, or ``None``.

    Prefers ``label: 8`` / ``label 8``; falls back to ``8 label``. The *last* match wins so
    a trailing summary beats any per-mutation chatter earlier in the log.
    """
    labelled = re.findall(_LABEL_RE_TEMPLATE.format(label=label), text)
    if labelled:
        return int(labelled[-1])
    numbered = re.findall(_NUMBER_RE_TEMPLATE.format(label=label), text)
    if numbered:
        return int(numbered[-1])
    return None
