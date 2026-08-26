# Janus — Development & Architecture Plan

Source of truth: `PLAN.md` (a.k.a. `JANUS_PLAN.txt`). This document translates the
product plan into an architecture and a phased build order. It does **not** replace
`PLAN.md`; it implements it. Scope is **S0 + S1 + S2** only. No deferred item may be
started before S2 is demoed.

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
   │  Ingress (FastAPI)                           │
   │  manifest flow · webhook verify · enqueue    │
   └───────────────┬─────────────────────────────┘
                   │ jobs (Postgres, SKIP LOCKED)
                   ▼
   ┌─────────────────────────────────────────────┐
   │  Worker / Orchestrator                       │
   │  job state machine · retries · cost caps ·   │
   │  quotas · kill switch · idempotency          │
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
| **Ingress** | GitHub App manifest install, webhook HMAC verification, event filtering, job enqueue | S0 |
| **GitHub client** | App JWT → installation token; fetch run logs, commits/diff, post Checks, PRs, comments | S0/S2 |
| **Persistence** | Postgres schema + repository layer + Alembic migrations | S0 |
| **Worker / orchestrator** | Postgres-backed queue, job state machine, retries/backoff, cost accounting, quotas, kill switch | S0→S2 |
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
  app/            FastAPI entrypoint, config, health
    api/          webhooks.py, manifest.py, health.py
  github/         app_auth.py, client.py, webhook_verify.py, manifest.py
  db/             models.py, session.py, repository.py, migrations/ (alembic)
  jobs/           queue.py, worker.py, state.py, caps.py
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
  report/         check_run.py, comment.py, pr.py
  config/         janus_yml.py
  benchmark/      bugs_in_py.py, metrics.py
tests/            unit / integration / e2e
docs/             IMPLEMENTATION_PLAN.md, specs/S0.md S1.md S2.md
```

Rule from `PLAN.md` §6: the **core stays language-agnostic**; every Python
specific lives behind `core/adapter.py` in `adapters/python/`.

### 1.4 Data model (Postgres, minimal)

| Table | Purpose | Key fields |
|---|---|---|
| `installations` | GitHub App installs | installation_id, account, app_id, scopes |
| `repositories` | tracked repos + config | repo_id, owner/name, default_branch, `.janus.yml` cache |
| `runs` | observed workflow runs | run_id, repo_id, head_sha, conclusion, baseline_sha (last-green) |
| `jobs` | work queue | id, run_id, status, attempts, cost_minutes, llm_tokens, timestamps |
| `reproductions` | rerun evidence | job_id, profile, per-run result, exit code, telemetry JSON |
| `signals` | scored evidence | job_id, signal_name, raw, normalized, weight |
| `diagnoses` | triage outcome | job_id, route, confidence, top2_gap, evidence JSON |
| `suspects` | SBFL/bisect output | job_id, file, line, score, introducing_commit |
| `fixes` | generated diffs | job_id, suspect_id, diff, attempt, status |
| `gate_results` | proof outcomes | fix_id, gate, passed, measured_value, baseline_value |
| `benchmark_runs` | BugsInPy harness | bug_id, predicted_route, actual, fix_rate, false_fix, t2g |

Alembic owns all schema changes. `jobs` uses `SELECT ... FOR UPDATE SKIP LOCKED`
for safe concurrent claiming.

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

### Phase 2 — Orchestration core
**Build:** job state machine, Postgres queue with `SKIP LOCKED`, worker loop,
retry/backoff, idempotency keys; cost accounting (sandbox minutes, LLM tokens, fix
retries); per-repo quotas; global kill switch.
**Acceptance:** two workers never double-process a job; exceeding a cap routes to
comment-only; kill switch stops claiming immediately; retries are bounded.

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
- **Observability:** structured logs, `jobs`/`diagnoses`/`gate_results` rows, cost
  accounting per failure.
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
- **Queue vs. Temporal:** intentionally a ~50-line Postgres job table; revisit only at
  scale.

---

## 5. Open questions (to resolve at the relevant phase, not blockers)

1. Which workflow events are authoritative — `workflow_run` (Actions) or `check_run`
   (generic)? Plan implies Actions (`workflow_run`) with Check-run output. *(Phase 1)*
2. Exact "last-green → first-red" baseline: previous successful run on the branch vs.
   merge-base with default branch. *(Phase 1)*
3. Which BugsInPy repos/bugs make the 25–30 subset. *(Phase 8)*
4. Concrete default cost caps (sandbox minutes, LLM tokens, fix retries). *(Phase 2)*

---

## 6. Immediate next step

Write `docs/specs/S0.md` with the acceptance tests above, then implement **Phase 0 →
Phase 1**. Do not start any deferred item (`PLAN.md` §3) before S2 is demoed.

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
| DB | Postgres 16 via `docker-compose.yml`; SQLAlchemy 2.0 (sync) + `psycopg[binary]`; Alembic | `SKIP LOCKED` works on sync; simplest for the ~50-line queue. |
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
