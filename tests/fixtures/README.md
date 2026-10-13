# `tests/fixtures/`

Fixtures for Janus's own tests. Nothing here is collected by Janus's suite.

## `sample_repo/`

The **S1 seed repository** — a tiny, stdlib-only Python repo the sandbox builds and runs.
It carries one deterministic logic bug plus a control, a driven (non-random) flake and an
infrastructure-failure case, so each triage route has a real input. See
`sample_repo/README.md` for the file list and how each test is driven.

Its own tests are **meant to fail**, so `norecursedirs` in `[tool.pytest.ini_options]`
(in `pyproject.toml`) excludes this directory — otherwise pytest would collect them as part
of Janus's suite:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
norecursedirs = ["tests/fixtures"]
```

## `recorded/`

Recorded tool outputs (pytest JSON reports, a CI log, a coverage contexts file, mutmut
stdout) used by the adapter unit tests. These are data, not tests.
