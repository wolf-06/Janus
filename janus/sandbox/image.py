"""Resolve and build the sandbox image for a single run.

There is no published Janus base image (ADR D-056). Instead every run that needs one builds
its own image, uses it once, and (by default) removes it afterwards. The image provides the
*environment* — tooling and, optionally, the project's dependencies — but never the source:
the source is still copied in at run time with ``docker cp`` (ADR D-047).

Three sources are supported. A ``repo`` Dockerfile is used as-is with the repository root as
the build context. A ``generated`` Dockerfile is written by Janus and built from a tiny
context holding only the manifest files it ``COPY``s, so no source is baked into a layer.
``auto`` picks the repo Dockerfile when one exists and generates otherwise. ``prebuilt``
skips building entirely and pulls ``settings.image``.

The build context for a generated image lives in a fresh temp directory. The resolver does
not own its lifetime: **the caller owns cleanup** and the runner removes it after the build.
"""

from __future__ import annotations

import contextlib
import hashlib
import shutil
import tempfile
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import docker
from docker.errors import BuildError, DockerException

from janus.sandbox.settings import ImageSource, SandboxSettings

# A repo Dockerfile is only honoured at the repository root; a nested one is the project's
# business, not the sandbox's.
_DOCKERFILE_NAMES = ("Dockerfile", "dockerfile")

# Lock files copied alongside ``pyproject.toml`` when present, in a stable order.
_LOCK_FILES = ("uv.lock", "poetry.lock", "requirements.txt")

_GENERATED_DOCKERFILE = "Dockerfile"
_CONTEXT_PREFIX = "janus-sandbox-build-"


class ImageResolutionError(RuntimeError):
    """Raised when no image can be resolved for a run."""


class ImageBuildError(RuntimeError):
    """Raised when building a per-run image fails or exceeds the time cap."""


@dataclass(frozen=True)
class ResolvedImage:
    """How a run should obtain its image.

    ``build`` is true when the caller must build ``tag`` first; ``dockerfile`` is the
    Dockerfile text and ``context`` the directory the build runs against. ``context`` is
    ``None`` for a prebuilt image and, for a generated one, a temp directory the caller owns.
    """

    tag: str
    build: bool
    dockerfile: str | None = None
    context: Path | None = None


def find_repo_dockerfile(source_dir: Path) -> Path | None:
    """Return the repository's root Dockerfile, or ``None`` when there is not one."""
    for name in _DOCKERFILE_NAMES:
        candidate = source_dir / name
        if candidate.is_file():
            return candidate
    return None


def build_context_files(source_dir: Path) -> list[Path]:
    """The manifest files that go into a generated image's build context.

    Only the dependency manifests are included so the context stays tiny and no source is
    baked into the image. The set mirrors the ``COPY`` lines in :func:`generate_dockerfile`.
    """
    requirements = source_dir / "requirements.txt"
    if requirements.is_file():
        return [requirements]
    pyproject = source_dir / "pyproject.toml"
    if pyproject.is_file():
        files = [pyproject]
        files.extend(source_dir / name for name in _LOCK_FILES if (source_dir / name).is_file())
        return files
    return []


def generate_dockerfile(settings: SandboxSettings, source_dir: Path | None) -> str:
    """Generate a Dockerfile that provides the run environment.

    Deterministic for identical inputs: fixed line order, no timestamps. Dependency install
    only appears when the matching manifest exists in ``source_dir``; ``source_dir`` is
    ``None`` when the caller has no repository (e.g. a synthetic probe).
    """
    base = settings.base_image or settings.image
    lines = [
        f"FROM {base}",
        "ENV PYTHONDONTWRITEBYTECODE=1",
        "ENV PYTHONUNBUFFERED=1",
        "ENV PIP_DISABLE_PIP_VERSION_CHECK=1",
        "ENV DEBIAN_FRONTEND=noninteractive",
    ]
    if settings.tooling_packages:
        packages = " ".join(settings.tooling_packages)
        lines.append(f"RUN pip install --no-cache-dir {packages}")
    if source_dir is not None:
        if (source_dir / "requirements.txt").is_file():
            lines.append("COPY requirements.txt ./")
            lines.append("RUN pip install --no-cache-dir -r requirements.txt")
        elif (source_dir / "pyproject.toml").is_file():
            lines.append("COPY pyproject.toml ./")
            for name in _LOCK_FILES:
                if (source_dir / name).is_file():
                    lines.append(f"COPY {name} ./")
            lines.append("RUN pip install --no-cache-dir .")
    lines.append(f"WORKDIR {settings.workdir}")
    return "\n".join(lines) + "\n"


