"""Image resolution and per-run Dockerfile generation (no Docker daemon required)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from janus.sandbox.image import (
    ImageResolutionError,
    ResolvedImage,
    build_context_files,
    find_repo_dockerfile,
    generate_dockerfile,
    image_tag,
    resolve_image,
)
from janus.sandbox.settings import ImageSource, SandboxSettings


def test_find_repo_dockerfile_finds_uppercase(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    assert find_repo_dockerfile(tmp_path) == tmp_path / "Dockerfile"


def test_find_repo_dockerfile_finds_lowercase(tmp_path: Path) -> None:
    (tmp_path / "dockerfile").write_text("FROM scratch\n")
    assert find_repo_dockerfile(tmp_path) == tmp_path / "dockerfile"


def test_find_repo_dockerfile_ignores_nested(tmp_path: Path) -> None:
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "Dockerfile").write_text("FROM scratch\n")
    assert find_repo_dockerfile(tmp_path) is None


def test_find_repo_dockerfile_missing(tmp_path: Path) -> None:
    assert find_repo_dockerfile(tmp_path) is None


def test_generate_dockerfile_is_deterministic() -> None:
    settings = SandboxSettings()
    assert generate_dockerfile(settings, None) == generate_dockerfile(settings, None)


def test_generate_dockerfile_has_base_tooling_and_workdir() -> None:
    settings = SandboxSettings(
        image="python:3.12-slim",
        tooling_packages=["pytest", "coverage"],
        workdir="/work",
    )

    text = generate_dockerfile(settings, None)

    assert text.startswith("FROM python:3.12-slim\n")
    assert "RUN pip install --no-cache-dir pytest coverage" in text
    assert "WORKDIR /work" in text
    assert "ENV PYTHONDONTWRITEBYTECODE=1" in text


def test_generate_dockerfile_uses_base_image_when_set() -> None:
    settings = SandboxSettings(image="python:3.12-slim", base_image="debian:bookworm")
    assert generate_dockerfile(settings, None).startswith("FROM debian:bookworm\n")


def test_generate_dockerfile_without_tooling_omits_install() -> None:
    settings = SandboxSettings(tooling_packages=[])
    assert "pip install --no-cache-dir" not in generate_dockerfile(settings, None)


def test_generate_dockerfile_requirements_branch_only_when_present(tmp_path: Path) -> None:
    settings = SandboxSettings()

    assert "requirements.txt" not in generate_dockerfile(settings, tmp_path)

    (tmp_path / "requirements.txt").write_text("")
    text = generate_dockerfile(settings, tmp_path)

    assert "COPY requirements.txt ./" in text
    assert "RUN pip install --no-cache-dir -r requirements.txt" in text


def test_generate_dockerfile_pyproject_branch(tmp_path: Path) -> None:
    settings = SandboxSettings()
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "uv.lock").write_text("")

    text = generate_dockerfile(settings, tmp_path)

    assert "COPY pyproject.toml ./" in text
    assert "COPY uv.lock ./" in text
    assert "RUN pip install --no-cache-dir ." in text
    assert "requirements.txt" not in text


def test_build_context_files_requirements(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("")
    assert build_context_files(tmp_path) == [tmp_path / "requirements.txt"]


def test_build_context_files_pyproject_with_lock(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "poetry.lock").write_text("")
    assert build_context_files(tmp_path) == [
        tmp_path / "pyproject.toml",
        tmp_path / "poetry.lock",
    ]


def test_build_context_files_empty(tmp_path: Path) -> None:
    assert build_context_files(tmp_path) == []


def test_image_tag_is_deterministic() -> None:
    settings = SandboxSettings()
    assert image_tag(settings, dockerfile="FROM x\n") == image_tag(settings, dockerfile="FROM x\n")


def test_image_tag_changes_with_the_dockerfile() -> None:
    settings = SandboxSettings()
    assert image_tag(settings, dockerfile="FROM x\n") != image_tag(settings, dockerfile="FROM y\n")


def test_image_tag_changes_with_a_context_file() -> None:
    settings = SandboxSettings()
    first = image_tag(settings, dockerfile="FROM x\n", context_files=[("requirements.txt", b"a")])
    second = image_tag(settings, dockerfile="FROM x\n", context_files=[("requirements.txt", b"b")])
    assert first != second


def test_image_tag_uses_the_configured_prefix() -> None:
    settings = SandboxSettings(image_tag_prefix="mybox")
    assert image_tag(settings, dockerfile="FROM x\n").startswith("mybox:")


def test_resolve_prebuilt_uses_the_image_unchanged() -> None:
    settings = SandboxSettings(image_source=ImageSource.PREBUILT, image="python:3.12-slim")

    resolved = resolve_image(settings, None)

    assert resolved == ResolvedImage(tag="python:3.12-slim", build=False)
    assert resolved.context is None


def test_resolve_repo_uses_the_repo_dockerfile(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    settings = SandboxSettings(image_source=ImageSource.REPO)

    resolved = resolve_image(settings, tmp_path)

    assert resolved.build is True
    assert resolved.context == tmp_path
    assert resolved.dockerfile == "FROM scratch\n"


def test_resolve_repo_without_dockerfile_raises(tmp_path: Path) -> None:
    settings = SandboxSettings(image_source=ImageSource.REPO)
    with pytest.raises(ImageResolutionError):
        resolve_image(settings, tmp_path)


def test_resolve_generated_creates_a_context(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("")
    settings = SandboxSettings(image_source=ImageSource.GENERATED, tooling_packages=["pytest"])

    resolved = resolve_image(settings, tmp_path)

    try:
        assert resolved.build is True
        assert resolved.context is not None
        assert resolved.context != tmp_path
        assert (resolved.context / "Dockerfile").read_text() == resolved.dockerfile
        assert (resolved.context / "requirements.txt").is_file()
        assert "COPY requirements.txt ./" in (resolved.dockerfile or "")
    finally:
        assert resolved.context is not None
        shutil.rmtree(resolved.context)


def test_resolve_auto_prefers_the_repo_dockerfile(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    settings = SandboxSettings(image_source=ImageSource.AUTO)

    resolved = resolve_image(settings, tmp_path)

    assert resolved.build is True
    assert resolved.context == tmp_path
    assert resolved.dockerfile == "FROM scratch\n"


def test_resolve_auto_generates_without_a_dockerfile(tmp_path: Path) -> None:
    settings = SandboxSettings(image_source=ImageSource.AUTO)

    resolved = resolve_image(settings, tmp_path)

    try:
        assert resolved.build is True
        assert resolved.context is not None
        assert resolved.context != tmp_path
        assert (resolved.dockerfile or "").startswith("FROM ")
    finally:
        assert resolved.context is not None
        shutil.rmtree(resolved.context)
