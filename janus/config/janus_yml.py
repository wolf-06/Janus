"""Per-repository ``.janus.yml`` configuration.

``.janus.yml`` is committed alongside a repository's code and tells Janus how to behave
for *that* repository: whether to only comment (``suggest``) or also open a pull request
(``pr``), the reproduction/decision thresholds (``k_reruns``, ``tau``, ``epsilon``) and the
per-failure cost caps. Every key is optional; a key that is absent — or set to ``null`` —
means **fall back to the process-wide setting** (the ``JANUS_*`` environment / config file
handled by ``janus.app.config``).

Because the file is committed to the repository it **must not carry secrets**. Tokens,
credentials and webhook secrets belong in the process environment, never here; Janus reads
this file as untrusted repository content and redacts before anything reaches a model.

Parsing is dependency-light on purpose. If PyYAML happens to be installed it is used; when
it is not, JSON (a valid subset of YAML) is accepted, and for the simple ``key: value`` /
block-list / inline-list forms this file uses a small documented fallback parser stands in.
A missing file is defaults; a malformed file raises :class:`RepoConfigError` so the caller
can log and skip the repository rather than silently misconfigure it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

try:  # pragma: no cover - which branch runs depends on the environment
    import yaml as _yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - only when PyYAML is not installed
    _yaml = None


class RepoConfigError(Exception):
    """Raised when a ``.janus.yml`` is malformed or fails validation.

    The message is safe to log: it names the offending key and value but never echoes file
    contents wholesale (a misconfigured file must not leak into logs or a UI).
    """


@dataclass(frozen=True)
class RepoConfig:
    """Validated per-repository configuration.

    ``None`` for any optional field means "no per-repo override": the caller falls back to
    the process-wide setting. ``raw`` holds the whole parsed mapping so unknown keys survive
    for forward compatibility (a newer ``.janus.yml`` read by an older Janus is preserved,
    not rejected).
    """

    mode: Literal["suggest", "pr"] = "suggest"
    k_reruns: int | None = None
    tau: float | None = None
    epsilon: float | None = None
    max_sandbox_minutes: float | None = None
    max_llm_tokens: int | None = None
    setup_command: tuple[str, ...] = ()
    test_command: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)


_MODES: tuple[Literal["suggest", "pr"], Literal["suggest", "pr"]] = ("suggest", "pr")


def parse_repo_config(text: str) -> RepoConfig:
    """Parse and validate the contents of a ``.janus.yml``.

    Raises :class:`RepoConfigError` on malformed input or a failed validation check.
    """
    return _build_config(_parse_mapping(text))


def load_repo_config(path: Path) -> RepoConfig:
    """Load a ``.janus.yml`` from ``path``.

    A missing file yields :class:`RepoConfig` defaults (the repository simply opts into
    nothing). Any other read failure or a malformed file raises :class:`RepoConfigError`.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return RepoConfig()
    except OSError as exc:
        raise RepoConfigError(f"cannot read {path}: {exc}") from exc
    return parse_repo_config(text)


# --- parsing ------------------------------------------------------------------------------


def _parse_mapping(text: str) -> Mapping[str, Any]:
    """Turn the file's text into a top-level mapping, or raise :class:`RepoConfigError`."""
    if _yaml is not None:
        try:
            loaded = _yaml.safe_load(text)
        except Exception as exc:  # noqa: BLE001 - any YAML failure is a malformed file
            raise RepoConfigError(f"malformed .janus.yml: {exc}") from exc
        return _require_mapping(loaded)

    # No PyYAML available. JSON is a valid subset of YAML, so try it first; only if that
    # fails do we fall back to the small parser for the simple YAML forms this file uses.
    try:
        loaded = json.loads(text)
    except ValueError:
        return _parse_simple(text)
    return _require_mapping(loaded)


def _require_mapping(loaded: Any) -> Mapping[str, Any]:
    if loaded is None:
        return {}
    if not isinstance(loaded, Mapping):
        raise RepoConfigError("malformed .janus.yml: top level must be a mapping")
    return loaded


