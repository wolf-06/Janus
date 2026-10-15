"""Retrieval: term building, argv construction, log parsing and ranking."""

from __future__ import annotations

from janus.core.retrieve.git_grep import PriorFix, build_terms, git_log_argv, parse_git_log, rank

TRANSCRIPT = """\
a1b2c3d Fix parser crash on empty input
diff --git a/src/parser.py b/src/parser.py
index 1111111..2222222 100644
--- a/src/parser.py
+++ b/src/parser.py
@@ -10,7 +10,7 @@ def parse(value):
-    return int(value)
+    return int(value.strip())

e4f5a6b Merge branch 'release' into main

c7d8e9f Add logo asset
diff --git a/assets/logo.png b/assets/logo.png
new file mode 100644
index 0000000..3333333
Binary files /dev/null and b/assets/logo.png differ
"""


def test_build_terms_filters_noise_and_deduplicates() -> None:
    terms = build_terms(
        failing_tests=[
            "tests/test_parser.py::TestParser::test_raises",
            "tests/test_parser.py::TestParser::test_raises",
        ],
        suspect_files=[
            "/home/user/repo/src/janus/parser.py",
            "src/parser.py",
        ],
        error_lines=[
            "2026-10-20T19:29:57Z ERROR 1234 --- [main] parser: TypeError: expected str",
            '  File "/home/user/repo/src/parser.py", line 42, in parse',
            "Address 0xDEADBEEF at line 99",
        ],
    )

    assert "test_raises" in terms
    assert "parser.py" in terms
    assert "TypeError" in terms
    assert "TestParser" in terms
    assert "/home/user" not in " ".join(terms)
    assert "2026-10-20" not in " ".join(terms)
    assert "42" not in terms
    assert len(terms) == len({term.casefold() for term in terms})
    assert len(terms) <= 8


def test_build_terms_respects_limit() -> None:
    terms = build_terms(
        failing_tests=["tests/test_a.py::test_one", "tests/test_a.py::test_two"],
        suspect_files=["src/alpha.py", "src/beta.py"],
        error_lines=["ValueError: bad thing"],
        limit=2,
    )

    assert len(terms) == 2


def test_git_log_argv_is_shell_free() -> None:
    argv = git_log_argv(("TypeError", "parser.py", "foo;rm -rf /"), max_commits=25)

    assert argv[:4] == ("git", "log", "--oneline", "-p")
    assert "--max-count=25" in argv
    assert sum(1 for arg in argv if arg.startswith("-G")) == 3
    joined = " ".join(argv)
    assert not any(character in joined for character in ";&|<>$`")


def test_git_log_argv_empty_for_no_terms() -> None:
    assert git_log_argv(()) == ()
    assert git_log_argv(("", "   ")) == ()


def test_parse_git_log_handles_normal_merge_and_binary_commits() -> None:
    fixes = parse_git_log(TRANSCRIPT)

    assert [fix.sha for fix in fixes] == ["a1b2c3d", "e4f5a6b", "c7d8e9f"]
    assert fixes[0].subject == "Fix parser crash on empty input"
    assert fixes[0].files == ("src/parser.py",)
    assert "return int(value.strip())" in fixes[0].diff_excerpt

    assert fixes[1].subject == "Merge branch 'release' into main"
    assert fixes[1].files == ()
    assert fixes[1].diff_excerpt == ""

    assert fixes[2].files == ("assets/logo.png",)
    assert "Binary files" in fixes[2].diff_excerpt


def test_parse_git_log_bounds_the_excerpt() -> None:
    huge = "a1b2c3d Big patch\ndiff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n"
    fixes = parse_git_log(huge + ("+x" * 10_000))

    assert len(fixes) == 1
    assert len(fixes[0].diff_excerpt) <= 4000


def test_parse_git_log_garbage_is_empty() -> None:
    assert parse_git_log("") == []
    assert parse_git_log("not a git log\n\njust prose") == []
    assert parse_git_log(TRANSCRIPT, max_commits=0) == []


def test_rank_scores_distinct_terms_and_is_deterministic() -> None:
    candidates = [
        PriorFix("aaa", "Fix parser TypeError", ("src/parser.py",), "ValueError path"),
        PriorFix("bbb", "Unrelated cleanup", (), "parser"),
        PriorFix("ccc", "cosmetic", (), ""),
        PriorFix("ddd", "Also parser", (), ""),
    ]

    ranked = rank(candidates, ("parser", "TypeError", "PARSER"))

    assert [fix.sha for fix in ranked] == ["aaa", "bbb", "ddd", "ccc"]
    assert [fix.score for fix in ranked] == [2.0, 1.0, 1.0, 0.0]
    assert [fix.sha for fix in rank(candidates, ("parser", "TypeError", "PARSER"))] == [
        fix.sha for fix in ranked
    ]
