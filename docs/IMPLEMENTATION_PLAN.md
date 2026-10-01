# Janus — Development & Architecture Plan

Sources of truth:

1. `PLAN.md` (a.k.a. `JANUS_PLAN.txt`) — the **product** plan (S0–S2: reproduce, triage,
   fix).
2. `JANUS_BACKEND_SPEC-7def8ad9.md.txt` — the **backend** spec (durable, at-least-once,
   idempotent pipeline; milestones D1–D6). Re-issued 2026-10-20; D1–D3 before triage
   logic re-confirmed 2026-10-19.

This document implements both. They cover different layers, so they compose rather than
conflict: the product plan defines the *triage brain* (unchanged, behind
`worker.process(job)`), and the backend spec defines the *pipeline* that carries it. Where
they disagree about the pipeline (transport, queue, state names, deployables), **the
backend spec wins**; those deltas and the reconciliations are recorded in §9.

Two tracks:

- **Product milestones** S0 → S1 → S2 (§2). S0 is done; Phase 2 (orchestration) is done
  but is **superseded** by the backend track's D1–D3 (async, leases, dead-letter).
- **Backend track** D1 → D6 (§8). D1–D3 must land before triage logic (Phase 4). Scope is
  still S0 + S1 + S2; no `PLAN.md` §3 deferred item may start before S2 is demoed.

---

## 0. Understanding (restated)

When CI goes red, Janus answers three questions and acts:

1. **Reproduce** — rerun the failing targets K=5 times in a rootless Docker sandbox,
   then run the full suite once. Two profiles: `clean` (deterministic) and `ambient`
   (matches the failed run's image/env).
2. **Triage** — score 7 signals into three routes (`CODE_BUG`, `FLAKY`,
   `ENV/PIPELINE`); pick argmax only if `score >= tau (0.80)` and the top two are
   `> epsilon (0.05)` apart, else `UNSURE`. `UNSURE`, `FLAKY`, and `ENV` are
   **comment-only**. Only `CODE_BUG` proceeds.
3. **Fix (CODE_BUG only)** — localize (SBFL → ranked `file:line`, then `git bisect`),
   retrieve similar past fixes via `git log -p` grep, generate a **source-only** diff,
   prove it with 4 gates + a policy gate, then comment or open a PR.

Non-negotiables from `PLAN.md` §4: cost caps per failure, PR-only (no auto-merge,
no force-push), least-privilege GitHub App scopes, installation tokens only, secret
redaction before logs reach the LLM, sandbox with no `docker.sock`, global kill
switch + per-repo quotas, and "no claim without a gate."

---

## 1. Architecture

### 1.1 Layer view

```
            GitHub (App + webhooks)
                     │  HMAC-signed events
                     ▼
   ┌─────────────────────────────────────────────┐
   │  api (FastAPI deployable)                    │
   │  manifest flow · HMAC verify ·               │
   │  delivery dedup · enqueue · 503 backpressure │
   └───────────────┬─────────────────────────────┘
                   │ deliveries + jobs  (Postgres = source of truth)
                   │ Redis = wake-up + rate-limit counters only
                   ▼
   ┌─────────────────────────────────────────────┐
   │  worker (deployable, N processes)            │
   │  state machine · leases + reaper · retries   │
   │  (full jitter) · dead-letter · fairness ·    │
   │  rate limits · daily budgets · dedup · caps  │
   └───────────────┬─────────────────────────────┘
                   │
       ┌───────────┴────────────┬───────────────┐
       ▼                        ▼               ▼
 ┌───────────┐          ┌──────────────┐  ┌──────────────┐
 │ Sandbox   │          │ Core (lang-  │  │ GitHub       │
 │ Runner    │◀────────▶│ agnostic)    │  │ Surface      │
 │ rootless  │  adapter │ repro ·      │  │ checks ·     │
 │ docker ·  │  Protocol│ triage ·     │  │ comments ·   │
 │ telemetry │          │ localize ·   │  │ PRs          │
 │ redaction │          │ retrieve ·   │  └──────────────┘
 └───────────┘          │ generate ·   │
                        │ prove        │
                        └──────┬───────┘
                               │ adapter calls
                        ┌──────▼───────┐
                        │ Python       │
                        │ adapter      │
                        │ pytest ·     │
                        │ coverage.py ·│
                        │ mutmut       │
                        └──────────────┘
```

### 1.2 Component responsibilities

| Component | Responsibility | Milestone |
|---|---|---|
| **Ingress (api)** | GitHub App manifest install, webhook HMAC verification, delivery dedup, event filtering, transactional enqueue, 503 backpressure | S0 / D1 |
| **GitHub client** | App JWT → installation token; fetch run logs, commits/diff, post Checks, PRs, comments; outbound token-bucket + 429 handling | S0/S2 / D4 |
| **Persistence** | Postgres schema + async repository layer + Alembic migrations | S0 / D1 |
| **Worker / orchestrator** | Postgres queue, state machine, leases + heartbeat + reaper, retries with full jitter, dead-letter, cost accounting, fairness, kill switch | S0→S2 / D1–D4 |
| **Fairness + rate limiting** | fewest-running-per-repo claim, per-repo concurrency cap, per-installation GitHub token bucket (Redis) | D4 |
| **Budgets** | per-repo daily sandbox-minute / LLM-token budget → `skipped_budget` | D4 |
| **Failure dedup** | fingerprint normalize + cache verdict + partial unique index | D5 |
| **Output idempotency** | hidden marker on Check runs/comments; update instead of duplicate | D2/D3 |
| **Observability** | OTel traces, Prometheus metrics, JSON logs with ids, Grafana dashboard | D6 |
| **Admin** | dead-letter listing + requeue endpoints | D3 |
| **Load test** | locust/k6 harness + `bench/RESULTS.md` | D6 |
| **Sandbox runner** | Ephemeral rootless Docker containers, profiles, mounts, network allowlist, resource caps, telemetry, log capture | S1 |
| **Redaction** | Scrub secrets / `.env` / tokens / high-entropy strings before LLM/UI | S1 |
| **Adapter interface** | Language-agnostic `Protocol`: discover targets, run tests, parse results, per-test coverage, mutate | S1 |
| **Python adapter** | pytest, coverage.py (contexts), mutmut | S1/S2 |
| **Reproducer** | K=5 reruns + final full-suite run, clean & ambient profiles | S1 |
| **Signal collectors** | The 7 signals → normalized evidence | S1 |
| **Triage scorer** | Weights + `tau`/`epsilon` decision → route | S1 |
| **Localizer** | SBFL (Ochiai/DStar) ranked `file:line`, then sandbox `git bisect` | S2 |
| **Retriever** | `git log -p` grep for similar fault signatures | S2 |
| **Generator** | LLM minimal source-only unified diff, retry with verifier feedback | S2 |
| **Proof gates** | Suite green, mutation non-regression, coverage non-regression, test-file hash lock | S2 |
| **Policy gate** | Reject test edits / `continue-on-error` / `|| true` / `set +e` / skip/xfail / threshold lowering / assertion removal | S2 |
| **Reporter** | Check runs, evidence comments, PR body with verification table | S0/S1/S2 |
| **Config** | `.janus.yml` (`mode`, K, tau, epsilon, caps) + env settings | S1/S2 |
| **Benchmark** | BugsInPy subset harness + metrics table | S2 |

### 1.3 Repository layout

```
janus/
  app/            FastAPI deployable (the "api" process): entrypoint, config, health
    api/          webhooks.py, manifest.py, health.py, admin.py
  github/         app_auth.py, client.py, webhook_verify.py, manifest.py, ratelimit.py
  db/             models.py, session.py (async), repository.py, migrations/ (alembic)
  jobs/           the "worker" process: queue.py, worker.py, state.py, caps.py,
                  lease.py (heartbeat + reaper), budgets.py, dedup.py, ratelimit.py
  observability/  tracing.py (OTel), metrics.py (Prometheus)
  sandbox/        runner.py, profiles.py, telemetry.py, redaction.py, allowlist.py
  core/           language-agnostic engine
    reproducer.py
    signals/      base.py + determinism/signatures/exit_codes/resources/
                  change_attribution/correlation/flake_history
    triage/       scorer.py, routes.py
    localize/     sbfl.py, bisect.py
    retrieve/     git_grep.py
    generate/     llm.py, prompt.py
    prove/        gates.py, policy.py
    adapter.py    Protocol / ABC
  adapters/
    python/       adapter.py, pytest_runner.py, coverage.py, mutmut.py
  report/         check_run.py, comment.py, pr.py, markers.py (output idempotency)
  config/         janus_yml.py
  benchmark/      bugs_in_py.py, metrics.py
bench/            locust/k6 load scripts, RESULTS.md
tests/            unit / integration (testcontainers) / chaos
docs/             IMPLEMENTATION_PLAN.md, DESIGN.md, specs/S0.md S1.md S2.md
```

**Mapping to the backend spec's §9 layout** (kept as one flat package to avoid a
churny rename of the already-built S0 code; new modules are added under the existing
names):