def image_tag(
    settings: SandboxSettings,
    *,
    dockerfile: str,
    context_files: Sequence[tuple[str, bytes]] = (),
) -> str:
    """Content-addressed tag so identical inputs reuse a tag and any change makes a new one.

    The payload covers the Dockerfile text, the base image, and the ``(name, content)`` of
    each context file. Nothing time- or path-dependent is included, so the tag is stable
    across machines.
    """
    digest = hashlib.sha256()
    digest.update(dockerfile.encode("utf-8"))
    digest.update(b"\0")
    digest.update((settings.base_image or settings.image).encode("utf-8"))
    digest.update(b"\0")
    for name, content in context_files:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return f"{settings.image_tag_prefix}:{digest.hexdigest()[:12]}"


def _repo_image(settings: SandboxSettings, source_dir: Path, dockerfile: Path) -> ResolvedImage:
    text = dockerfile.read_text(encoding="utf-8")
    return ResolvedImage(
        tag=image_tag(settings, dockerfile=text),
        build=True,
        dockerfile=text,
        context=source_dir,
    )


def _generated_image(settings: SandboxSettings, source_dir: Path | None) -> ResolvedImage:
    text = generate_dockerfile(settings, source_dir)
    files = build_context_files(source_dir) if source_dir is not None else []
    tag = image_tag(
        settings,
        dockerfile=text,
        context_files=[(path.name, path.read_bytes()) for path in files],
    )
    context = Path(tempfile.mkdtemp(prefix=_CONTEXT_PREFIX))
    (context / _GENERATED_DOCKERFILE).write_text(text, encoding="utf-8")
    for path in files:
        shutil.copy2(path, context / path.name)
    return ResolvedImage(tag=tag, build=True, dockerfile=text, context=context)


def resolve_image(settings: SandboxSettings, source_dir: Path | None) -> ResolvedImage:
    """Decide how this run gets its image.

    ``prebuilt`` uses ``settings.image`` unchanged; ``repo`` requires a root Dockerfile and
    raises :class:`ImageResolutionError` without one; ``generated`` always writes one; and
    ``auto`` prefers a repo Dockerfile but falls back to generating. For generated images the
    returned ``context`` is a temp directory the caller must remove after building.
    """
    if settings.image_source is ImageSource.PREBUILT:
        return ResolvedImage(tag=settings.image, build=False)

    repo_dockerfile = find_repo_dockerfile(source_dir) if source_dir is not None else None
    if settings.image_source is ImageSource.REPO:
        if repo_dockerfile is None:
            raise ImageResolutionError("no Dockerfile at the repository root")
        assert source_dir is not None
        return _repo_image(settings, source_dir, repo_dockerfile)
    if settings.image_source is ImageSource.AUTO and repo_dockerfile is not None:
        assert source_dir is not None
        return _repo_image(settings, source_dir, repo_dockerfile)
    return _generated_image(settings, source_dir)


def _dockerfile_name(context: Path) -> str:
    """The Dockerfile's name relative to the build context, as the daemon expects it."""
    found = find_repo_dockerfile(context)
    return found.name if found is not None else _GENERATED_DOCKERFILE


def build_image(
    client: docker.DockerClient,
    resolved: ResolvedImage,
    settings: SandboxSettings,
) -> None:
    """Build ``resolved`` and tag it, or raise :class:`ImageBuildError`.

    The build runs on a worker thread and is abandoned after ``build_timeout_seconds``. That
    cap is best-effort: the daemon may keep building in the background, but the caller stops
    waiting and reports the timeout.
    """
    if resolved.context is None:
        raise ImageBuildError("missing build context")
    dockerfile = _dockerfile_name(resolved.context)
    holder: dict[str, BaseException] = {}

    def _build() -> None:
        try:
            _image, logs = client.images.build(
                path=str(resolved.context),
                tag=resolved.tag,
                dockerfile=dockerfile,
                pull=settings.pull_base_image,
                network_mode=None if settings.build_network else "none",
                labels={"janus.sandbox": "1"},
                rm=True,
            )
            for _ in logs:
                pass
        except (BuildError, DockerException) as exc:
            holder["error"] = exc

    thread = threading.Thread(target=_build, daemon=True)
    thread.start()
    thread.join(timeout=settings.build_timeout_seconds)
    if thread.is_alive():
        raise ImageBuildError("image_build_timeout")
    error = holder.get("error")
    if error is not None:
        raise ImageBuildError(str(error))


def remove_image(client: docker.DockerClient, tag: str) -> None:
    """Remove ``tag`` best-effort; a missing or in-use image is not an error here."""
    with contextlib.suppress(DockerException):
        client.images.remove(tag, force=True)
