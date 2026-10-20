# Janus benchmark results

- Generated: 2026-10-20T20:56:16+00:00
- Mode: **full**
- Sections run: code, static, tests, db_ingest, queue_drain, sandbox_overhead, reproduce, triage, sbfl, llm, caveats
- Benchmark: `uv run python bench/run_bench.py`

## Environment

| Field | Value |
|---|---|
| os | Linux-7.1.9-arch1-2-x86_64-with-glibc2.44 |
| cpu_count | 8 |
| python | 3.12.14 |
| timestamp | 2026-10-20T20:56:16+00:00 |
| docker | 29.7.2 |
| postgres | PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2) on x86_64-pc-linux-gnu, compiled by gcc (Debian 14.2.0-19) 14.2.0, 64-bit |


## A. code — static size

- Python files under `janus/`: **86**
- Raw lines: **11204**
- Non-blank, non-comment lines (docstrings included): **9113**
- Tests (`pytest --collect-only -q`): **611** — last line: `611 tests collected in 0.47s`
- Collection wall time: 1.467 s


| Top-level package | Python modules |
|---|---:|
| `adapters` | 6 |
| `app` | 9 |
| `benchmark` | 3 |
| `config` | 2 |
| `core` | 28 |
| `db` | 7 |
| `github` | 8 |
| `jobs` | 7 |
| `report` | 4 |
| `sandbox` | 10 |
| `janus` (root modules) | 2 |


## B. static — static analysis wall time

| Check | Wall time (s) | Exit code |
|---|---:|---:|
| `ruff check` | 0.044 | 0 |
| `ruff format --check` | 0.028 | 0 |
| `mypy` | 0.254 | 0 |

Wall time includes the `uv run` process; tool caches may be warm.


## C. tests — suite wall time

- Wall time: **92.564 s**
- Exit code: **0**
- Parsed counts: passed=609, skipped=2
- Last line: `609 passed, 2 skipped, 1 warning in 91.39s (0:01:31)`


## D. db_ingest — transactional enqueue against Postgres

- N: **200** deliveries/jobs
- Path measured: `record_delivery` + `enqueue` + `commit` in one transaction (the ingest critical path)
- p50: **8.726 ms**
- p95: **12.240 ms**
- p99: **14.196 ms**
- min: 6.199 ms; max: 15.832 ms; mean: 9.032 ms
- Total enqueue wall time: 1.806 s
- Queued jobs after ingest: **200**


## E. queue_drain — stub worker drain

- Jobs processed: **200**
- Total drain time: **3.941 s**
- Throughput: **50.7 jobs/s** (single worker, default `stub_processor`)
- Final job states: succeeded=200


## F. sandbox_overhead — generated image and container

- Generated image tag: `janus-sandbox:b1df206220b6`
- First build after removing the tag (cold): **20.762 s**
- Second build of the same tag (layers cached): **2.540 s**
- Trivial command `python -c "print(1)"`, N=5:
  - p50: **1.495 s**
  - p95: **1.733 s**
  - min/max: 1.456 / 1.741 s
  - peak memory (max over N): **1.3 MiB**
  - CPU seconds (max over N): **0.120 s**
- Trivial-run errors: none


## G. reproduce — real Reproducer on the fixture

- Target: `tests/test_logic.py::test_sum_to_includes_the_last_term`; k=5
- Total wall time: **12.015 s**
- K attempt durations (s): 1.775, 1.759, 1.776, 1.772, 1.773
- Attempts: 5; failures: 5
- Full-suite duration: **1.788 s** (failed)
- `cost_minutes`: **0.177**


## H. triage — collectors and scorer on one Reproduction

- Collection time: **0.000997 s**
- Reported signals (4): determinism, signatures, exit_codes, resources
- Abstained signals (3): change_attribution, correlation, flake_history
- Route: **UNSURE**
- Confidence (top score): **0.656**
- Top-2 gap: **0.656**
- Scores: CODE_BUG=0.656, FLAKY=0.000, ENV=0.000, UNSURE=0.000
- This is a **single-case smoke measurement**, not an accuracy metric.


## I. sbfl — coverage and localization correctness

- Real `coverage_command(())` wall time: **2.018 s**
- Real adapter context count: **0** — `PythonAdapter.parse_coverage` returned 0 contexts: coverage.py 7.16.2 writes per-file `contexts`, while the adapter expects a top-level `contexts` mapping
- Supplementary localizer check (same command; temp copy with `.coveragerc` `show_contexts = true` and the infra-only `tests/test_env.py` removed because its `RuntimeError` suppressed whole-suite context recording; benchmark-side format shim for coverage 7.x): wall time **1.992 s**
- Coverage contexts: **5**
- Failing test ids used: tests/test_logic.py::test_sum_to_includes_the_last_term
- Top-3 suspects:
  1. `src/calc.py:25` score=1.0000
  2. `src/calc.py:26` score=1.0000
  3. `src/calc.py:27` score=1.0000
- Known faulty location: **rank 2** (known line `src/calc.py:26`); rank 1: no


## J. llm — configured endpoint

n/a — no LLM endpoint configured (`build_llm(Settings())` returned `NullLLM`)

## K. caveats — what this benchmark does not measure

- **Triage accuracy on a labelled set**: not measured; there is no BugsInPy harness in this repository.
- **Real GitHub API latency**: not measured.
- **LLM latency/quality**: not measured.
- **Multi-worker throughput under load**: not measured; the drain uses one worker.
- **Crash/chaos behaviour**: not measured.
- **PR-mode end-to-end**: not measured; it needs a GitHub App installation.