| Backend spec | Here | Note |
|---|---|---|
| `api/` | `janus/app/` (+ `app/api/admin.py`) | FastAPI deployable |
| `worker/` | `janus/jobs/` | claim loop, heartbeat, reaper, runner |
| `core/` (models, queue ops, dedup, ratelimit, budgets) | `janus/db/`, `janus/jobs/{queue,dedup,budgets,ratelimit}.py`, `janus/github/ratelimit.py` | split by concern, not one folder |
| `triage/` | `janus/core/*` + `janus/adapters/` | the language-agnostic brain + Python adapter |
| `bench/` | `bench/` | new, at repo root |
| `docs/DESIGN.md` | `docs/DESIGN.md` | new, required by spec §10 |

Rule from `PLAN.md` §6: the **core stays language-agnostic**; every Python
specific lives behind `core/adapter.py` in `adapters/python/`.

### 1.4 Data model (Postgres, minimal)

| Table | Purpose | Key fields |
|---|---|---|
| `installations` | GitHub App installs | installation_id, account, app_id, scopes |
| `repositories` | tracked repos + config | repo_id, owner/name, default_branch, `.janus.yml` cache |
| `deliveries` | webhook deliveries (ingest idempotency) | delivery_id (`X-GitHub-Delivery`, PK), repo_id, event, received_at |
| `runs` | observed workflow runs | run_id, repo_id, head_sha, conclusion, baseline_sha (last-green) |
| `jobs` | work queue | id, delivery_id, repo_id, fingerprint, **state**, attempts, max_attempts, run_after, locked_by, lease_expires_at, last_error, created_at, finished_at |
| `reproductions` | rerun evidence | job_id, profile, per-run result, exit code, telemetry JSON |
| `signals` | scored evidence | job_id, signal_name, raw, normalized, weight |
| `diagnoses` | triage outcome | job_id, route, confidence, top2_gap, evidence JSON |
| `suspects` | SBFL/bisect output | job_id, file, line, score, introducing_commit |
| `fixes` | generated diffs | job_id, suspect_id, diff, attempt, status |
| `gate_results` | proof outcomes | fix_id, gate, passed, measured_value, baseline_value |
| `budget_usage` | per-repo daily spend | repo_id, day, sandbox_minutes, llm_tokens |
| `benchmark_runs` | BugsInPy harness | bug_id, predicted_route, actual, fix_rate, false_fix, t2g |

