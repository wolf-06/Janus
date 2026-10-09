# Janus — Component Reference

How each component works, why it is shaped that way, and how the pieces flow into one
another. For the first-principles framing see [`ARCHITECTURE.md`](ARCHITECTURE.md); for
the reasoning behind individual choices see [`DECISIONS.md`](DECISIONS.md). This document
is the "working manual": it explains behaviour, not just structure.

Scope: **S0 (spine) + Phase 2 (orchestration) + the Phase 3 sandbox core**. Everything under
`janus/core/`, `janus/adapters/` and the fix surfaces is still a stub.

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

### `api/webhooks.py` — ingress
`POST /webhooks/github`, in order:

1. **Verify** the HMAC over the *raw* body (`github/webhook_verify.py`). Re-serialising
   the JSON would change whitespace and break the digest, so the endpoint reads raw bytes
   first. A bad signature returns **401 before anything is persisted**.
2. **Filter**: only `workflow_run` with `action == "completed"` and a failure conclusion
   (`failure`/`timed_out`/`startup_failure`) is actionable. `check_run` events are
   acknowledged (202) but ignored — Janus posts Check runs itself, so reacting would loop.
3. **Backpressure**: if queued depth ≥ `JANUS_QUEUE_DEPTH_LIMIT`, return **503 +
   `Retry-After`** so GitHub redelivers instead of the queue growing unbounded.
4. **One transaction**: `record_delivery` (`INSERT ... ON CONFLICT DO NOTHING` on
   `deliveries`), then `upsert_installation` / `upsert_repository`, `get_or_create_run`,
   and `enqueue`. A duplicate delivery → **200** and nothing enqueued; accepted → **202**.
5. **After the commit (best-effort)**: resolve the baseline and post the acknowledgement
   Check run. These call the **sync** GitHub SDK via `run_in_threadpool` (the DB layer is
   async), and any failure is logged, never fatal to ingest.

**Why this order:** verification, backpressure and idempotency come before any external
call, so a forged or duplicated delivery can never cause work; the job is durably enqueued
before any GitHub call, so a GitHub outage cannot lose the event.

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
GitHub's natural ids are primary keys where they are globally unique and stable
(`installation_id`, `repo_id`, `run_id`); internal tables use surrogate ids.

- `deliveries` (`delivery_id` PK, repo_id, event, received_at) — **ingest idempotency**.
- `installations`, `repositories`, `runs` (with `baseline_sha`).
- `jobs`: `delivery_id` (unique), `repo_id`, `run_id`, `fingerprint`, `state`, `attempts`,
  `max_attempts`, `run_after`, `locked_by`, `lease_expires_at`, `last_error`,
  `cost_minutes`, `llm_tokens`, `created_at`/`updated_at`/`finished_at`.
- `diagnoses`: triage outcome (route/confidence/evidence) — populated in S1.
- `system_flags`: runtime toggles (the kill switch).

A deterministic `naming_convention` keeps Alembic autogenerate stable. Natural-id PKs are
`autoincrement=False` so Postgres does not create unused sequences. `jobs` carries a
**partial unique index** on `fingerprint` where `state IN ('queued','running')` (used by
dedup, D5).

### `session.py` — async engine & sessions
`create_async_engine` (asyncpg) is created **lazily** (importing never opens a connection)
with `pool_pre_ping`. `AsyncSessionLocal()` is the factory; `get_session()` is the async
FastAPI dependency that always closes the session. Alembic uses a sync `psycopg` URL
derived from the same setting.

### `repository.py` — typed async CRUD
Upserts and inserts that **flush but never commit** — the caller owns the transaction.
`record_delivery` uses dialect-specific `INSERT ... ON CONFLICT DO NOTHING` and returns
whether the row was newly inserted; that boolean is the ingest idempotency decision.

### `migrations/` — Alembic
Schema changes are migrations only. `env.py` takes the URL from `Settings` (never
hardcoded) and points `target_metadata` at `Base.metadata`. Post-write hooks run ruff on
newly generated revisions so they are always lint-clean.

---

## 5. `janus/jobs` — the orchestrator