def _parse_simple(text: str) -> dict[str, Any]:
    """A deliberately small YAML subset parser used only when PyYAML is unavailable.

    Supports the documented ``.janus.yml`` schema and nothing more:

    * top-level ``key: value`` scalars (ints, floats, booleans, ``null``/``~``, and single-
      or double-quoted strings);
    * block lists — ``key:`` followed by indented ``- item`` lines;
    * inline lists — ``key: [a, b, c]``.

    Comments introduced by ``#`` (at line start or after whitespace, outside quotes) and
    blank lines are ignored. A ``key:`` with no value and no list items parses to ``None``,
    matching YAML. Anything it cannot understand raises :class:`RepoConfigError` rather than
    guessing: a silent misparse is worse than a loud refusal.
    """
    result: dict[str, Any] = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        raw = lines[index]
        index += 1
        stripped = _strip_comment(raw).strip()
        if not stripped:
            continue
        if ":" not in stripped:
            raise RepoConfigError(f"malformed .janus.yml: cannot parse line {raw!r}")
        key, _, remainder = stripped.partition(":")
        key = key.strip()
        if not key:
            raise RepoConfigError(f"malformed .janus.yml: missing key in line {raw!r}")

        value_text = remainder.strip()
        if value_text:
            result[key] = _parse_inline_value(value_text)
            continue

        items: list[Any] = []
        while index < len(lines):
            candidate = _strip_comment(lines[index])
            if not candidate.strip():
                index += 1
                continue
            if candidate[0] in " \t":
                item_text = candidate.strip()
                if not item_text.startswith("-"):
                    raise RepoConfigError(
                        f"malformed .janus.yml: expected a list item under {key!r}, "
                        f"got {candidate!r}"
                    )
                items.append(_coerce_scalar(item_text[1:].strip()))
                index += 1
                continue
            break
        result[key] = items or None
    return result


def _parse_inline_value(text: str) -> Any:
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_coerce_scalar(part) for part in _split_inline(inner)]
    return _coerce_scalar(text)


def _split_inline(text: str) -> list[str]:
    """Split an inline list body on top-level commas, respecting quotes."""
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in text:
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            current.append(char)
            continue
        if char == ",":
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return [part for part in (p.strip() for p in parts) if part]


def _strip_comment(line: str) -> str:
    """Drop a trailing ``#`` comment, ignoring ``#`` inside quoted values."""
    quote: str | None = None
    for index, char in enumerate(line):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index]
    return line


def _coerce_scalar(token: str) -> Any:
    text = token.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    lowered = text.lower()
    if lowered in {"null", "~", ""}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


# --- validation ---------------------------------------------------------------------------


def _build_config(data: Mapping[str, Any]) -> RepoConfig:
    return RepoConfig(
        mode=_parse_mode(data.get("mode")),
        k_reruns=_optional_int(data, "k_reruns", minimum=1),
        tau=_optional_float(data, "tau", minimum=0.0, maximum=1.0),
        epsilon=_optional_float(data, "epsilon", minimum=0.0, maximum=1.0),
        max_sandbox_minutes=_optional_float(data, "max_sandbox_minutes", exclusive_minimum=0.0),
        max_llm_tokens=_optional_int(data, "max_llm_tokens", minimum=1),
        setup_command=_command(data, "setup_command"),
        test_command=_command(data, "test_command"),
        raw=MappingProxyType(dict(data)),
    )


def _parse_mode(value: Any) -> Literal["suggest", "pr"]:
    if value is None:
        return "suggest"
    if value in _MODES:
        return value  # type: ignore[no-any-return]
    raise RepoConfigError(f"mode must be one of {_MODES}, got {value!r}")


def _optional_int(data: Mapping[str, Any], key: str, *, minimum: int | None = None) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    number = _to_int(value, key)
    if minimum is not None and number < minimum:
        raise RepoConfigError(f"{key} must be >= {minimum}, got {number}")
    return number


def _optional_float(
    data: Mapping[str, Any],
    key: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    exclusive_minimum: float | None = None,
) -> float | None:
    value = data.get(key)
    if value is None:
        return None
    number = _to_float(value, key)
    if minimum is not None and number < minimum:
        raise RepoConfigError(f"{key} must be >= {minimum}, got {number}")
    if maximum is not None and number > maximum:
        raise RepoConfigError(f"{key} must be <= {maximum}, got {number}")
    if exclusive_minimum is not None and number <= exclusive_minimum:
        raise RepoConfigError(f"{key} must be > {exclusive_minimum}, got {number}")
    return number


def _to_int(value: Any, key: str) -> int:
    if isinstance(value, bool):
        raise RepoConfigError(f"{key} must be an integer, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise RepoConfigError(f"{key} must be an integer, got {value!r}")


def _to_float(value: Any, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RepoConfigError(f"{key} must be a number, got {value!r}")
    return float(value)


def _command(data: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        parts: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise RepoConfigError(f"{key} entries must be strings, got {item!r}")
            parts.append(item)
        return tuple(parts)
    raise RepoConfigError(f"{key} must be a list of strings, got {value!r}")