Notes (backend spec §4.2, §4.4, §4.3):

- **Ingest idempotency** is `deliveries.delivery_id` (PK) with
  `INSERT ... ON CONFLICT DO NOTHING`; the delivery row and the job row are written in
  **one transaction** (spec §4.1).
- **Claiming** uses `SELECT ... WHERE state='queued' AND run_after<=now()
  ORDER BY run_after FOR UPDATE SKIP LOCKED LIMIT 1`.
- **Leases**: a running job carries `locked_by` + `lease_expires_at`; a heartbeat extends
  it, and a reaper requeues jobs whose lease expired (spec §4.2).
- **Dedup**: a **unique partial index** on `jobs(fingerprint) WHERE state IN
  ('queued','running')` coalesces concurrent duplicates (spec §4.4).
- **States** are `queued → running → succeeded | failed | dead`, plus `skipped_budget`.
  Route (`CODE_BUG`/`FLAKY`/`ENV`/`UNSURE`) lives on `diagnoses`, not on the queue state
  (§9 R2).

Alembic owns all schema changes.

---

## 2. Development phases

Each phase is a spec file (`docs/specs/`), each spec lists acceptance tests, and the
agent does not advance until those tests pass. Phases 1, 4, 6 map to milestones
S0, S1, S2; the rest are enabling work that `PLAN.md` §5 assumes.

### Phase 0 — Scaffold & foundations
**Build:** package layout, `pyproject.toml`, deps (FastAPI, uvicorn, SQLAlchemy,
alembic, pydantic-settings, httpx, docker SDK, pytest, ruff, mypy), settings module,
structured logging, DB session, Alembic init, `docker-compose` Postgres, local
webhook tunnel note (smee.io/ngrok).
**Acceptance:** `pytest` runs; `ruff` + `mypy` clean; app boots; `GET /health` = ok;
migration creates an empty schema.

### Phase 1 — S0 GitHub App spine
**Build:** manifest-flow endpoints; webhook endpoint with HMAC-SHA256 verification;
event handling (`workflow_run`/`check_run`); JWT→installation-token client; fetch
run logs + last-green→first-red diff (commits API); post Check run; schema +
migrations for installations/repositories/runs/jobs/diagnoses; enqueue on failed run.
**Acceptance:** signature verify (valid/invalid) unit tests; manifest callback stores
credentials; Check-run post mocked; diff computation correct; **E2E local: push a
failing commit → Janus posts a Check run.**

### Phase 2 — Orchestration core *(done, superseded)*
Built a synchronous Postgres queue: `SKIP LOCKED`, state machine, idempotency key, cost
caps, per-repo quota, kill switch. This is **superseded by Phase 2R** (D1–D3), which
converts it to async, adds leases/reaper, a dead-letter state, and per-delivery ingest
idempotency. The Phase 2 rows and the rework are tracked in §8/§9 (R1, R2).

### Phase 2R — Durable pipeline (backend D1–D3)
**Build:** async SQLAlchemy + asyncpg; `deliveries` table with
`INSERT ... ON CONFLICT DO NOTHING`; expanded `jobs` (state/lease/fingerprint columns);
claim by `run_after` with `SKIP LOCKED`; worker pool with heartbeat + reaper; retries
with **full jitter** (transient only); dead-letter state + admin routes; 503 backpressure
on queue depth.
**Acceptance (spec §6):** replay the same delivery 100× → one job (D1); `kill -9` a worker
mid-job → job completes once, no duplicate output (D2); fault-injected GitHub/LLM (5xx /
429 / timeouts) all recover, permanent faults land in `dead` with a reason (D3).

### Phase 2F — Fairness, rate limits, budgets (backend D4)
**Build:** fairness in the claim query (fewest running jobs per repo first), per-repo
concurrency cap (default 2), Redis token-bucket per installation for outbound GitHub
calls (respect `X-RateLimit-Remaining`/`Retry-After`; 429 requeues without burning an
attempt), per-repo daily sandbox/LLM budgets → `skipped_budget` + comment.
**Acceptance:** a noisy repo (500 jobs) does not delay a quiet repo's job beyond a stated
bound; a 429 storm causes zero lost jobs.

### Phase 3 — Sandbox runner + adapter interface
**Build:** rootless Docker runner (ephemeral, network-isolated with allowlist,
read-only mounts, CPU/mem/pids/time caps, **no `docker.sock` in sandbox**); clone via
installation token with a credential helper (token never in URL); cgroup telemetry;
secret redaction pipeline; `core/adapter.py` Protocol; Python adapter (pytest,
coverage.py contexts, mutmut).
**Acceptance:** sample repo runs under pytest in the sandbox and returns structured
results + telemetry; redaction unit tests (secrets/`.env`/high-entropy); caps enforced;
adapter conformance test suite passes for the Python adapter.

### Phase 4 — S1 Reproduce + Triage
**Build:** reproducer (K=5 reruns of failing targets, full suite once at end; clean +
ambient profiles); the 7 signal collectors; scoring engine with configurable weights,
`tau`, `epsilon`, argmax/UNSURE logic; evidence comment rendering; pipeline wiring
(failed run → reproduce → triage → Check update + comment); `.janus.yml`/settings for
K/tau/epsilon.
**Acceptance:** seed repo with a logic bug, a flaky test, and an infra break is labeled
differently with evidence; confusion matrix over the seed set is sensible; `UNSURE`
comments only; thresholds configurable.