### `state.py` — the state machine
`JobStatus` is a `StrEnum`. The legal transitions are encoded as data:

```
QUEUED ─▶ RUNNING ─▶ SUCCEEDED | FAILED | DEAD | SKIPPED_BUDGET   (terminal)
              │
              └────▶ QUEUED        (retry with backoff, or lease reaped)
```

There is no `claimed` state: a claim moves `queued → running` and stamps a lease in the
same statement. `comment_only` is not a state — whether a run is comment-only is behaviour
driven by the triage route. `can_transition` / `assert_transition` make an illegal move a
testable bug; terminal states have no exits.

### `caps.py` — guardrails
Four mechanisms answering one question — *"may this job keep going?"*:

- **`cap_exceeded`** bounds a single job by sandbox minutes and LLM tokens. Exceeding one
  is terminal `skipped_budget`: report what we have, spend no more.
- **`repo_quota_exceeded`** bounds a repository (jobs per rolling window) so one noisy repo
  cannot starve others. The count includes the job being claimed, so exactly `max_jobs`
  are allowed. (D4 adds the fairness/concurrency-cap form.)
- **`is/set_kill_switch`** store the global stop in `system_flags`; read on every claim, so
  flipping it takes effect immediately without a redeploy. `Settings.kill_switch` is a hard
  env-level override.
- **`backoff_seconds`** gives **full-jitter** retry spacing:
  `rand(0, min(cap, base·2^attempts))`.

`max_fix_retries` is carried here for the S2 fix loop.

### `queue.py` — the Postgres queue
- `enqueue(session, delivery_id, repo_id, run_id, ...)` dedupes on `delivery_id` (one job
  per delivery), returning the existing job if present.
- `claim_next(session, worker_id, now, lease_seconds)` runs
  `SELECT … WHERE state='queued' AND (run_after IS NULL OR run_after <= now)
  ORDER BY run_after NULLS FIRST, id LIMIT 1 FOR UPDATE SKIP LOCKED`, then sets `running`,
  increments `attempts`, and stamps `locked_by` + `lease_expires_at`. It **flushes but
  does not commit**.
- `queued_depth(session)` supports ingest backpressure.

**Why SKIP LOCKED:** each worker locks the oldest eligible row and skips rows other workers
have locked, so N workers are safe with no external lock service — the database *is* the
queue.

### `lease.py` — heartbeat and reaper
A claim stamps a lease. `heartbeat` extends it if this worker still owns the job; a
background task calls it every few seconds during processing. `reap_expired` returns
running jobs whose lease expired to `queued` — the crash-recovery mechanism for `kill -9`
(and it does **not** consume an attempt). `SKIP LOCKED` keeps two reapers from
double-requeuing.

### `worker.py` — the loop and transaction discipline
`Worker.run_once()`:

1. **Claim** in a short transaction: kill switch check, then `claim_next`, then **commit**.
2. **Quota**: over the per-repo limit → `skipped_budget`, done.
3. **Process** via the pluggable async `JobProcessor` with **no open transaction**; a
   background task heartbeats the lease. Any exception becomes a retryable failure.
4. **Finalize** in another short transaction: add cost/tokens; cap exceeded →
   `skipped_budget`; success → `succeeded`; retryable and under `max_attempts` → `queued`
   with a jittered `run_after`; otherwise → `dead`.

`run()` reaps, then polls until `max_iterations` (tests) or forever (daemon,
`python -m janus.jobs.worker`).

**Why processing runs outside a transaction:** holding a transaction open across long work
would lock the row and block heartbeats; instead the lease carries ownership, so a crash is
recovered by the reaper rather than by a rollback that never happens.

**Why the processor is a callable:** the *mechanics* (claiming, leases, retries, caps,
quotas, kill switch) are stable and testable; the *method* is what changes between
milestones. The triage brain plugs in at S1/S2 without touching the loop.

---

## 6. `janus/report` — surfaces

`check_run.start_check_run` posts an `in_progress` Check run acknowledging a red build. It
is the only external claim S0 makes, and it proves the webhook → Check path before any
triage exists. Later phases replace the summary with real evidence and add comment/PR
surfaces.

