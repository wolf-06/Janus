# Janus — Adapter Spec (Phase 3)

Source: `PLAN.md` §1.3 and §6, implemented via `docs/IMPLEMENTATION_PLAN.md` §2
(Phase 3) and §1.3/§1.4. This spec covers the **language-agnostic adapter interface**
and the **conformance suite** every adapter must pass. It is the second half of Phase 3;
the first half — the sandbox — is specified in [`SANDBOX.md`](SANDBOX.md).

Status: **interface, conformance suite and Python adapter implemented.** The sample fixture
repo is the remaining Phase 3 work and is **deferred to Phase 4** (see §7).

---

## 1. Scope

**In scope:**

- The `Adapter` Protocol in `janus/core/adapter.py`: the language-neutral types
  (`TestTarget`, `TestOutcome`, `CoverageContext`, `MutationResult`) and the eight
  methods that translate between a language's tooling and the core.
- The three contract rules that bind every adapter.
- The reusable conformance suite in `tests/conformance/`, plus a canned `FakeAdapter`
  that proves the suite is not vacuous.
- The artifact channel: how structured tool output leaves the container.

**Non-goals (still Phase 3, but not this spec):**

- The Python adapter (`janus/adapters/python/`): pytest, coverage.py contexts, mutmut.
- The sample fixture repo (`tests/fixtures/`) — **deferred to Phase 4**.
- The reproducer (`janus/core/reproducer.py`): the K=5 loop, profiles, retries, caps.

The rule from `PLAN.md` §6: the core stays language-agnostic, and every Python-specific
thing lives behind `core/adapter.py`, so a Node adapter is later "one new folder".

---

## 2. Contract rules

These three rules are the whole contract. The conformance suite enforces all of them.

1. **Commands are argv lists, never shell strings.** `setup_commands`, `test_command`,
   `coverage_command` and `mutation_command` return `tuple[str, ...]` values. The sandbox
   runner execs them **without a shell**, so no element may contain a shell
   metacharacter: `&&`, `||`, `;`, `|`, `$(`, a backtick, `>` or `<`. An adapter that
   builds a command by joining strings is wrong.
2. **Every parser is total.** Given empty, whitespace, random garbage, truncated or
   otherwise malformed input, a parser returns an empty list or a zeroed result — it
   **never raises**. Logs and sandbox output are untrusted; a parse failure must degrade,
   not crash the worker.
3. **The adapter holds no policy.** It never decides *whether* to run, *how often* to
   run, which profile to use, or what a result means for triage. The core owns the
   sandbox, the K=5 loop, retries and caps. The adapter only translates.

---

## 3. Interface

`janus/core/adapter.py` defines five frozen dataclasses and one `Protocol`.

### 3.1 Data

| Type | Fields | Meaning |
|---|---|---|
| `Command` | `argv: tuple[str, ...]`, `artifacts: tuple[str, ...] = ()` | One command to run: an argv list (no shell) plus the absolute container paths it writes that the runner reads back. |
| `TestTarget` | `id: str`, `node_ids: tuple[str, ...]` | `id` is how the test appears in CI logs; `node_ids` is what the runner is given. |
| `TestOutcome` | `target_id`, `passed`, `duration_s`, `exit_code`, `stdout`, `stderr`, `sandbox_error: str \| None` | One target's result in one run. `sandbox_error` is set when the run failed at the infrastructure level. |
| `CoverageContext` | `test_id: str`, `files: dict[str, set[int]]` | The lines one test executed, per file. The SBFL input for Phase 5. |
| `MutationResult` | `total: int`, `killed: int`, `survived: int` | The mutation gate input for S2. |

A `Command.artifacts` entry is a unique, non-blank, **absolute** container path (it starts
with `/`). The core reads these to know which paths to request from the sandbox, so a
structured report the core needs is declared on the command rather than scraped from stdout.

### 3.2 `Adapter` (Protocol)

| Member | Signature | Purpose |
|---|---|---|
| `name` | `str` | Identifies the adapter (used in logs, the conformance ids). |
| `parse_failing_tests` | `(log: str) -> list[TestTarget]` | Extract failing tests from a CI log. |
| `setup_commands` | `() -> list[Command]` | Dependency-install commands, in order (may be empty). |
| `test_command` | `(targets: Sequence[TestTarget]) -> Command` | Run `targets`; empty means the whole suite; argv never empty. |
| `parse_results` | `(result: SandboxResult) -> list[TestOutcome]` | Turn one sandbox run into per-target outcomes. |
| `coverage_command` | `(targets: Sequence[TestTarget]) -> Command` | Produce per-test coverage for `targets`. |
| `parse_coverage` | `(result: SandboxResult) -> list[CoverageContext]` | Read per-test coverage out of one sandbox run. |
| `mutation_command` | `(files: Sequence[str]) -> Command` | Run mutation testing over `files`. |
| `parse_mutation` | `(result: SandboxResult) -> MutationResult` | Read the mutation result out of one sandbox run. |

Every parser takes the whole `SandboxResult`, so it can read stdout **and** the extracted
artifacts (`result.artifacts`, `result.artifact_errors`). `SandboxResult` is the sandbox's
return type (`janus/sandbox/spec.py`); the adapter never constructs or runs it.

### 3.3 Boundary

The core calls the adapter; the adapter calls nothing. In particular the adapter does
**not** own `SandboxRunner`, `build_plan`, or the K=5 loop. The reproducer (Phase 4)
decides how many runs to make, which profile to use, and what to do with the outcomes.

---

## 4. Artifact channel

