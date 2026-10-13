"""The sample fixture repo must never be collected as part of Janus's own suite.

``tests/fixtures/sample_repo`` is a *repository*, not test code for Janus: its tests are
supposed to fail (that is the point). If pytest collected them they would break Janus's
suite, so ``norecursedirs`` must exclude the fixtures directory.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_REPO = REPO_ROOT / "tests" / "fixtures" / "sample_repo"


def test_fixtures_are_in_norecursedirs() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    norecursedirs = data["tool"]["pytest"]["ini_options"]["norecursedirs"]

    assert "tests/fixtures" in norecursedirs


def test_sample_repo_has_the_expected_shape() -> None:
    expected = (
        "README.md",
        "requirements.txt",
        ".janus.yml",
        "src/calc.py",
        "tests/test_logic.py",
        "tests/test_flaky.py",
        "tests/test_env.py",
        "tests/test_passing.py",
    )

    missing = [relative for relative in expected if not (SAMPLE_REPO / relative).is_file()]

    assert missing == []