---

## 7. `janus/sandbox` — the container

Runs one command in an ephemeral, locked-down container and returns facts about it. The
runner knows nothing about pytest: the adapter decides the command, the sandbox just runs
it. Configuration comes from `janus.toml` (see `DECISIONS.md` D-046).

### Pipeline
One run, end to end:

1. **Build image** — resolve the per-run image and build it when it is not `prebuilt`; the tag
   is content-addressed from the Dockerfile, base image, and build-context manifests
   (`image.py`, ADR D-056).
2. **Two-phase run** — `RunPlan` yields a setup phase (network on) and an offline test phase;
   the container is created with dropped capabilities and resource limits, the source is
   copied in with `docker cp`, and, when `allow_hosts` is set, the container joins an internal
   network whose only route out is the allowlisting proxy (`proxy.py`, ADR D-059).
3. **Streamed bounded capture** — stdout/stderr are bounded at `max_output_bytes` as they
   arrive, and the stream keeps draining so the command cannot block on a full pipe (D-058).
4. **Redaction** — the bounded output is redacted before it leaves the sandbox, the container
   is removed, and the per-run image is removed when this run built it.

### `settings.py` — `SandboxSettings`
Image, work dir, run user, wall-clock timeout, resource limits, network policy, output cap,
egress-proxy limits (`proxy_port`, `proxy_memory_mb`, `proxy_pids_limit`), and the
setup/test commands. Validators reject blank names, a relative `workdir`, non-positive
limits, and a `proxy_port` outside 1..65535. A bad config fails at startup rather than
mid-run.

### `spec.py` — `SandboxSpec` / `SandboxResult`
`SandboxSpec` is a frozen description of one run (image, command, limits, network, phase,
pull policy, output cap); `build_spec` fills it from settings, defaults the network to the
*phase's* intent (setup may use it, tests never do), and forces it off when the
configuration forbids it. `SandboxResult` carries the exit code, stdout/stderr, duration,
telemetry, timeout/OOM flags, and an `error` for infrastructure failures. Both are plain
data, so they are cheap to log and test without Docker.

### `image.py` — per-run image
`resolve_image` picks the source: `prebuilt` uses `image` as-is; `repo` requires a root
Dockerfile; `generated` writes a Janus Dockerfile; `auto` prefers a repo Dockerfile and
generates otherwise. A generated image is built from a temp context holding only the manifest
files it `COPY`s (never the source), and the tag is content-addressed from the Dockerfile,
base image, and those manifests, so identical inputs reuse a tag. The build runs on a worker
thread and is abandoned after `build_timeout_seconds` (best-effort). The caller removes the
context after the build and the image after the run; a `prebuilt` image is never removed
(ADR D-056).

### `allowlist.py` — `NetworkPolicy`
No network by default. When enabled, an empty host list means "any host" (the install
phase); a non-empty list is *enforced* (`enforced`) by the egress proxy. `host_allowed()`
owns the matching rule (exact, case-insensitive, trailing-dot-insensitive, leading-dot
wildcard, empty = any). `docker_network_mode()` is the single mapping from "network allowed"
to a Docker network mode, used by both `NetworkPolicy` and `SandboxSpec`.

