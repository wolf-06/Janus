"""The static policy gate: every known-bad diff shape is rejected, clean diffs are not."""

from __future__ import annotations

from janus.core.prove.policy import (
    PolicyViolation,
    added_lines,
    check_policy,
    diff_files,
)

_REALISTIC_DIFF = """\
diff --git a/janus/core/foo.py b/janus/core/foo.py
index 1111111..2222222 100644
--- a/janus/core/foo.py
+++ b/janus/core/foo.py
@@ -1,2 +1,3 @@
 def foo():
-    return 1
+    return 2
+    # fixed
diff --git a/docs/notes.md b/docs/notes.md
index 3333333..4444444 100644
--- a/docs/notes.md
+++ b/docs/notes.md
@@ -1 +1,2 @@
 # Notes
+More notes.
"""

_CLEAN_DIFF = """\
diff --git a/janus/core/foo.py b/janus/core/foo.py
--- a/janus/core/foo.py
+++ b/janus/core/foo.py
@@ -1 +1 @@
-    return 1
+    return 2
"""


def rules(violations: list[PolicyViolation]) -> set[str]:
    return {violation.rule for violation in violations}


def test_diff_files_parses_every_touched_path() -> None:
    assert diff_files(_REALISTIC_DIFF) == ("janus/core/foo.py", "docs/notes.md")


def test_diff_files_skips_dev_null_but_keeps_created_and_deleted_paths() -> None:
    diff = """\
diff --git a/new.py b/new.py
--- /dev/null
+++ b/new.py
@@ -0,0 +1 @@
+print("new")
diff --git a/old.py b/old.py
--- a/old.py
+++ /dev/null
@@ -1 +0,0 @@
-print("old")
"""

    assert diff_files(diff) == ("new.py", "old.py")


def test_added_lines_excludes_file_headers() -> None:
    assert added_lines(_REALISTIC_DIFF) == ("    return 2", "    # fixed", "More notes.")


def test_empty_diff_has_no_violations() -> None:
    assert check_policy("") == []


def test_clean_source_diff_has_no_violations() -> None:
    assert check_policy(_CLEAN_DIFF) == []


def test_editing_a_test_file_is_rejected() -> None:
    diff = """\
diff --git a/tests/test_foo.py b/tests/test_foo.py
--- a/tests/test_foo.py
+++ b/tests/test_foo.py
@@ -1 +1 @@
-def test_foo():
+def test_foo():  # changed
"""

    assert "edits_tests" in rules(check_policy(diff))


def test_explicit_test_files_are_rejected_even_without_a_test_like_name() -> None:
    diff = """\
diff --git a/spec/check.py b/spec/check.py
--- a/spec/check.py
+++ b/spec/check.py
@@ -1 +1 @@
-x = 1
+x = 2
"""

    violations = check_policy(diff, test_files=("spec/check.py",))

    assert "edits_tests" in rules(violations)


def test_editing_a_protected_file_is_rejected() -> None:
    diff = """\
diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -1 +1 @@
-name: ci
+name: ci-fixed
"""

    violations = check_policy(diff, protected_files=(".github/workflows/ci.yml",))

    assert "edits_protected" in rules(violations)


def test_weakening_ci_with_or_true_is_rejected() -> None:
    diff = """\
diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -1 +1 @@
-        run: pytest
+        run: pytest || true
"""

    assert "weakens_ci" in rules(check_policy(diff))


def test_weakening_ci_with_continue_on_error_and_set_plus_e_is_rejected() -> None:
    for line in ("        continue-on-error: true", "        set +e", "        run: false || true"):
        diff = f"diff --git a/ci.sh b/ci.sh\n--- a/ci.sh\n+++ b/ci.sh\n@@ -1 +1 @@\n-old\n+{line}\n"
        assert "weakens_ci" in rules(check_policy(diff))


def test_adding_a_skip_marker_is_rejected() -> None:
    for marker in (
        "@pytest.mark.skip(reason='flaky')",
        "@pytest.mark.skipif(True, reason='nope')",
        "@pytest.mark.xfail",
        "@unittest.skip('nope')",
        "test('x', () => { test.skip(); })",
    ):
        diff = (
            "diff --git a/tests/test_a.py b/tests/test_a.py\n"
            "--- a/tests/test_a.py\n"
            "+++ b/tests/test_a.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            f"+{marker}\n"
        )
        assert "skips_tests" in rules(check_policy(diff))


def test_lowering_a_coverage_threshold_is_rejected() -> None:
    diff = """\
diff --git a/pyproject.toml b/pyproject.toml
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -1 +1 @@
-      --cov-fail-under=90
+      --cov-fail-under=80
"""

    assert "lowers_threshold" in rules(check_policy(diff))


def test_lowering_a_fail_under_setting_is_rejected() -> None:
    diff = """\
diff --git a/pyproject.toml b/pyproject.toml
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -1 +1 @@
-fail_under = 90
+fail_under = 75
"""

    assert "lowers_threshold" in rules(check_policy(diff))


def test_raising_a_coverage_threshold_is_not_rejected() -> None:
    diff = """\
diff --git a/pyproject.toml b/pyproject.toml
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -1 +1 @@
-fail_under = 80
+fail_under = 90
"""

    assert "lowers_threshold" not in rules(check_policy(diff))


def test_removing_an_assertion_is_rejected() -> None:
    diff = """\
diff --git a/janus/core/foo.py b/janus/core/foo.py
--- a/janus/core/foo.py
+++ b/janus/core/foo.py
@@ -1 +1 @@
-    assert result == 1
+    return result
"""

    assert "removes_assertions" in rules(check_policy(diff))


def test_replacing_an_assertion_with_another_assertion_is_allowed() -> None:
    diff = """\
diff --git a/janus/core/foo.py b/janus/core/foo.py
--- a/janus/core/foo.py
+++ b/janus/core/foo.py
@@ -1 +1 @@
-    assert result == 1
+    assert result == 2
"""

    assert "removes_assertions" not in rules(check_policy(diff))


def test_deleting_more_test_lines_than_added_is_rejected() -> None:
    diff = """\
diff --git a/tests/test_foo.py b/tests/test_foo.py
--- a/tests/test_foo.py
+++ b/tests/test_foo.py
@@ -1,2 +1 @@
-    first()
-    second()
+    first()
"""

    assert "weakens_tests" in rules(check_policy(diff))


def test_growing_a_test_file_is_not_weakening_tests() -> None:
    diff = """\
diff --git a/tests/test_foo.py b/tests/test_foo.py
--- a/tests/test_foo.py
+++ b/tests/test_foo.py
@@ -1 +1,2 @@
     first()
+    second()
"""

    assert "weakens_tests" not in rules(check_policy(diff))


def test_violations_are_deterministic() -> None:
    diff = """\
diff --git a/tests/test_foo.py b/tests/test_foo.py
--- a/tests/test_foo.py
+++ b/tests/test_foo.py
@@ -1,2 +1,2 @@
-    assert first()
-    second()
+    assert first() || true
+    third()
"""

    assert check_policy(diff) == check_policy(diff)


def test_multiple_rules_are_all_reported() -> None:
    diff = """\
diff --git a/tests/test_foo.py b/tests/test_foo.py
--- a/tests/test_foo.py
+++ b/tests/test_foo.py
@@ -1,2 +1 @@
-    assert first()
-    second()
+    pytest.mark.skip
"""

    violations = check_policy(diff)

    assert {"edits_tests", "skips_tests", "removes_assertions", "weakens_tests"} <= rules(
        violations
    )
