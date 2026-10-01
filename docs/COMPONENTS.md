# Janus — Component Reference

How each component works, why it is shaped that way, and how the pieces flow into one
another. For the first-principles framing see [`ARCHITECTURE.md`](ARCHITECTURE.md); for
the reasoning behind individual choices see [`DECISIONS.md`](DECISIONS.md). This document
is the "working manual": it explains behaviour, not just structure.

Scope: **S0 (spine) + Phase 2 (orchestration)**. Everything under `janus/core/`,
`janus/sandbox/`, `janus/adapters/` and the fix surfaces is still a stub.

---

## 1. Request lifecycle at a glance

There are two entry points into the system and one background loop:

```
                       ┌──────────────────────────────────────────────┐
 GitHub red build ───▶ │ POST /webhooks/github   (ingress, fast)      │
                       │  verify HMAC → persist → enqueue → Check run │
                       └───────────────────────┬──────────────────────┘
                                               │ jobs row (status=queued)
                                               ▼
                       ┌──────────────────────────────────────────────┐
                       │ Worker loop   (slow, guarded)                │
                       │  kill switch → claim → quota → process →     │
                       │  caps/retry → commit                         │
                       └──────────────────────────────────────────────┘

 Browser ───────────▶ GET /manifest/start ─▶ GitHub ─▶ /manifest/callback
                                                         (persist credentials)
```

The split is deliberate: ingress must answer GitHub within seconds and never blocks; the
worker does the slow, expensive, failure-prone work behind cost caps. They communicate
only through the `jobs` table, so neither needs to know the other is running.

---

## 2. `janus/app` — application shell

### `config.py` — Settings
`Settings(BaseSettings)` reads `JANUS_`-prefixed env vars (or `.env`). It holds three
groups: infrastructure (`database_url`, `log_level`, `public_url`), GitHub App
credentials (filled by the manifest flow), and orchestrator guardrails (retry/cost caps,
quota, worker cadence, `kill_switch`). `get_settings()` is `lru_cache`d, so it is one
object per process; tests clear the cache when they patch the environment.

**Why a single object:** every component reads the same validated values. Nothing parses
`os.environ` ad hoc, so a typo is a startup error rather than a runtime surprise.

### `logging.py` — structured logs
`configure_logging(level)` sets stdlib logging plus `structlog` to emit one JSON object
per line. JSON (not text) because logs are meant to be shipped and queried, and because
per-job context can be attached without a parser.

### `main.py` — app factory
`create_app()` builds the FastAPI app; a `lifespan` context configures logging and
constructs the DB engine on startup and disposes it on shutdown. A factory (rather than a
module-level `app`) keeps import side effects out of tests — each test can build an
isolated app and override dependencies.

### `api/health.py` — liveness
`GET /health` returns `{"status": "ok"}` and touches nothing. Liveness must stay green
even when a dependency (the database, GitHub) is degraded; conflating the two would make
an orchestrator kill a healthy process during a DB blip.

### `api/webhooks.py` — ingress (the core of S0)
`POST /webhooks/github`, in order:

1. **Verify** the HMAC over the *raw* body (`github/webhook_verify.py`). Re-serialising
   the JSON would change whitespace and break the digest, so the endpoint reads raw bytes
   first. A bad signature returns **401 before anything is persisted**.
2. **Filter**: only `workflow_run` with `action == "completed"` and a failure conclusion
   (`failure`/`timed_out`/`startup_failure`) is actionable. `check_run` events are
   acknowledged (202) but ignored — Janus posts Check runs itself, so reacting would loop.
3. **Off-load**: the endpoint is `async` (to read the body) but the DB and `httpx` are
   synchronous, so the real work runs via `run_in_threadpool`. The event loop is never
   blocked and GitHub gets a fast 202.
4. **Persist**: `upsert_installation`, `upsert_repository`, then `get_or_create_run`.
5. **Idempotency**: `runs.delivery_id` (the `X-GitHub-Delivery` header) is unique. A
   redelivered webhook returns the existing run and skips every side effect.
6. **Baseline**: `resolve_baseline` finds the last-green commit for the fault diff.
7. **Enqueue**: `jobs.queue.enqueue` creates the queued job (idempotent on the run).
8. **Report**: `report.check_run.start_check_run` posts an `in_progress` Check run.
9. One transaction is committed at the end.

**Why this order:** verification and idempotency come before any external call, so a
forged or duplicated delivery can never cause work. The Check run is the last step
because it is the only externally visible claim, and it should only happen once the run
and job are durably recorded.