### `proxy.py` — `ProxySession` / allowlisting proxy
Implements host-list enforcement (ADR D-059): two run-owned networks (an `internal=True` one
for the sandbox + proxy, a normal bridge for the proxy's upstream egress), a standard-library
asyncio proxy that allows plain HTTP and `CONNECT` only to listed hosts and answers `403`
otherwise, and teardown of sandbox → proxy → networks. The proxy image is shared and
content-addressed (`proxy_image_tag`), built once and never removed; the per-run host list is
passed via `JANUS_PROXY_ALLOW_HOSTS`.

### `redaction.py` — `redact` / `redact_bytes`
Six checks, cheapest and most certain first: PEM blocks; known token shapes (GitHub, GitLab,
AWS, Slack, Stripe, Google, npm, HuggingFace, Docker, PyPI, SSH key material, JWTs,
`user:pass@host`); `Authorization: Bearer`/`Basic` credentials (the scheme word is kept);
`KEY=value` lines whose key looks secret-ish (including quoted JSON keys); long high-entropy
strings; and high-entropy path *segments*, which redact a random-looking segment in place
while the path structure survives. Filesystem paths are deliberately preserved — they are
evidence the triage stage needs. Everything leaving the sandbox passes through it.
Best-effort by design.

### `profiles.py` — `RunPlan`
Builds the ordered phases for a job. `clean` is a fresh, repeatable environment; `ambient`
attempts to match the failed CI run and records `ambient_reconstructed` when it cannot.
Phase A (setup) is included only when the configuration allows network access **and** a
setup command exists — otherwise the plan goes straight to the offline test phase. A
disabled sandbox yields an empty plan (`plan.disabled`), which is the caller's signal to
skip the container.

### `telemetry.py` — `StatsSampler`
Polls an injected callable on a background thread and keeps the peak memory and cumulative
CPU seconds. Docker's stats API returns nothing after exit, so sampling while the container
runs is the reliable option; the injected callable keeps it Docker-free and testable.

### `runner.py` — `SandboxRunner`
`resolve_image`/`build_image` (per-run, D-056) → `create` (via the pure `container_options`)
→ `start` → stream the source in with `put_archive` from a spooled temp file, entries owned
by the run user → stream the command on a worker thread, bounding each stream at
`max_output_bytes` while draining the rest (D-058) → kill on timeout → redact → `remove`.
When `allow_hosts` is set the container joins a `ProxySession`'s internal network instead of a
bridge. The client comes from `docker_host` when set, else `docker.from_env()`, and
`require_rootless` fails closed with `error="not_rootless"` before any container exists
(D-057). The root keepalive creates and `chmod 0777`s the work dir, because capabilities are
dropped and a runtime `chown` cannot work (D-054). No host bind mounts and never the Docker
socket. Every failure mode returns a `SandboxResult` with an `error`, never raises; event-loop
callers use `run_in_thread`.

---

## 8. Cross-cutting behaviours

- **Idempotency.** Webhook redeliveries are deduped by the `deliveries` PK; a job is
  one-per-delivery (`jobs.delivery_id` unique). Both are database constraints, not
  application checks.
- **Transactions.** Ingest is one transaction (delivery + run + job). In the worker, a
  claim and the finalization are each short transactions and processing runs between them
  with no open transaction.
- **Failure direction.** Every stage degrades *safe*: bad signature → 401 and no work;
  saturated queue → 503 and GitHub redelivers; over a cap/quota → `skipped_budget`;
  processor exception → bounded retry then `dead`; unknown baseline → `None`. Nothing
  guesses.
- **Crash safety.** Ownership is a lease, not a lock: a crashed worker's job is reaped
  back to `queued`. See `docs/specs/BACKEND.md` §3.
- **Observability.** Structured logs plus the `runs`/`jobs`/`diagnoses` rows record what
  happened and why, which is also what makes "no claim without a gate" possible later.

---

## 9. Still stubbed / deferred

The triage brain and the rest of the backend track. See [`FUTURE.md`](FUTURE.md) for the
full ledger. In short:

- **Product brain (S1):** `janus/core/**` (reproducer, signals, triage), the adapter
  Protocol (`janus/core/adapter.py`), `janus/adapters/python/**`, `janus/report/comment.py`,
  `janus/config/janus_yml.py`, and wiring the sandbox into `JobProcessor`.
- **Fix loop (S2):** `janus/core/{localize,retrieve,generate,prove}`, `janus/report/pr.py`.
- **Backend D4–D6:** fairness/rate limits/budgets (`janus/jobs/{ratelimit,budgets}.py`),
  dedup, observability (`janus/observability/`), load test (`bench/`), Redis, and
  `docs/DESIGN.md`.
- **Output idempotency marker:** `janus/report/markers.py` (needed before the brain posts
  comments/checks).

The worker is wired to call the brain via `JobProcessor`; these modules are empty until
their phases.
