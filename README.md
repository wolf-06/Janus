# Janus

Janus watches your CI. When a build goes red, it works out whether the fault is your
**code**, your **tests**, or your **infrastructure** — and for a code bug it localizes the
fault, writes a fix, proves it against measured gates, and opens a pull request.

A red build is a signal with an ambiguous cause, and today the response is a guess: someone
reruns it, reads the log, and either patches the code, quarantines a flaky test, or blames
the runner. Janus replaces the guess with a deterministic, explainable verdict and — only
where mutation is provably safe to attempt — a proven patch.

> Plan: [`PLAN.md`](PLAN.md) · Architecture: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) ·
> Component reference: [`docs/COMPONENTS.md`](docs/COMPONENTS.md) · Design notes:
> [`docs/DECISIONS.md`](docs/DECISIONS.md) · Specs: [`docs/specs/`](docs/specs)

## The problem, and what Janus does

A failing test can mean three mutually exclusive things: the **code** is wrong, the
**test** is wrong (non-deterministic or stale), or the **environment/pipeline** is wrong
(out of memory, a network reset, a registry rate-limit, a full disk). The correct response
to each is different, and the *wrong* response is actively harmful — editing a real bug's
test hides the bug, and "fixing" an infra outage by changing code makes the code worse.

So Janus classifies before it mutates. It produces one of four outcomes:

| Outcome | Plain meaning | What Janus does |
|---|---|---|
| `CODE_BUG` | A real defect in your source. | Proceeds to the fix loop (below). |
| `FLAKY` | The test is non-deterministic. | Comment only. |
| `ENV` | The environment or pipeline broke. | Comment only. |
| `UNSURE` | The evidence is not strong enough to route. | Comment only — Janus never guesses. |

The pipeline, end to end:

```
GitHub red build ─▶ webhook ─▶ queue ─▶ reproduce ─▶ triage ─▶ verdict
                                         (K reruns)   (7 signals)  │
                                                                    ├─ CODE_BUG ─▶ localize ─▶ retrieve
                                                                    │                          │
                                                                    │            ship ◀─ prove ◀─ generate
                                                                    │
                                                                    └─ FLAKY / ENV / UNSURE ─▶ comment only
```

## How it decides

Triage scores seven independent signals. Each signal votes support in `[0, 1]` for the
three real routes, or **abstains** when it has no evidence — a missing signal is not a zero
vote.

| Signal | What it looks at | What it argues for |
|---|---|---|
| `determinism` | the K reruns | `CODE_BUG` when every rerun fails; `FLAKY` when the failure is intermittent |
| `signatures` | stdout/stderr excerpts | `ENV` for timeout / `ECONNRESET` / 429-503 / `ENOSPC` / OOM / `SIGKILL` / DNS strings; `CODE_BUG` for assertion-style failures |
| `exit_codes` | the process exit code | `ENV` for 137 / 124 (kill / timeout); `CODE_BUG` for 1 / 2 |
| `resources` | cgroup telemetry + duration spread | `ENV` on an OOM kill, near the memory cap, or an order-of-magnitude stall; weakly `CODE_BUG` otherwise |
| `change_attribution` | the last-green → first-red diff | `CODE_BUG` when a changed file is a failing test's module; `ENV` when the diff touches nothing related |
| `correlation` | CI facts (cache, runner, matrix leg) | `ENV` for a cache miss, a retried job, a runner change, or a matrix leg that only fails there |
| `flake_history` | per-test historical failure rate | `FLAKY` at ≥ 0.30; `CODE_BUG` when every target is ≤ 0.05 |

The scorer takes a **weighted average over the signals that reported**, so an abstaining
signal does not dilute the score. Default weights sum to 1.0:

| Signal | Weight |
|---|---:|
| `determinism` | 0.22 |
| `signatures` | 0.18 |
| `change_attribution` | 0.16 |
| `exit_codes` | 0.14 |
| `correlation` | 0.10 |
| `flake_history` | 0.10 |
| `resources` | 0.10 |