### `api/manifest.py` — one-click App install
`GET /manifest/start` renders a self-submitting HTML form (GitHub's manifest endpoint
needs the manifest as a **POST** field, which a redirect cannot carry). It also sets a
random `state` nonce in an HttpOnly, SameSite=Lax cookie. `GET /manifest/callback`
rejects the request with **400** unless the `state` GitHub echoes back matches that
cookie — closing the CSRF gap — before fetching or writing any credential. On success it
persists credentials and redirects to `/manifest/success`.

---

## 3. `janus/github` — talking to GitHub

### `webhook_verify.py` — `verify_signature(secret, body, header)`
Computes a constant-time HMAC-SHA256 of the raw body and compares it to
`X-Hub-Signature-256` (`sha256=<hex>`). It returns a bool and **never raises** — a missing,
malformed or mismatched header is simply `False`. Constant-time comparison prevents a
timing side channel from leaking the expected digest.

### `app_auth.py` — App identity → installation token
Two steps, by design. `app_jwt()` signs an RS256 JWT with the App's private key, valid
~9 minutes (under GitHub's 10-minute cap). `installation_token()` exchanges it for a
scoped installation token and caches it in memory with a 60-second refresh skew. Tokens
are **never** placed in clone URLs (a token in a URL leaks into logs and process lists).

**Why cache:** installation tokens are the hot path for every API call; minting one per
call would double the request count and add latency.

### `client.py` — REST wrapper
`GitHubClient` has one method per endpoint S0 needs (workflow run, run logs, commits,
compare, check-run create, issue comment). HTTP is injectable, so tests drive it with
`respx` or a mock transport. It is deliberately thin — no retry/business logic — so the
core decides *when* to call, and the client only knows *how*.

### `baseline.py` — last-green → first-red
`select_baseline_sha(runs, head_sha)` picks the most recent completed `success` on the
branch that is not the failing head (GitHub returns runs newest-first). `resolve_baseline`
then **confirms with the compare API** that the candidate is an ancestor of the head:
`ahead`/`identical` is accepted; `diverged`/`behind` (force-push or rebase) yields `None`
so the diff is never computed from an unrelated base. A failed compare is non-fatal and
keeps the candidate (losing the diff is worse than a best-effort base).

### `manifest.py` — App manifest & credentials
`build_manifest` emits the least-privilege App definition (Contents/PRs/Checks RW,
Actions/Metadata read; events `workflow_run`, `check_run`). `exchange_manifest_code`
trades the callback code for credentials. `persist_credentials` writes the PEM to disk and
records the non-secret values in `.env`, preserving comments and updating keys in place.

---

## 4. `janus/db` — persistence

### `models.py` — schema
Five tables. GitHub's natural ids are the primary keys (`installation_id`, `repo_id`,
`run_id`) because they are globally unique and stable, so they double as idempotency keys
and remove a mapping layer.

- `installations`, `repositories`, `runs` (with `baseline_sha`, unique `delivery_id`).
- `jobs`: `status`, `attempts`, `cost_minutes`, `llm_tokens`, unique `idempotency_key`,
  `run_after` (backoff gate), `last_error`.
- `diagnoses`: triage outcome (route/confidence/evidence) — populated in S1.
- `system_flags`: runtime toggles (the kill switch).

A deterministic `naming_convention` keeps Alembic autogenerate stable. Natural-id PKs are
`autoincrement=False` so Postgres does not create unused sequences.

### `session.py` — engine & sessions
The engine is created **lazily** (importing never opens a connection), with
`pool_pre_ping` to drop dead connections. `SessionLocal()` is the factory; `get_session()`
is the FastAPI dependency that always closes the session.

### `repository.py` — typed CRUD
Upserts and inserts that **flush but never commit** — the caller owns the transaction.
This is what lets one webhook delivery be one atomic unit and keeps idempotency decisions
in a single place.

### `migrations/` — Alembic
Schema changes are migrations only. `env.py` takes the URL from `Settings` (never
hardcoded) and points `target_metadata` at `Base.metadata`. Post-write hooks run ruff on
newly generated revisions so they are always lint-clean.

---

## 5. `janus/jobs` — the orchestrator (Phase 2)

### `state.py` — the state machine
`JobStatus` is a `StrEnum`. The legal transitions are encoded as data:

```
QUEUED ─▶ CLAIMED ─▶ RUNNING ─▶ SUCCEEDED | FAILED | COMMENT_ONLY   (terminal)
   ▲          │          │
   └──────────┴──────────┴──── QUEUED        (release / retry with backoff)
```

`can_transition` / `assert_transition` make an illegal move a testable bug rather than
silent queue corruption. Terminal states have no exits.

### `caps.py` — guardrails
Four mechanisms answering one question — *"may this job keep going?"*:

- **`cap_exceeded`** bounds a single job by sandbox minutes and LLM tokens. Exceeding one
  is terminal `COMMENT_ONLY`: report what we have, spend no more.
- **`repo_quota_exceeded`** bounds a repository (jobs per rolling window) so one noisy
  repo cannot starve others. The count includes the job being claimed, so exactly
  `max_jobs` are allowed.
- **`is/set_kill_switch`** store the global stop in `system_flags`; because it is read on
  every claim, flipping it takes effect immediately without a redeploy. `Settings.kill_switch`
  is a hard env-level override.
- **`backoff_seconds`** gives exponential retry spacing (`base * 2**(attempts-1)`).

`max_fix_retries` is carried here for the S2 fix loop; Phase 2 does not consume it yet.

### `queue.py` — the Postgres queue
- `enqueue(session, run_id, idempotency_key=...)` dedupes on the key (default
  `run:{run_id}`), returning the existing job if present.
- `claim_next(session, now=...)` runs
  `SELECT … WHERE status='queued' AND (run_after IS NULL OR run_after <= now)
  ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED`, then sets `CLAIMED` and increments
  `attempts`. It **flushes but does not commit**.

**Why SKIP LOCKED:** each worker locks the oldest eligible row and skips rows other
workers have locked, so N workers are safe with no external lock service — the database
*is* the queue. Flushing without committing means a crash mid-processing rolls the claim
back and the job returns to `queued` on its own.

### `worker.py` — the loop
`Worker.run_once()` performs, in order:

1. **Kill switch** (settings or DB) → return without claiming.
2. **Claim** the next eligible job (`QUEUED → CLAIMED → RUNNING`).
3. **Quota**: over the per-repo limit → `COMMENT_ONLY`, commit, done.
4. **Process** via the pluggable `JobProcessor`; any exception becomes a retryable
   failure, so a buggy processor cannot wedge the queue.
5. **Apply** cost accounting, then: cap exceeded → `COMMENT_ONLY`; retryable and under
   `max_attempts` → back to `QUEUED` with `run_after = now + backoff`; else the terminal
   status the processor asked for.
6. **Commit**.

`run()` polls until `max_iterations` (tests) or forever (daemon, `python -m
janus.jobs.worker`). The default processor is a Phase 2 no-op; the real
reproduce→triage→fix pipeline slots in behind the same interface.

**Why the processor is a callable:** the *mechanics* (claiming, retries, caps, quotas,
kill switch) are stable and testable; the *method* is what changes between milestones.
Separating them means Phase 2 is provably correct on its own and S1/S2 plug in without
touching the loop.

---

## 6. `janus/report` — surfaces

`check_run.start_check_run` posts an `in_progress` Check run acknowledging a red build. It
is the only external claim S0 makes, and it proves the webhook → Check path before any
triage exists. Later phases replace the summary with real evidence and add comment/PR
surfaces.

---

## 7. Cross-cutting behaviours

- **Idempotency, twice.** Webhook redeliveries are deduped by `runs.delivery_id`; job
  re-enqueues by `jobs.idempotency_key`. Both are unique constraints, so the guarantee is
  enforced by the database, not by application logic alone.
- **Transactions.** One webhook delivery = one transaction; one claim+process = one
  transaction. Repositories flush, callers commit.
- **Failure direction.** Every stage degrades *safe*: bad signature → 401 and no work;
  over a cap/quota → `COMMENT_ONLY`; processor exception → bounded retry then `FAILED`;
  unknown baseline → `None`. Nothing guesses.
- **Observability.** Structured logs plus the `runs`/`jobs`/`diagnoses` rows record what
  happened and why, which is also what makes "no claim without a gate" possible later.

---

## 8. Still stubbed (not part of S0/Phase 2)

`janus/core/**` (reproducer, signals, triage, localize, retrieve, generate, prove,
adapter), `janus/sandbox/**`, `janus/adapters/python/**`, `janus/report/{comment,pr}.py`,
`janus/config/janus_yml.py`, `janus/benchmark/**`. The worker is wired to call them; they
are empty until their phases.