Structured tool output (pytest JSON, coverage, mutation reports) leaves the container by
**artifact extraction**, not stdout scraping. The sandbox runner collects declared
artifacts out of the container and exposes them as a mapping (`name -> raw bytes`);
`SandboxResult` carries them (and a parallel change adds `artifacts: dict[str, bytes]`
and `artifact_errors: dict[str, str]`).

- `parse_results(result)`, `parse_coverage(result)` and `parse_mutation(result)` read
  `result.artifacts`; they take the whole result, so they are testable without Docker and
  without a real run.
- A missing or unreadable artifact is a normal, total outcome: the parser returns `[]` or
  a zeroed `MutationResult`.
- Per-test coverage uses **one** `--cov-context=test` run, not one run per test.

The adapters' command builders **declare** the artifact files they expect on the returned
`Command.artifacts` (e.g. `--cov-report=json:/var/tmp/janus/coverage.json` paired with
`artifacts=("/var/tmp/janus/coverage.json",)`); the core tells the runner to extract those
paths. The artifact directory is on the writable root filesystem, not `/tmp`, because the
runner mounts `/tmp` as a tmpfs the extractor cannot read back.

---

## 5. Conformance suite

`tests/conformance/` is the reusable suite every adapter must pass. It is written as
plain functions so it can also be called directly from an adapter's own tests.

| File | Contents |
|---|---|
| `cases.py` | `ConformanceCase` dataclass and `available_cases()`. |
| `fake_adapter.py` | `FakeAdapter`, an `Adapter` over canned in-memory data. |
| `adapter_suite.py` | The checks: `assert_argv`, `assert_artifacts`, `assert_command`, `check_commands`, `check_parsers_are_total`, `check_parse_failing_tests`, `check_parse_results`, `check_coverage_shape`, `check_mutation_shape`. |
| `test_adapter_conformance.py` | Parametrizes over `available_cases()` × the checks. |

`ConformanceCase` bundles one adapter with: `failing_log`, `expected_target_ids`,
`passing_result`, `failing_result`, `coverage_result`, `mutation_result`. The two result
fields are runs of the targets parsed from `failing_log`, so their outcome ids are a subset
of `expected_target_ids`.

The checks assert at minimum:

- every `Command` from `setup_commands`, `test_command`, `coverage_command` and
  `mutation_command` is a valid argv list with unique, non-blank, absolute artifacts
  (`assert_command`);
- `test_command(())` is the non-empty whole-suite command, and `test_command((target,))`
  mentions that target's node ids;
- parsers are total: empty string, whitespace, random garbage and truncated input never
  raise and yield `[]` / zeroed values;
- `parse_failing_tests(failing_log)` returns exactly `expected_target_ids` (as a set);
- `parse_results(passing_result)` is all-pass with no `sandbox_error`;
  `parse_results(failing_result)` has at least one failure; returned `target_id`s are a
  subset of what was requested;
- `parse_results` on a result whose `error` is set yields outcomes carrying
  `sandbox_error` (or an empty list), never a raise;
- `parse_coverage` returns contexts with non-empty `test_id`s and only positive line
  numbers; `parse_mutation` returns a `MutationResult` with `killed + survived <= total`
  and no negative counts.

### 5.1 Adding an adapter

`available_cases()` always includes the fake case and **lazily** includes the Python case:
it imports `tests.conformance.python_case` and calls its `python_case()` factory, but only
when that module imports cleanly and returns a `ConformanceCase`. Anything else is skipped
silently — the suite never raises during collection. A new adapter adds a case (and its
own `python_case`-style factory module) and the same checks run unchanged.

`FakeAdapter` is the template a future Node adapter copies: argv-only command builders,
total parsers, and no policy.

---

## 6. Acceptance tests

| Command | Expected |
|---|---|
| `uv run pytest tests/conformance -q` | The fake case passes every check; the Python case runs too once it exists. |
| `uv run ruff check .` / `uv run ruff format --check .` | Clean. |
| `uv run mypy janus` | Clean (strict; `tests/` is not type-checked). |
| `uv run pytest -q` | All green. |

The suite is deliberately non-vacuous: `FakeAdapter` is a full adapter over canned data,
so a check that silently did nothing would fail against it.

---

## 7. Python adapter

**Python adapter (`janus/adapters/python/`) — implemented.** `PythonAdapter` composes three
modules and holds no policy:

- `pytest_runner.py` — `parse_failing_tests` (short-summary `FAILED`/`ERROR` lines and bare
  node ids), `test_command` (a `--json-report` run writing to `TEST_REPORT_PATH`), and
  `parse_results` (reads that artifact; a requested target missing from the report gets
  `sandbox_error="not_in_report"`).
- `coverage.py` — `coverage_command` (one `--cov-context=test` run) and `parse_coverage`
  (reads the JSON `contexts` mapping into `CoverageContext`s).
- `mutmut.py` — `mutation_command` and `parse_mutation` (parses the killed/survived summary
  from stdout; the cache path is unknown to the adapter, so no artifact is declared).
- `tests/conformance/python_case.py` — the `python_case()` factory, picked up by
  `available_cases()`.

**Deferred.**

- **Sample fixture repo** (`tests/fixtures/`) — **deferred to Phase 4.** It will hold a
  passing test, a failing test, a flaky test and a `requirements.txt`; `norecursedirs`
  must be set for it when it lands. See `tests/fixtures/README.md`.
- **Reproducer** (`janus/core/reproducer.py`) — Phase 4: the K=5 reruns, the clean/ambient
  profiles, the final full-suite run, retries and caps. It owns the sandbox; the adapter
  only translates.