The decision rule then picks the highest-scoring route **only if** it reaches `tau`
(default `0.80`) **and** leads the runner-up by more than `epsilon` (default `0.05`).
Otherwise the verdict is `UNSURE`.

This verdict is **deterministic and explainable — there is no LLM in it.** Every signal's
raw and normalized value, and the resulting diagnosis (route, confidence, top-two gap), is
stored as a database row, so a comment or PR can cite exactly why a route was chosen. The
verdict is posted as a **"Janus triage"** Check run, and — when the failing run belongs to a
pull request — as an evidence comment.

## The fix loop

Only a `CODE_BUG` verdict reaches the fix loop; `FLAKY`, `ENV` and `UNSURE` stop at the
comment. The loop runs inside the checkout:

1. **Localize.** One per-test coverage run feeds SBFL (Ochiai by default, DStar optional),
   which ranks `file:line` suspects. Lines covered only by passing tests are excluded.
2. **Retrieve.** Deterministic terms from the failing tests, suspect files and error lines
   grep `git log -p` for similar past fixes — no vector database.
3. **Generate.** An OpenAI-compatible endpoint receives a bounded prompt (evidence,
   suspects, source, prior fixes) and returns a unified diff. Up to three attempts use gate
   feedback from the previous try.
4. **Prove.** A static policy gate rejects test edits, CI weakening, skip/xfail, lowered
   thresholds and removed assertions. Then four measured gates run: **full suite green**,
   **mutation non-regression**, **coverage non-regression**, and a **test-file hash lock**.
   A missing measurement fails closed.
5. **Ship.** The default `suggest` mode comments on the PR (or posts a neutral Check run
   when there is no PR). `mode: pr` in `.janus.yml` opens a **draft** PR from
   `janus/fix-<job_id>` carrying the verification table.

**Safety properties.** Janus never merges, never force-pushes, and never edits your tests.
Without an LLM key the loop degrades to a comment rather than a speculative change.

## Requirements