### Phase 4D — Failure dedup (backend D5)
**Build:** `fingerprint = sha256(repo_id + workflow + normalized failing test ids +
normalized top error lines)`; normalization strips timestamps, paths, hashes, line
numbers. If an identical fingerprint was triaged within T hours and its commit range is
unchanged, reuse the cached verdict and post a short "same failure as <run>" comment.
Unique partial index on `(fingerprint) WHERE state IN ('queued','running')`; track
`dedup_hits`.
**Acceptance:** 100 identical failures → 1 triage + 99 cache hits; distinct failures are
not merged (false-merge rate measured on the BugsInPy subset).

### Phase 5 — S2 Localize + Retrieve + Generate
**Build:** SBFL (Ochiai/DStar) over per-test coverage contexts → ranked `file:line`;
sandbox `git bisect` for the introducing commit; retrieval via `git log -p` grep on
fault signature (error type, location, change shape); LLM generation of a minimal
source-only unified diff (1 attempt/suspect, ≤3 retries with verifier feedback).
**Acceptance:** on known bugs the suspect list contains the true `file:line`; bisect
identifies the introducing commit; generated diffs apply cleanly and never touch tests.

### Phase 6 — S2 Proof + Policy gates
**Build:** Gate 1 full suite green; Gate 2 mutation non-regression (`mutmut`, targeted
to changed lines); Gate 3 coverage non-regression; Gate 4 test-file hash lock; policy
gate (reject test edits, `continue-on-error`, `|| true`, `set +e`, unexplained
skip/xfail, lowered coverage thresholds, removed assertions, weakened tests); retry
loop using gate feedback; fallback to comment with evidence.
**Acceptance:** each gate has pass/fail unit tests; policy rejects a corpus of known-bad
diffs; false-fix rate ≈ 0 on the sample set; failed proof after retries falls back to a
comment.

### Phase 7 — S2 PR mode + surfaces
**Build:** `.janus.yml` `mode: pr|suggest` (**default suggest**); PR creation with
diagnosis, root cause, diff, and verification table; PR-only, no auto-merge, no
force-push; comment rendering parity with PR body.
**Acceptance:** suggest mode comments only; pr mode opens a PR with the verification
table; assertions that no merge/force-push API path exists.

### Phase 7O — Observability + load test (backend D6)
**Build:** OpenTelemetry traces (one per delivery; spans for ingest, queue wait, claim,
log fetch, sandbox, LLM, post; trace id propagated via the job row); Prometheus metrics
(`jobs_enqueued`, `jobs_by_state`, `queue_age_seconds`, `job_duration_seconds` by stage,
`retries_total`, `dead_total`, `dedup_hits`, `github_429_total`, `lease_reaped_total`);
structured JSON logs with `delivery_id`/`job_id`/`repo_id`; Grafana dashboard JSON +
screenshot; `locust`/`k6` load test committed with mock GitHub and a stub triage step
(realistic latency) writing `bench/RESULTS.md`.
**Acceptance:** traces/metrics/dashboard running; the load scenarios (steady, burst,
worker-count sweep, kill-during-burst, injected 5xx/429) produce a report with ingest
p50/p95/p99, queue-age p95, time-to-drain, throughput-vs-workers, and **0 lost / 0
duplicated jobs**. Publish measured numbers only.

### Phase 8 — Benchmark + hardening + docs
**Build:** BugsInPy subset harness (25–30 bugs); seed repo + one real repo;
metrics: triage confusion matrix, fix rate, false-fix rate, mutation/coverage delta,
median time-to-green; README "What's next" (deferred items from `PLAN.md` §3);
production-hardening note (gVisor).
**Acceptance:** benchmark table generated end to end; seed repo and one real repo both
produce a proven PR.

---

## 3. Cross-cutting concerns

- **Security:** least-privilege App scopes (Contents RW, PRs RW, Checks RW, Actions
  read, Metadata read); installation tokens only; secret redaction before LLM/UI; no
  `docker.sock` in sandbox; network allowlist; read-only mounts; resource caps.
- **Config:** env settings (`pydantic-settings`) for infra; `.janus.yml` for per-repo
  behavior (`mode`, K, tau, epsilon, caps).
- **Testing strategy:** unit (signals, scorer, gates, policy, redaction, signature
  verify); integration (sandbox against a fixture repo); E2E (local webhook → Check);
  benchmark (BugsInPy subset).
- **Observability (backend spec §4.6):** structured JSON logs carrying
  `delivery_id`/`job_id`/`repo_id`; `jobs`/`diagnoses`/`gate_results` rows and cost
  accounting per failure; OpenTelemetry traces (one per delivery, trace id on the job
  row); Prometheus metrics; a shipped Grafana dashboard. Only claim OTel/Prometheus on the
  resume once they are actually running.
- **Repo hygiene:** ruff, mypy, pytest, pre-commit, and a CI workflow for Janus itself.

---

## 4. Key design decisions & risks

- **Docker without `docker.sock`:** the worker talks to the Docker daemon on the host
  via a controlled channel; sandbox containers never receive the socket. (Decision to
  confirm in Phase 3.)
- **SBFL needs per-test coverage contexts:** Ochiai/DStar over failing-vs-passing test
  coverage; depends on coverage.py `--context` support — a reason the Python adapter is
  first.
