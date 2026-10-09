# `tests/fixtures/` — placeholder

This directory is intentionally almost empty. The **sample fixture repo** it will hold is
**deferred to Phase 4** (S1 reproduce + triage) so the adapter work in Phase 3 is not
blocked on it.

## What will live here

When it lands, the fixture is a tiny self-contained Python repository:

- a **passing** test,
- a **failing** test (a deterministic logic bug),
- a **flaky** test (fails intermittently, to exercise determinism scoring),
- a `requirements.txt`.

It is the input the reproducer runs under both the `clean` and `ambient` profiles and the
repo the Python-adapter integration test runs against inside the sandbox.

## When it lands

Set `norecursedirs` in `[tool.pytest.ini_options]` (in `pyproject.toml`) so pytest does
**not** collect the fixture repo's own tests as part of Janus's suite, e.g.:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
norecursedirs = ["tests/fixtures"]
```

Nothing here should be importable by Janus's own tests or collected by `pytest`.

## Status

No repo and no test files exist here yet — this README is the only file.