- [uv](https://docs.astral.sh/uv/) (Python 3.12 is pinned via `.python-version`)
- Docker + Docker Compose (for Postgres; also required by the sandbox)

## Quickstart

```bash
uv sync                          # create .venv and install locked deps
cp .env.example .env             # then edit if needed
docker compose up -d postgres    # start Postgres 16
uv run alembic upgrade head      # apply migrations
uv run uvicorn janus.app.main:app --reload
```

Then check liveness:

```bash
curl -s localhost:8000/health    # -> {"status":"ok"}
```

Run a worker in a second terminal (start several for a pool):

```bash
uv run python -m janus.jobs.worker
```

The worker consumes the `jobs` table. It claims with `SELECT ... FOR UPDATE SKIP LOCKED`,
stamps a lease and heartbeats while processing, and a reaper requeues jobs whose lease
expired — so `kill -9` is safe. It enforces full-jitter retries, cost caps, per-repo quotas
and the kill switch.

## Connecting a repository

**Install the GitHub App (manifest flow).**

1. Start a tunnel (below) and set `JANUS_PUBLIC_URL` to its HTTPS URL in `.env`.
2. Visit `http://localhost:8000/manifest/start` in a browser. It submits the App manifest
   to GitHub.
3. Approve the App. GitHub redirects to `/manifest/callback`, which writes the private key
   to `JANUS_GITHUB_PRIVATE_KEY_PATH` and records the App id, webhook secret and OAuth
   credentials in `.env`.
4. Restart the app so the new settings load, then point the App's webhook at
   `<JANUS_PUBLIC_URL>/webhooks/github` if GitHub did not set it automatically. The webhook
   secret must match `JANUS_GITHUB_WEBHOOK_SECRET`.

**Local webhooks.** GitHub cannot reach `localhost`, so expose the app with a tunnel and
point the App's webhook URL at `POST /webhooks/github`:

- [smee.io](https://smee.io/) — create a channel and forward events to the local port.
- [ngrok](https://ngrok.com/) — `ngrok http 8000` and use the HTTPS URL.

To exercise signature verification and persistence without GitHub, post a signed
`workflow_run` payload with the bundled script:

```bash
JANUS_GITHUB_WEBHOOK_SECRET=<secret> \
  uv run python scripts/post_test_webhook.py --url http://localhost:8000/webhooks/github
```

**Per-repository config.** A repository can commit a `.janus.yml` next to its code. Every
key is optional; an omitted key falls back to the process-wide setting. It is read as
untrusted repository content and must never contain secrets.

```yaml
# "suggest" comments only; "pr" also opens a draft pull request.
mode: suggest

# Reruns per failing target while reproducing (>= 1).
k_reruns: 3

# Accept a route only at or above tau, and only if it beats the runner-up by more than epsilon.
tau: 0.8
epsilon: 0.05
```

`mode`, `k_reruns`, `tau` and `epsilon` are honoured today. The parser also accepts
`max_sandbox_minutes`, `max_llm_tokens`, `setup_command` and `test_command`, but those are
**not applied yet**: the per-failure caps are process-wide only, and the commands a run
executes come from the language adapter (see below). A malformed file is reported rather
than silently ignored.

## Configuration

Process-wide settings live in [`janus.toml`](janus.toml) (or `[tool.janus]` in
`pyproject.toml`; set `JANUS_CONFIG_FILE` to use another path). Environment variables
override the file, with nested keys using a double underscore, e.g.
`JANUS_SANDBOX__IMAGE=python:3.12-slim`.

```toml
[sandbox]
image_source = "auto"          # auto | repo | generated | prebuilt (see ADR D-056)
image = "python:3.12-slim"     # the "prebuilt" image, and the generated base by default
# base_image = "python:3.12-slim"  # override the generated base (unset => use `image`)
tooling_packages = ["pytest", "pytest-json-report", "coverage", "mutmut"]
remove_image = true            # remove the per-run image after the run
build_network = true           # allow the build network (needed to install packages)
build_timeout_seconds = 900.0  # wall-clock cap for one image build
timeout_seconds = 900.0        # wall-clock cap for one command
memory_mb = 2048
cpus = 2.0
pids_limit = 256
max_output_bytes = 1048576     # per-command stdout/stderr kept
telemetry_sample_interval_seconds = 0.2
# docker_host = "unix:///run/user/1000/docker.sock"  # point at a rootless daemon
require_rootless = false       # true => fail closed unless the daemon is rootless
network = false                # false => no network AND the setup phase is skipped
allow_hosts = []               # enforced by the allowlisting egress proxy; empty = any host
```

The commands a run executes come from the **language adapter**, not from this file. The
Python adapter runs `pytest` with a JSON report and per-test coverage contexts, and needs no
setup step because its tooling is installed while the per-run image is built. A run is
therefore up to two phases — **setup** with the network on, then **tests** always offline —
and with the Python adapter only the offline test phase runs. Each run builds its own image
(from the repository Dockerfile or a Janus-generated one) and removes it afterwards;
`image_source = "prebuilt"` uses `image` as-is. When `network` is true and `allow_hosts` is
non-empty, the run joins an internal network whose only route out is an allowlisting proxy,
so only the listed hosts are reachable.

## Safety model

The sandbox is the boundary closest to the danger, so the guarantees live there:

- **No `docker.sock`** inside the sandbox and **no host bind mounts** — the source is copied
  in with `docker cp`.
- **Dropped capabilities**, `no-new-privileges`, and a **non-root** run user.
- **CPU, memory and PID caps**, plus a wall-clock timeout that kills the container.
- **Tests run offline**; when egress is allowed it is an **enforced host allowlist** through
  an internal network and an allowlisting proxy — a non-listed host is unreachable even if
  the proxy variables are ignored.
- **Output is streamed and bounded** at `max_output_bytes` (the stream is still drained so a
  chatty command cannot block).
- **Secrets are redacted** before anything leaves the sandbox.
- **`require_rootless`** makes a non-rootless daemon fail closed in production.

## Operations

- **Kill switch.** Stored as a `system_flags` row and read on every claim, so flipping it
  stops claiming immediately without a redeploy. `JANUS_KILL_SWITCH` is a hard env-level
  override.
- **Cost caps and quota.** Exceeding `max_sandbox_minutes` or `max_llm_tokens`, or a
  per-repo quota, routes the job to terminal `skipped_budget`: report what was observed,
  spend nothing more.
- **Dead-letter admin.** Inspect and requeue dead jobs:

  ```bash
  curl -s localhost:8000/admin/dead
  curl -s -X POST localhost:8000/admin/jobs/<id>/requeue
  ```

- **Crash safety.** A claim stamps a lease; a background task heartbeats it; a reaper
  requeues running jobs whose lease expired (without consuming an attempt). `kill -9` mid-job
  leaves a `running` row that is recovered, never a lost job or a half-open transaction.

## Measured performance

From [`bench/RESULTS.md`](bench/RESULTS.md) (reproduce with `uv run python bench/run_bench.py`):

| Measurement | Value |
|---|---:|
| Ingest + enqueue latency (p50 / p95 / p99) | 8.726 / 12.240 / 14.196 ms |
| Single-worker drain throughput | 50.7 jobs/s |
| Sandbox image build (cold / cached) | 20.762 s / 2.540 s |
| Sandbox trivial-command latency (p50) | 1.495 s |
| Reproduce a failing target (k=5 + full suite) | 12.015 s (`cost_minutes` 0.177) |

The benchmark's own caveats apply: it is a single-case smoke measurement, not an accuracy
metric, and it does not measure triage accuracy on a labelled set, real GitHub API latency,
LLM quality, multi-worker throughput, or chaos behaviour.

## Limitations

- **Python / pytest only.** The core is language-agnostic behind the `Adapter` protocol, but
  only the Python adapter (pytest, coverage.py, mutmut) exists.
- **No labelled accuracy benchmark yet.** The BugsInPy-style confusion matrix is not built;
  the triage result in the benchmark is a single-case smoke test.
- **The correlation signal has no CI metadata wired yet.** It reads an empty context today,
  so it abstains.
- **`git bisect` is deferred.** The localizer ranks `file:line` suspects, but the checkout is
  shallow, so no introducing commit is found.
- **The mutation gate depends on parsing mutmut output**, and can be disabled
  (`mutation_gate_enabled = false`), in which case it is reported as `disabled`.
- **The admin endpoints are unauthenticated** — put them behind your own access control.

## Documentation

- [`PLAN.md`](PLAN.md) — what the product is and what it deliberately is not.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the problem decomposed, and why the system is shaped this way.
- [`docs/COMPONENTS.md`](docs/COMPONENTS.md) — how each component works.
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — the decision log (ADRs).
- [`docs/specs/S0.md`](docs/specs/S0.md) — GitHub App spine.
- [`docs/specs/S1.md`](docs/specs/S1.md) — reproduce and triage.
- [`docs/specs/S2.md`](docs/specs/S2.md) — the `CODE_BUG` fix loop.
- [`docs/specs/SANDBOX.md`](docs/specs/SANDBOX.md) — the container runner.
- [`docs/specs/ADAPTER.md`](docs/specs/ADAPTER.md) — the adapter protocol and Python adapter.
- [`docs/specs/BACKEND.md`](docs/specs/BACKEND.md) — ingest, queue and worker.

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy janus
uv run pytest -q
uv run pre-commit install        # optional: run the same checks on commit
```

The `SKIP LOCKED` concurrency test needs Postgres:

```bash
JANUS_TEST_DATABASE_URL=postgresql+asyncpg://janus:janus@localhost:5432/janus_test \
  uv run pytest -q
```

The Docker-gated sandbox tests skip themselves when no daemon is reachable:

```bash
uv run pytest tests/integration/test_sandbox_runner.py -q
```