- **Ambient profile fidelity:** matching the failed run's image/env is best-effort; if
  it can't be reconstructed, triage may lean to `UNSURE` (safe, comment-only).
- **LLM diff reliability:** the verification loop (up to 3 retries with gate feedback)
  is the safeguard; failed proof always degrades to a comment.
- **Bisect cost:** bounded by sandbox-time caps; on cap, fall back to SBFL-only
  localization and lower confidence.
- **Queue vs. Temporal:** intentionally a Postgres job table (now with leases + reaper);
  revisit only at scale. Interview defense in `docs/DESIGN.md` (§10).
- **Sync → async rework:** adopting async SQLAlchemy + `asyncpg` rewrites the Phase 2
  code and moves integration tests to testcontainers; done before triage so the brain is
  built on the final transport (§9 R1).
- **Leases, not locks, for crash recovery:** a `kill -9` cannot release a row lock, so
  correctness under crashes relies on lease expiry + a reaper, not on transactions alone.
- **At-least-once + idempotent effects:** exactly-once is a non-goal; duplicates are made
  harmless by delivery dedup, output markers, and the dedup fingerprint.
- **Dedup false-merges:** over-eager normalization could merge genuinely distinct
  failures; measured on the BugsInPy subset (D5).
- **Redis is not on the critical path:** a flush must lose no jobs; polling fallback is
  mandatory (§9 R5).

---

## 5. Open questions (to resolve at the relevant phase, not blockers)

1. Which workflow events are authoritative — `workflow_run` (Actions) or `check_run`
   (generic)? Plan implies Actions (`workflow_run`) with Check-run output. *(Phase 1 —
   resolved: Actions, `check_run` acknowledged not enqueued.)*
2. Exact "last-green → first-red" baseline: previous successful run on the branch vs.
   merge-base with default branch. *(Phase 1 — resolved: previous success on the branch,
   ancestry-verified.)*
3. Which BugsInPy repos/bugs make the 25–30 subset. *(Phase 8)*
4. Concrete default cost caps (sandbox minutes, LLM tokens, fix retries). *(Phase 2 —
   partly resolved: per-job caps exist; backend spec replaces these with per-repo **daily
   budgets** → `skipped_budget`. Defaults still to confirm in Phase 2F.)*
5. Backend spec §4.2 — `LISTEN/NOTIFY` vs Redis pubsub for wake-up, and whether Redis is
   required or optional-with-polling-fallback. *(Phase 2R; recommendation: optional,
   polling fallback required — §9 R5.)*
6. Backend spec §4.4 — dedup window `T` and "commit range unchanged" semantics.
   *(Phase 4D)*

---

## 6. Immediate next step

S0 (Phases 0–1) and the synchronous Phase 2 are built. The next work is the backend
track's **Phase 2R (D1–D3)**: `docs/specs/BACKEND.md`, then the durable pipeline
(`deliveries` ingest, async DB, leases + reaper, dead-letter) — see §8 and §10. Do not
start any deferred item (`PLAN.md` §3) before S2 is demoed.

---

## 7. S0 Implementation Plan (Phase 0 + Phase 1)

This section converts §2 Phase 0/Phase 1 into an ordered, executable task list. Scope is
**S0 only**. Stop at the S0 demo (webhook → Check run, locally) and do **not** begin
Phase 2+ (orchestration, sandbox, triage, fix) until S0 is demoed. Canonical product plan
stays `PLAN.md`; the architecture stays in §1 above.

### 7.1 Toolchain decisions (greenfield — no prior convention to match)

| Area | Choice | Rationale |
|---|---|---|
| Env/deps | `uv` + `pyproject.toml` + `uv.lock` | `uv 0.12.5` is installed; fast, reproducible; no poetry present. |
| Python | pin `3.12` (`uv python pin 3.12`), `requires-python = ">=3.12"` | System default is 3.14; ecosystem wheels (docker SDK, SQLAlchemy, mutmut) lag, so 3.12 is the safe floor. |
| Packaging | flat `janus/` package at repo root | matches §1.3 layout exactly. |
| HTTP | `httpx` (async), mocked with `respx` | GitHub client + tests. |
| DB | Postgres 16 via `docker-compose.yml`; **SQLAlchemy 2 async + `asyncpg`**; Alembic (sync `psycopg` for migrations) | backend spec §8 mandates async; one event loop serves api + worker I/O. Supersedes the S0 sync/`psycopg` choice (§9 R1). |
| Cache / wake-up | **Redis 7**, optional | rate-limit counters + wake-up signal only; **never** the source of truth, polling fallback required (spec §3, §4.2). |
| Observability | **OpenTelemetry** + **prometheus-client** | traces + metrics required by spec §4.6. |
| Test DB | **testcontainers** (Postgres/Redis) for integration; pure-unit tests stay DB-free | spec §8; replaces SQLite for anything exercising real DB behaviour (§9 R6). |
| Load test | **locust** or **k6** | spec §7; script + `bench/RESULTS.md` committed. |
| Config | `pydantic-settings`, `JANUS_` env prefix | infra from env now; `.janus.yml` per-repo arrives in S1/S2. |
| Logging | `structlog` (JSON) | structured logs required by §3. |
| Quality | `ruff` (lint+format), `mypy` (strict on `janus/`), `pytest` (+`pytest-asyncio`, `respx`), `pre-commit` | §3 repo hygiene + CI. |
| Auth | `PyJWT[crypto]` + `cryptography` | GitHub App JWT (RS256) → installation token. |

Local webhook delivery via `smee.io`/ngrok (documented in README; no tunnel code in S0).
Secrets in `.env` (gitignored) with committed `.env.example`.

### 7.2 Phase 0 — Scaffold & foundations (ordered tasks)

1. **Repo bootstrap**: commit the current plan docs as the initial commit; remove the
   duplicate `JANUS_PLAN.txt` (or replace it with a symlink to `PLAN.md`).
2. **`pyproject.toml`**: metadata, `requires-python`, runtime + dev deps, and tool config
   `[tool.ruff]`, `[tool.mypy]`, `[tool.pytest.ini_options]` (`testpaths = ["tests"]`).
3. **Layout**: create the §1.3 package tree with `__init__.py` in every package; stub
   modules empty until Phase 1.
4. **Hygiene files**: `.gitignore` (`.venv/`, `__pycache__/`, `.env`, caches),
   `.env.example`, `.pre-commit-config.yaml`.
5. **`janus/app/config.py`**: `Settings(BaseSettings)` with `database_url`, `log_level`,
   `github_app_id`, `github_private_key_path`, `github_webhook_secret`,
   `github_client_id`, `github_client_secret`; `env_prefix="JANUS_"`, `env_file=".env"`.
6. **`janus/app/logging.py`**: `configure_logging(level)` → structlog JSON renderer.
7. **`janus/db/session.py`**: `create_engine(settings.database_url)`, `SessionLocal`,
   `get_session()` FastAPI dependency.
8. **`janus/db/models.py`**: `Base(DeclarativeBase)`; tables added in Phase 1.
9. **Alembic**: `alembic init janus/db/migrations`; wire `env.py` to `settings.database_url`
   and `Base.metadata`; `alembic.ini` reads the URL from env (not hardcoded).
10. **`janus/app/main.py`**: FastAPI app factory, lifespan (logging + engine), router
    includes, `GET /health` → `{"status": "ok"}`.
11. **`docker-compose.yml`**: `postgres:16` service with healthcheck, volume, env from `.env`.
12. **Tests**: `tests/conftest.py`, `tests/unit/test_config.py` (env/prefix parsing),
    `tests/unit/test_health.py` (TestClient `/health` == ok).
13. **CI**: `.github/workflows/ci.yml` — `uv sync`, `ruff check`, `ruff format --check`,
    `mypy janus`, `pytest`.
14. **`README.md`**: quickstart (`uv sync`, `docker compose up -d`, `alembic upgrade head`,
    `uvicorn`), smee.io webhook note, S0 scope + non-goals.

**Gate (Phase 0):** `pytest` passes; `ruff` + `mypy` clean; app boots; `GET /health` = ok;
`alembic upgrade head` creates an empty schema against compose Postgres.

### 7.3 Phase 1 — S0 GitHub App spine (ordered tasks)

1. **Schema + migration**: tables `installations`, `repositories`, `runs`, `jobs`,
   `diagnoses` (§1.4 fields); one Alembic revision; `alembic upgrade head` clean.
2. **`janus/db/repository.py`**: typed CRUD — upsert installation/repository, insert run,
   create job, set diagnosis; idempotency keyed on `run_id`/delivery id.
3. **`janus/github/webhook_verify.py`**: `verify_signature(secret, body, header)` — constant-time
   HMAC-SHA256 over raw body vs `X-Hub-Signature-256`; returns bool, never raises on mismatch.
4. **`janus/github/app_auth.py`**: `app_jwt(settings)` (RS256, ≤10 min) and
   `installation_token(jwt, installation_id)` via httpx; in-memory cache with expiry.
   **Tokens never appear in clone URLs** (§4).
5. **`janus/github/client.py`**: installation-token REST client — get workflow run, download
   logs, list commits, compare commits (last-green→first-red), create Check run, post issue
   comment. All HTTP injectable/mockable.
6. **Baseline diff**: resolve last-green run on the branch vs first-red head; compute the
   `compare` diff; store `baseline_sha` on `runs`. Resolves §5 Q2 provisionally as
   "previous successful run on the branch" (documented in code + spec).
7. **`janus/app/api/webhooks.py`**: `POST /webhooks/github` — read raw body, verify HMAC
   (401 on failure), filter `workflow_run` (completed + failure) and `check_run`, map to
   repo/installation, create `runs` + `jobs`, return 202; idempotent on delivery id.
8. **`janus/github/manifest.py` + `janus/app/api/manifest.py`**: `GET /manifest/start`
   (redirect to GitHub manifest creation) and `GET /manifest/callback` (exchange `code` →
   app id / private key / webhook secret), persist credentials, redirect to success page.
9. **`janus/report/check_run.py`**: on enqueued failed run, post a Check run (`in_progress`)
   proving the webhook → Check path.
10. **Tests**:
    - `tests/unit/test_webhook_verify.py` — valid signature passes; tampered body/signature fails.
    - `tests/unit/test_manifest.py` — callback exchange mocked (`respx`); credentials persisted.
    - `tests/unit/test_diff.py` — last-green→first-red compare selects correct base/head.
    - `tests/unit/test_check_run.py` — Check-run POST payload mocked (`respx`).
    - `tests/integration/test_webhook_flow.py` — signed payload → run+job persisted → Check posted.
11. **E2E (manual, documented in `docs/specs/S0.md`)**: smee.io tunnel → push a failing commit
    to the seed repo → Janus verifies, enqueues, posts a Check run.

**Gate (Phase 1 / S0):** the four unit suites pass; the integration test proves persist +
Check; the manual E2E posts a real Check run on a red build. Matches `PLAN.md` §5 S0
("webhook to Check run works end to end, locally").

### 7.4 `docs/specs/S0.md` (next artifact — required content)

Mirror §7.2/§7.3 and include:
- **Scope**: S0 only; explicit non-goals (reproduce/triage/fix) start only after the S0 demo.
- **Acceptance tests** (exact commands + expected):
  - `uv run pytest tests/unit/test_webhook_verify.py` → valid passes, tampered fails.
  - `uv run pytest tests/unit/test_manifest.py` → credentials persisted.
  - `uv run pytest tests/unit/test_diff.py` → correct last-green→first-red.
  - `uv run pytest tests/unit/test_check_run.py` → mocked Check posted.
  - `uv run pytest` → all green; `uv run ruff check .` / `uv run mypy janus` clean.
  - Manual E2E checklist: smee.io + failing push → Check run appears.
- **Definition of done**: `PLAN.md` §5 S0 demo achieved locally.

### 7.5 Verification commands (run at each gate)

```
uv sync
docker compose up -d postgres
uv run alembic upgrade head
uv run ruff check . && uv run ruff format --check .
uv run mypy janus
uv run pytest -q
uv run uvicorn janus.app.main:app --reload   # then GET /health
```

From **Phase 2R** onward the pipeline is async and runs as two deployables: the same
commands still apply, but `docker compose up -d` brings up `postgres` + `redis` (and later
`api`/`worker`/`prometheus`/`grafana`), and DB-touching integration tests run against
**testcontainers** rather than SQLite (see §9 R1/R6).

### 7.6 Git workflow

- Initial commit = the current plan docs (`PLAN.md`, `docs/`).
- One commit per task group (bootstrap, config/db, app, github, webhook, manifest, tests).
- Optional branch `feat/s0-spine`; no remote/PR workflow needed until a remote exists.

### 7.7 Open decisions to confirm before coding

1. Python pin 3.12 vs system 3.14 — **recommend 3.12**.
2. Manifest-callback credential persistence: `.env` write vs OS keyring vs DB-encrypted
   column — **recommend `.env` for S0**, document rotation.
3. Test Postgres: `docker-compose` vs `testcontainers` — **recommend compose** for S0.
4. Baseline definition (§5 Q2) — accept the provisional "previous successful run on branch".

---

## 8. Backend spec track (D1–D6)

Converts the pipeline into a durable, at-least-once, idempotent service. Source:
`JANUS_BACKEND_SPEC-7def8ad9.md.txt`. The triage brain is unchanged and sits behind
`worker.process(job)`.

### 8.1 Scope & non-goals (spec §1–§2)
- **Goal:** handle webhook bursts from many repos with no lost events, no duplicate work,
  bounded cost, and honest observability.
- **Non-goals (state in README):** no microservices/Kafka/Kubernetes/service mesh; two
  deployables only (`api`, `worker`) from one codebase; no exactly-once (at-least-once +
  idempotent effects); one Postgres (no sharding). Stop after D6.

### 8.2 D1 — Ingest + queue
- `deliveries` table + migration; ingest writes delivery + job in **one transaction**.
- Rewrite `POST /webhooks/github`: verify HMAC; `INSERT ... ON CONFLICT DO NOTHING` on
  `delivery_id`; **duplicate → 200** (enqueue nothing), accepted → **202**, ignored → 202;
  **503 + `Retry-After`** when queued jobs exceed the configured cap.
- Expanded `jobs` schema (§1.4); async engine/session/repository.
- Single async worker using the existing `SKIP LOCKED` claim.
- **Accept:** replay the same delivery 100× → exactly one job; 202 path adds no triage work.

### 8.3 D2 — Worker pool + leases
- `locked_by`, `lease_expires_at`; heartbeat every 10 s extends the lease.
- **Reaper** requeues `running` jobs whose lease expired (crashed worker).
- Run N worker processes (same codebase) claiming via `SKIP LOCKED`.
- **Accept:** `kill -9` a worker mid-job → the job completes exactly once with no
  duplicate comment/check.

### 8.4 D3 — Retries, DLQ, backoff
- Classify errors: transient (GitHub 5xx/429, LLM timeout) → retry; permanent → `dead`.
- Backoff with **full jitter**: `run_after = now + rand(0, min(cap, base*2^attempts))`.
- Dead-letter: `state='dead'` + `GET /admin/dead` + `POST /admin/jobs/{id}/requeue`.
- Fault-injecting fake GitHub/LLM (random 500/429/timeouts) for tests.
- **Accept:** all transient faults recover; permanent faults land in `dead` with a reason.

### 8.5 D4 — Fairness, rate limits, budgets
- Claim query prefers the repo with the fewest running jobs; per-repo concurrency cap
  (default 2).
- Redis token-bucket per installation (Lua, atomic) for outbound GitHub calls; honour
  `X-RateLimit-Remaining`/`Retry-After`; 429 → requeue with `run_after` without burning an
  attempt.
- Per-repo **daily budget** for sandbox minutes and LLM tokens; over budget →
  `skipped_budget` + comment (not a failure).
- **Accept:** a 500-job noisy repo does not delay a quiet repo's job beyond a stated
  bound; a 429 storm loses zero jobs.

### 8.6 D5 — Failure dedup
- `fingerprint = sha256(repo_id + workflow + normalized failing test ids + normalized top
  error lines)`; normalization strips timestamps, paths, hashes, line numbers.
- Reuse the cached verdict when an identical fingerprint was triaged within T hours and
  the commit range is unchanged; post a short "same failure as <run>" comment.
- Unique **partial** index `(fingerprint) WHERE state IN ('queued','running')`; metric
  `dedup_hits`.
- **Accept:** 100 identical failures → 1 triage + 99 hits; distinct failures are not
  merged (false-merge rate measured on the BugsInPy subset).

### 8.7 D6 — Observability + load test
- OTel traces (one per delivery; spans ingest/queue-wait/claim/log-fetch/sandbox/LLM/post;
  trace id on the job row).
- Prometheus metrics: `jobs_enqueued`, `jobs_by_state`, `queue_age_seconds`,
  `job_duration_seconds` (by stage), `retries_total`, `dead_total`, `dedup_hits`,
  `github_429_total`, `lease_reaped_total`.
- JSON logs with `delivery_id`/`job_id`/`repo_id`; Grafana dashboard JSON + screenshot.
- Load test (`locust`/`k6`) with mock GitHub + stub triage (log fetch 200–800 ms, LLM
  3–10 s, sandbox 20–60 s); scenarios (a)–(e) from spec §7; results in `bench/RESULTS.md`.
- **Accept:** report shows ingest p50/p95/p99, 202 rate, queue-age p95, time-to-drain,
  throughput vs worker count, **0 duplicated / 0 lost jobs**; measured numbers only.

### 8.8 Data-integrity invariants (write a test for each — spec §5)
1. A delivery id is processed at most once to enqueue.
2. A job is never run concurrently by two workers (lease + `SKIP LOCKED`).
3. A worker crash at any point leaves exactly one visible effect.
4. A retry never posts a duplicate comment/check (output marker).
5. A Redis flush loses no jobs.

### 8.9 Compose topology (spec §8)
`docker compose up` brings up `api`, `worker`, `postgres`, `redis`, `prometheus`,
`grafana`. Two deployables from one image; Redis and Grafana are optional for the
polling-only local path (§9 R5).

### 8.10 Deliverables
- `docs/DESIGN.md` — decisions, tradeoffs, "Known limits", and answers to the spec §10
  interview questions.
- `bench/RESULTS.md` — the load report (bad numbers published with their cause).

### 8.11 Phase ↔ milestone mapping

| Backend milestone | Plan phase |
|---|---|
| D1 Ingest + queue | Phase 2R |
| D2 Worker pool + leases | Phase 2R |
| D3 Retries + DLQ + backoff | Phase 2R |
| D4 Fairness + rate limits + budgets | Phase 2F |
| D5 Dedup | Phase 4D |
| D6 Observability + load test | Phase 7O |

---

## 9. Spec delta & reconciliation decisions

Where the backend spec changes what already exists, these are the resolutions. Each is
also an ADR candidate for `docs/DECISIONS.md` when implemented.

- **R1 — Sync → async DB.** Adopt SQLAlchemy 2 async + `asyncpg` for `api` and `worker`;
  keep a sync `psycopg` engine for Alembic only. Reworks `db/session.py`,
  `db/repository.py`, `jobs/queue.py`, `jobs/worker.py`. *Cause:* spec §8; async I/O suits
  the worker pool and heartbeats. *Cost:* the Phase 2 code is rewritten; tests move to
  testcontainers Postgres.
- **R2 — State vocabulary.** Adopt `queued/running/succeeded/failed/dead` +
  `skipped_budget`; drop the local `claimed` (claim goes straight to `running` with a
  lease) and `comment_only` (comment-only is *behaviour* driven by `diagnoses.route`, not
  a queue state). Route stays in `diagnoses`.
- **R3 — Layout.** Keep the flat `janus/` package (S0 is built); add `jobs/{lease,budgets,
  dedup,ratelimit}.py`, `github/ratelimit.py`, `observability/`, `report/markers.py`,
  `app/api/admin.py`, `bench/`, `docs/DESIGN.md`. Mapping table in §1.3.
- **R4 — Ingest status codes.** Duplicate delivery → **200**; accepted → 202; ignored →
  202; over queue depth → **503 + `Retry-After`**. Replaces S0's "202 for everything" and
  moves idempotency from `runs.delivery_id` to the `deliveries` table.
- **R5 — Redis is optional.** Used for wake-up and rate-limit counters only; the pipeline
  must work with polling and no Redis. A Redis flush must lose no jobs (invariant 5).
- **R6 — Tests use testcontainers.** Anything exercising real DB behaviour runs against a
  containerized Postgres/Redis; pure-unit tests stay DB-free. Replaces the SQLite
  integration approach.
- **R7 — Guardrails compose.** Keep the global kill switch (`PLAN.md` §4); add the spec's
  per-repo concurrency cap, daily budgets, and queue-depth backpressure. Per-job caps
  (Phase 2) become budget accounting, not the primary limiter.
- **R8 — Dedup needs the brain.** D5 depends on parsed failing-test ids and error lines,
  so it is sequenced after Phase 4 (reproduce/triage), not with D1–D3.
- **R9 — Output idempotency is new.** Check runs/comments carry a hidden marker with the
  job's idempotency key; the reporter looks for it and updates instead of duplicating.
  This closes the "crash between post and mark-succeeded" window (invariant 3/4).

---

## 10. Immediate next step

Implement **Phase 2R (D1–D3)** before any triage logic: the `deliveries` table and
ingest rewrite, the async DB migration, the expanded `jobs` schema, leases + reaper, and
the dead-letter path with fault-injection tests. Write `docs/specs/BACKEND.md` first (the
ordered task list + acceptance from §8.2–§8.4). Do not begin Phase 4 (triage) until D1–D3
pass. Do not start any `PLAN.md` §3 deferred item before S2 is demoed.
