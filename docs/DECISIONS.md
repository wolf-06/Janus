# Janus — Decision Log

An append-only record of *what* we chose, *what we rejected*, and *why*, so the reasoning
survives longer than the code. Each entry is an ADR: **Context → Decision → Alternatives
→ Consequences**. Reversible choices are marked; keep entries small and add new ones
rather than rewriting old ones.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the first-principles view the decisions
serve.

---

## D-001 — Environment & dependency management: `uv` + `.venv`

**Context.** The project needs a reproducible Python environment and lockfile. The plan
recommends `uv`; the working agreement requires an actual virtualenv.

**Decision.** Use `uv` to manage a project-local `.venv` (`uv sync`), with
`pyproject.toml` as the source of metadata and `uv.lock` committed.

**Alternatives.**
- *pip + venv + requirements.txt* — universally available, but no lockfile by default and
  slow resolution; version drift across machines.
- *Poetry* — good lockfile and UX, but an extra tool to install and a non-standard
  `pyproject` layout; slower resolver.
- *conda* — heavyweight, aimed at native/scientific stacks.

**Consequences.**
- (+) One tool creates the venv, resolves, locks, and runs commands; `uv sync` is fast.
- (+) `.venv` is a normal virtualenv, satisfying the "use venv" requirement; `uv run`
  always targets it.
- (−) Contributors must install `uv`. Mitigated: CI installs it via
  `astral-sh/setup-uv`.
- *Reversible:* yes — `pyproject.toml` is standard; swapping the runner is cheap.

---

## D-002 — Pin Python 3.12

**Context.** The host default is Python 3.14. The plan pins 3.12 as the safe floor.

**Decision.** `requires-python = ">=3.12"` and `.python-version` = `3.12`; `uv` fetches
and uses a managed 3.12 interpreter.

**Alternatives.**
- *Use system 3.14* — newest features, but third-party wheels (Docker SDK, SQLAlchemy,
  mutmut, and especially the S1 sandbox toolchain) lag and may not have binary wheels
  yet, causing source builds or outright failures.
- *Pin 3.11* — even safer, but 3.12 is already widely supported and gives newer typing
  ergonomics.

**Consequences.**
- (+) Predictable builds; matches the toolchain the plan was written against.
- (−) Two Pythons exist on the host (system + uv-managed). `uv run` hides this.
- *Reversible:* yes — bump the pin when the ecosystem catches up.

---

## D-003 — Flat package layout (`janus/` at repo root)

**Context.** `IMPLEMENTATION_PLAN.md` §1.3 specifies a `janus/` package at the root.

**Decision.** Use the flat layout exactly as specified; every package gets an
`__init__.py`; modules are created as empty stubs and filled per phase.

**Alternatives.**
- *`src/janus/` layout* — prevents accidentally importing the working tree instead of the
  installed package, but adds a path indirection and diverges from the plan.
- *Single module* — would not survive the number of components.

**Consequences.**
- (+) Matches the plan; imports read as `janus.core...`, which maps to the architecture.
- (+) Empty stubs make the target structure legible before logic exists.
- (−) Flat layout can shadow installed packages if a stray name matches; mitigated by the
  package being installed editable via hatchling.
- *Reversible:* yes, but churny; decide now, keep it.

---

## D-004 — Configuration via `pydantic-settings` (`JANUS_` prefix)

**Context.** Infra config must come from the environment/`.env`; per-repo behaviour
(`.janus.yml`) arrives later.

**Decision.** A single `Settings(BaseSettings)` with `env_prefix="JANUS_"`,
`env_file=".env"`, and a cached `get_settings()`.

**Alternatives.**
- *Plain `os.environ` reads* — no validation, no types, easy to typo silently.
- *Dynaconf / custom loader* — more power than needed for S0.

**Consequences.**
- (+) Typed, validated config; `github_app_id` coerces to `int`; missing GitHub fields are
  optional until the manifest flow fills them.
- (+) The prefix namespaces Janus vars and avoids collisions with e.g. `DATABASE_URL`
  from other tools.
- (−) `get_settings` is `lru_cache`d, so tests must clear the cache when patching env
  (handled by an autouse fixture).
- *Reversible:* yes.

---

## D-005 — Structured JSON logging via `structlog`

**Context.** The plan requires structured logs for observability.

**Decision.** `configure_logging(level)` renders one JSON object per line with ISO UTC
timestamps and level.

**Alternatives.**
- *stdlib logging with a text format* — simple, but not machine-parseable.
- *`python-json-logger`* — similar result, less ergonomic context binding.

**Consequences.**
- (+) Logs are directly queryable; context vars allow per-job correlation later.
- (−) Human-reading raw JSON is less pleasant; can add a console renderer in dev later.
- *Reversible:* yes.

---

## D-006 — Synchronous SQLAlchemy 2.0 with `psycopg` (v3)

**Context.** The queue needs `SELECT ... FOR UPDATE SKIP LOCKED`, which Postgres provides.
S0 also has an async web framework (FastAPI).

**Decision.** Use SQLAlchemy 2.0 in **sync** mode with the `psycopg` v3 driver, a lazily
created engine (`pool_pre_ping=True`), a `SessionLocal` factory and a `get_session()`
dependency.

**Alternatives.**
- *Async SQLAlchemy (`asyncpg`)* — fits FastAPI's event loop, but complicates the worker
  loop and transactions for little gain at this scale; `SKIP LOCKED` works identically.
- *SQLModel* — nice DX, but couples models to Pydantic and constrains Alembic control.

**Consequences.**
- (+) Simpler mental model for the ~50-line queue and the worker.
- (+) Lazy engine means importing the app never opens a connection — tests and tooling
  stay fast.
- (−) Blocking DB calls inside async endpoints must run in a threadpool; acceptable for
  S0's low write volume. Revisit only if concurrency demands it.
- *Reversible:* yes — the repository layer isolates the ORM.

---

## D-007 — Migrations with Alembic, URL from Settings

**Context.** Schema must be versioned, and the plan forbids hardcoding the DB URL.

**Decision.** Alembic owns schema changes. `alembic.ini` leaves `sqlalchemy.url` blank;
`env.py` sets it from `Settings.database_url` and points `target_metadata` at
`Base.metadata` with `compare_type=True`.

**Alternatives.**
- *`Base.metadata.create_all()`* — fine for tests, but no upgrade path and no reviewable
  history; rejected for the real schema.
- *Hardcode the URL in `alembic.ini`* — leaks credentials and forks the source of truth.

**Consequences.**
- (+) Single source of truth for the URL; autogenerate is available.
- (+) Migration history is reviewable and reversible.
- (−) Every schema change is a migration to write/review; small overhead, worth it.
- *Reversible:* no — once migrations exist they are the schema's history.

---

## D-008 — FastAPI app **factory** + lifespan

**Context.** The app must boot with logging configured and an engine ready, without
import-time side effects that break tests.

**Decision.** `create_app()` builds the `FastAPI` instance; a `lifespan` context
configures logging, constructs the engine on startup and disposes it on shutdown. A
module-level `app = create_app()` remains for the ASGI entrypoint.

**Alternatives.**
- *Module-level construction with import-time setup* — simpler, but any import (e.g. in a
  test) would configure global logging and build an engine.
- *Startup event decorators* — older style, superseded by lifespan.

**Consequences.**
- (+) Tests can build isolated app instances; startup/shutdown are symmetric.
- (−) Slightly more indirection than a bare `app = FastAPI()`.
- *Reversible:* yes.

---

## D-009 — `/health` does not touch the database

**Context.** Liveness checks must distinguish "process is serving" from "a dependency is
down".

**Decision.** `/health` returns a static `{"status": "ok"}` and performs no I/O.

**Alternatives.**
- *Query the DB in `/health`* — conflates liveness with readiness; a DB blip would make
  orchestrators kill a healthy process.

**Consequences.**
- (+) Liveness stays green during dependency degradation.
- (−) A separate readiness endpoint will be needed later to report DB health.
- *Reversible:* yes.

---

## D-010 — Local Postgres via `docker-compose`

**Context.** Developers need a real Postgres (for `SKIP LOCKED` and Postgres-specific
types) without installing it natively.

**Decision.** A single `postgres:16` service with a named volume and `pg_isready`
healthcheck; credentials default to `janus/janus/janus` to match `.env.example`.

**Alternatives.**
- *`testcontainers`* — great for CI isolation, but heavier and adds a dependency; not
  needed while tests use a lightweight DB.
- *SQLite everywhere* — can't exercise `SKIP LOCKED`; fine for unit/integration only.

**Consequences.**
- (+) One command to get a production-like DB; data persists across restarts.
- (−) Requires Docker locally; CI can add the same service when an integration test needs
  it.
- *Reversible:* yes.

---

## D-011 — `mypy` strict; no SQLAlchemy plugin

**Context.** Static typing is required (`mypy janus`). SQLAlchemy 2.1 removed
`sqlalchemy.ext.mypy.plugin`.

**Decision.** Enable `strict = true` and the `pydantic.mypy` plugin only. Rely on
SQLAlchemy 2.0's native `Mapped[...]` typing.

**Alternatives.**
- *Pin SQLAlchemy 2.0 to keep the plugin* — older stack for no benefit; the plugin is
  legacy now.

**Consequences.**
- (+) Strict typing catches errors early; fewer moving parts.
- (−) Some ORM patterns need explicit annotations; acceptable.
- *Reversible:* yes.

---

## D-012 — `pre-commit` hooks run through `uv`

**Context.** Local hooks should match CI and the locked tool versions.

**Decision.** A `pre-commit` config using `language: system` entries that call
`uv run ruff ...` and `uv run mypy janus`, plus standard file-hygiene hooks.

**Alternatives.**
- *Pinned `ruff-pre-commit` repos* — self-contained, but the hook's ruff version can drift
  from the lockfile, producing "passes locally, fails CI" surprises.

**Consequences.**
- (+) One tool version everywhere; hooks fail exactly as CI would.
- (−) Requires `uv` and a synced env before committing.
- *Reversible:* yes.

---

## D-013 — Initial commit & the `JANUS_PLAN.txt` alias

**Context.** Two plan files existed: `PLAN.md` (canonical, with typographic dashes) and
`JANUS_PLAN.txt` (an ASCII duplicate). Docs refer to `PLAN.md` "a.k.a.
`JANUS_PLAN.txt`".

**Decision.** Replace `JANUS_PLAN.txt` with a symlink to `PLAN.md`; make the plan docs the
initial commit.

**Alternatives.**
- *Delete `JANUS_PLAN.txt`* — loses the alias referenced in prose.
- *Keep both* — guarantees they drift.

**Consequences.**
- (+) One source of truth; the alias still resolves.
- (−) Symlinks are awkward on Windows checkouts; acceptable for this Linux-first project.
- *Reversible:* yes.

---

## Phase 1 decisions (S0 spine)

### D-014 — GitHub natural ids as primary keys

**Context.** Webhook deliveries repeat; the system must not create duplicate rows.

**Decision.** Use GitHub's ids (``installation_id``, ``repo_id``, ``run_id``) as primary
keys; internal tables (``jobs``, ``diagnoses``) use surrogate ids.

**Alternatives.** Surrogate ids everywhere with a separate unique column per GitHub id —
more columns, no benefit, and every lookup needs an extra index.

**Consequences.** (+) Stable, globally-unique keys double as the idempotency key and
remove a mapping layer. (−) Coupling the schema to GitHub's id space; acceptable for an
App that only ever sees GitHub entities. *Reversible:* painful later.

---

### D-015 — Webhook idempotency via a unique ``runs.delivery_id``

**Context.** GitHub redelivers webhooks (timeouts, retries). Reprocessing would create
duplicate runs/jobs and duplicate Check runs.

**Decision.** Store the ``X-GitHub-Delivery`` id on ``runs`` with a unique constraint;
``get_or_create_run`` returns the existing row and the handler skips side effects.

**Alternatives.** A dedicated ``webhook_deliveries`` table — cleaner for arbitrary event
types, but S0 only enqueues ``workflow_run``, so one column suffices. Dedup in memory —
lost on restart.

**Consequences.** (+) At-least-once delivery becomes effectively-once processing; the
integration test proves a redelivery posts no second Check. (−) If a future event type
enqueues work without a ``run``, it needs its own dedup key. *Reversible:* yes.

---

### D-016 — ``check_run`` events are acknowledged, never enqueued

**Context.** Janus posts Check runs; GitHub then emits ``check_run`` events.

**Decision.** Accept ``check_run`` with 202 but take no action.

**Alternatives.** React to ``check_run`` — would loop on our own Check runs.

**Consequences.** (+) No feedback loop. (−) Genuine third-party ``check_run`` failures are
ignored in S0; the plan makes ``workflow_run`` authoritative (open question §5 Q1).
*Reversible:* yes, when ``check_run`` handling is needed.

---

### D-017 — Baseline = previous successful run on the branch

**Context.** The fault diff needs a base commit; the plan leaves §5 Q2 open.

**Decision.** Pick the most recent completed ``success`` run on the branch that is not the
failing head (``github/baseline.py``).

**Alternatives.** Merge-base with the default branch — more correct across rebases, but
needs extra API calls and ancestry checks.

**Consequences.** (+) One API call; matches the provisional plan. (−) ~~Does not verify
the green run is an ancestor of the head; a force-push/rebase can mislead.~~ **Resolved
in the S0 closeout:** `resolve_baseline` now confirms ancestry via the compare API and
returns ``None`` on divergence (see D-030). *Reversible:* yes.

---

### D-018 — Manifest credentials persisted to ``.env`` (S0)

**Context.** The manifest callback yields an App id, PEM, webhook secret and OAuth
credentials that must survive restarts.

**Decision.** Write the PEM to a file and the non-secret values to ``.env`` (gitignored),
per the plan's S0 recommendation.

**Alternatives.** OS keyring — better secrecy, but a runtime dependency and awkward on
servers. DB-encrypted column — needs a master key anyway.

**Consequences.** (+) Zero-dependency, easy rotation, obvious to a local operator.
(−) Secrets sit in a plaintext file; acceptable for a local single-operator S0, and
``.env`` is gitignored. Document rotation. *Reversible:* yes.

---

### D-019 — Manifest ``state`` is generated but not validated (S0)

**Context.** OAuth-style flows should validate ``state`` to prevent CSRF.

**Decision.** Generate ``state`` and pass it to GitHub, but do not persist/verify it in
S0.

**Alternatives.** Store ``state`` in a signed cookie or DB row and verify on callback.

**Consequences.** (−) ~~A known gap: a malicious callback could be replayed.~~ **Resolved
in the S0 closeout:** the state is bound to an HttpOnly cookie and verified on callback
(see D-029). *Reversible:* yes.

---

### D-020 — ``/manifest/start`` returns a self-submitting form, not a 302

**Context.** GitHub's manifest creation endpoint expects the manifest as a **POST** body;
a redirect cannot carry one.

**Decision.** Render a minimal HTML page whose form auto-submits to GitHub.

**Alternatives.** A bare ``RedirectResponse`` — impossible for a POST with a body. Asking
the user to paste JSON — worse UX.

**Consequences.** (+) True one-click install. (−) Returns HTML from an API-ish path;
acceptable for a browser-only endpoint. *Reversible:* yes.

---

### D-021 — Sync GitHub/DB work runs in a worker thread

**Context.** The webhook endpoint is ``async`` (to read the raw body), but the DB layer
and ``httpx`` client are synchronous.

**Decision.** Verify the signature on the event loop, then run persistence and GitHub
calls via ``run_in_threadpool``.

**Alternatives.** Make the whole stack async (async SQLAlchemy + ``httpx.AsyncClient``) —
more moving parts for S0. Block the event loop — would stall the server under load.

**Consequences.** (+) No event-loop blocking with a sync stack; 202 returns promptly.
(−) One thread hop per failed run; negligible at S0 volume. *Reversible:* yes, and the
call boundary is a single ``run_in_threadpool`` call.

---

## S0 closeout decisions

### D-029 — Manifest ``state`` validated via an HttpOnly cookie

**Context.** D-019 left the manifest ``state`` unvalidated: any callback could be replayed.

**Decision.** ``/manifest/start`` sets the random ``state`` in an HttpOnly, SameSite=Lax
cookie; ``/manifest/callback`` rejects the request (400) unless the echoed ``state``
matches it, before fetching or writing any credential.

**Alternatives.** A signed ``state`` value (itsdangerous) — unnecessary: ``state`` is a
per-browser nonce, not a secret, so it only needs to match the cookie. A server-side
store — state across restarts for no benefit at S0 scale.

**Consequences.** (+) Closes the CSRF gap with no new dependency. (−) Requires cookies in
the browser flow; fine for a browser-only endpoint. *Reversible:* yes.

---

### D-030 — Baseline ancestry verified with the compare API

**Context.** D-017 accepted "most recent success on the branch" without checking that the
green commit is an ancestor of the failing head; a force-push could make it unrelated.

**Decision.** After selecting a candidate, confirm it with the compare API. Accept
``ahead``/``identical``; on ``diverged``/``behind`` return ``None`` (unknown diff). A
failed compare is non-fatal and keeps the candidate.

**Alternatives.** Accept the candidate unchecked (D-017) — risks a meaningless diff.
Hard-fail on a failed compare — loses the diff over a transient network error.

**Consequences.** (+) The diff base is always an ancestor when we produce one. (−) One
extra API call per failed run. *Reversible:* yes.

---

## Phase 2 decisions (orchestration)

### D-031 — Postgres ``SKIP LOCKED`` queue instead of an external broker

**Context.** Workers must claim jobs without double-processing, at low volume, with no
extra infrastructure.

**Decision.** A `jobs` table is the queue; `claim_next` uses
`SELECT … FOR UPDATE SKIP LOCKED`. No Temporal/RabbitMQ/Celery.

**Alternatives.** Temporal — durable workflows, but a whole service to run and operate.
Celery/RabbitMQ — a broker and result backend for a ~50-line need. A plain `SELECT` +
`UPDATE` — races under concurrency.

**Consequences.** (+) The database is the queue: atomic, no new component, and the job
row *is* the state. (+) Proven by a Postgres-gated test that two workers never claim the
same job. (−) Not designed for tens of thousands of jobs/second; revisit at scale
(`PLAN.md` §2 already defers Temporal). *Reversible:* the queue is two functions.

---

### D-032 — Idempotent enqueue via ``jobs.idempotency_key``

**Context.** A redelivered webhook, a retry, or a re-run could enqueue duplicate work.

**Decision.** `jobs.idempotency_key` is unique (default ``run:{run_id}``); `enqueue`
returns the existing job instead of creating a second.

**Alternatives.** Dedup only at the webhook (`runs.delivery_id`) — misses re-enqueues from
other sources. No dedup — duplicate Check runs and duplicated cost.

**Consequences.** (+) Enqueue is safe to call repeatedly; the unique constraint enforces
it in the database. (−) A job is one-per-run by default; genuinely independent work needs
a distinct key. *Reversible:* yes.

---

### D-033 — Kill switch stored in the database, env as a hard override

**Context.** "Stops claiming immediately" rules out a config change that needs a restart.

**Decision.** The switch is a `system_flags` row read on every claim; `Settings.kill_switch`
is an env-level hard override for tests and fleet-wide shutdowns.

**Alternatives.** Env-only — requires a restart, so not "immediate". A file sentinel —
works but is awkward to set atomically across hosts.

**Consequences.** (+) Runtime-toggleable, observable, and testable. (−) One indexed read
per claim; negligible. *Reversible:* yes.

---

### D-034 — Caps and quotas degrade to ``COMMENT_ONLY``

**Context.** An automated agent will burn unbounded time/tokens if unchecked
(`PLAN.md` §4).

**Decision.** Exceeding a cost cap or a per-repo quota routes the job to terminal
``COMMENT_ONLY`` — report what was observed, spend nothing more.

**Alternatives.** Fail the job — loses the partial result. Ignore the cap — unbounded
spend, the exact failure mode the guardrail exists to prevent.

**Consequences.** (+) Bounded spend and a safe, honest outcome. (−) A legitimate large
job can be cut off; caps are configurable. *Reversible:* yes (defaults in `Settings`).

---

### D-035 — Retries via exponential backoff stored on ``run_after``

**Context.** Transient failures should be retried, but hammering makes them worse, and
retries must be bounded.

**Decision.** On a retryable failure with `attempts < max_attempts`, set the job back to
`QUEUED` with `run_after = now + base·2^(attempts-1)`; otherwise terminal `FAILED`.

**Alternatives.** Immediate retry — thundering herd on a flaky dependency. Unbounded
retries — never terminates, unbounded cost. An in-memory timer — lost on restart.

**Consequences.** (+) Bounded, spaced retries that survive restarts. (−) Backoff delays
recovery for genuinely transient blips; base is configurable. *Reversible:* yes.

---

### D-036 — The processor is a pluggable callable

**Context.** The worker's mechanics are stable across milestones; the *method* changes.

**Decision.** `JobProcessor = Callable[[Session, Job], JobResult]`. Phase 2 ships a no-op
default; S1/S2 pass the real reproduce→triage→fix pipeline.

**Alternatives.** Hard-code the pipeline in the loop — couples proven mechanics to
unfinished logic. A class hierarchy — more ceremony than one function needs.

**Consequences.** (+) Phase 2 is provably correct on its own and testable with fake
processors; S1/S2 plug in without touching the loop. (−) A processor must report costs and
retryability correctly for caps/backoff to work; the `JobResult` contract makes that
explicit. *Reversible:* yes.

---

### D-037 — Processor exceptions are retryable, never fatal to the loop

**Context.** A processor bug must not wedge the queue.

**Decision.** `Worker._process` catches any exception and returns a retryable `FAILED`,
which the normal bounded-retry path handles.

**Alternatives.** Let it propagate — one bad job could crash the worker loop. Silently
succeed — hides real failures.

**Consequences.** (+) The loop is resilient; failures are recorded in `last_error` and
bounded by `max_attempts`. (−) A deterministic bug burns `max_attempts` attempts before
`FAILED`; acceptable and visible in logs. *Reversible:* yes.

---

## Backend pipeline decisions (D1–D3)

These supersede parts of Phase 2 (D-031…D-037) and come from the backend spec.

### D-038 — Async database (SQLAlchemy 2 async + asyncpg)

**Context.** The backend spec mandates async; Phase 2 was sync (`psycopg`).

**Decision.** `api` and `worker` use SQLAlchemy 2 async with `asyncpg`; Alembic keeps a
sync engine by rewriting the driver to `psycopg`.

**Alternatives.** Stay sync — simpler, but not the spec's stack and blocks the event loop
for DB I/O. Two code paths — rejected.

**Consequences.** (+) One async stack for api + worker; `db/session.py`, `repository.py`,
`queue.py`, `worker.py` rewritten. (−) Async SQLAlchemy requires `greenlet`; tests use
`aiosqlite`. *Reversible:* costly (rewrite).

### D-039 — Ingest idempotency on a `deliveries` table

**Context.** S0 deduped on `runs.delivery_id` and returned 202 for everything.

**Decision.** A `deliveries` row keyed on `X-GitHub-Delivery` with
`INSERT ... ON CONFLICT DO NOTHING`; delivery + run + job in one transaction. Duplicate →
**200**; accepted → 202; over queue depth → **503 + `Retry-After`**.

**Alternatives.** Keep run-level dedup — conflates the run with the delivery. Application
check-then-insert — racy.

**Consequences.** (+) Idempotency is enforced by the PK; status codes match the spec.
(−) One more table. *Reversible:* yes.

### D-040 — State vocabulary: no `claimed`, no `comment_only`

**Context.** Phase 2 had `claimed` and `comment_only`.

**Decision.** States are `queued → running → succeeded | failed | dead | skipped_budget`;
a claim goes straight to `running` with a lease. Comment-only is *behaviour* driven by the
triage route (`diagnoses.route`), not a queue state.

**Alternatives.** Keep `claimed` — an extra state with no meaning now that a lease carries
ownership. Keep `comment_only` — conflates outcome with queue state.

**Consequences.** (+) The queue state is purely about scheduling; the triage outcome lives
where it belongs. (−) The brain must map its route to a terminal state (a comment is
`succeeded`). *Reversible:* yes.

### D-041 — Leases + heartbeat + reaper for crash safety

**Context.** A `kill -9` cannot release a row lock, so transaction-level correctness alone
does not recover a crashed worker.

**Decision.** A claim stamps `locked_by` + `lease_expires_at`; a background task extends
the lease every few seconds; a reaper requeues running jobs whose lease expired (without
consuming an attempt).

**Alternatives.** Rely on transaction rollback — only works if the process survives to roll
back. A separate lease service — unnecessary.

**Consequences.** (+) Crash-safe recovery; `SKIP LOCKED` keeps reapers from double-requeuing.
(−) A periodic reaper query; retries must tolerate one re-run. *Reversible:* yes.

### D-042 — Full-jitter backoff and a dead-letter state

**Context.** Phase 2 used deterministic exponential backoff and `comment_only` on
exhaustion.

**Decision.** `run_after = now + rand(0, min(cap, base·2^attempts))`; retries only on
transient failures; exhaustion or a permanent failure → `dead`, with admin requeue.

**Alternatives.** Fixed backoff — thundering herd. Unbounded retries — never terminates.

**Consequences.** (+) Spaced retries, a bounded failure path, and an operator escape hatch.
(−) Jitter makes timing non-deterministic (tests inject `rng`). *Reversible:* yes.

### D-043 — Transaction discipline: process outside the transaction

**Context.** Holding a transaction open across long processing would lock the row and block
heartbeats.

**Decision.** Claim commits immediately; processing runs with no open transaction; a
separate task heartbeats; finalization is a new short transaction.

**Alternatives.** Hold one transaction across processing — simpler but blocks heartbeats
and lengthens lock time.

**Consequences.** (+) Heartbeats work, locks are short, a crash is recoverable via the
lease. (−) Finalization re-loads the job by id and must be idempotent w.r.t. state.
*Reversible:* yes.

### D-044 — Sync GitHub SDK run on a worker thread

**Context.** The GitHub client uses sync `httpx`; the endpoint is async.

**Decision.** Keep the sync client and call it via `run_in_threadpool` for the best-effort
baseline + Check-run work; the DB layer stays async.

**Alternatives.** Convert the client to `httpx.AsyncClient` — cleaner long-term, but churns
the client and its tests for no correctness gain now.

**Consequences.** (+) No event-loop blocking; ingest stays fast; the GitHub layer is
unchanged. (−) A thread hop per failed run; revisit if the client becomes async.
*Reversible:* yes.

### D-045 — Squash migrations into one baseline

**Context.** Pre-release; two migrations existed and the jobs table was reworked.

**Decision.** Squash to a single `initial schema` migration.

**Alternatives.** An incremental alter migration (rename `status`→`state`, drop
`idempotency_key`, add lease columns) — more faithful history but fragile with no data to
preserve.

**Consequences.** (+) A clean baseline; `alembic check` clean. (−) History collapses
pre-release; acceptable with no deployments. *Reversible:* no (once deployed).

### D-046 — Sandbox settings live in a TOML config file

**Context.** Sandbox behaviour (image, limits, network, commands) needs tuning without code
changes, and it is process-wide infrastructure rather than per-repository behaviour.

**Decision.** Read sandbox settings from `janus.toml` (top-level tables), falling back to
`[tool.janus]` in `pyproject.toml`, with `JANUS_CONFIG_FILE` overriding the path.
Environment variables still win over the file; nested keys use a double underscore
(`JANUS_SANDBOX__IMAGE`).

**Alternatives.** Env vars only — awkward for nested lists like `allow_hosts`. A YAML file —
`PLAN.md` reserves `.janus.yml` for per-repo config, so the process-wide file uses TOML.

**Consequences.** (+) Limits are inspectable and diffable; one source of truth per process.
(−) A second configuration mechanism alongside env vars, mitigated by a strict precedence
order. *Reversible:* yes.

### D-047 — Copy source in with `docker cp`, no host bind mounts

**Context.** The sandbox needs the repository's code, and `PLAN.md` §4 requires read-only
mounts and no host exposure.

**Decision.** Create the container, start it, and copy the source in with `put_archive` (the
`docker cp` API), then `chown` it to the run user. Nothing is bind-mounted from the host.

**Alternatives.** Bind-mount the repo read-only — simpler, but exposes a host path and needs
a second writable mount for caches; a named volume — allows a read-only rootfs but adds
lifecycle and cleanup risk.

**Consequences.** (+) The host filesystem is never exposed; cleanup is just container
removal. (−) The copy is O(size) per run, and (see D-049) it precludes a read-only rootfs.
*Reversible:* yes.

### D-048 — Two-phase runs; setup may use the network, the test never does

**Context.** Installing dependencies needs the internet; running untrusted test code should
not have it.

**Decision.** A run is up to two phases. Phase A (setup) runs with network access; phase B
(the tests) always runs with `network_mode=none`. When the configuration forbids network
access, phase A is skipped entirely and only phase B runs.

**Alternatives.** One phase with network on throughout — simpler but gives untrusted tests
network access. Always offline — breaks dependency install for most repos.

**Consequences.** (+) Tests are offline while installs still work; the offline path is a
single phase. (−) Two container lifecycles when setup is needed. *Reversible:* yes.

### D-049 — Ephemeral writable rootfs instead of a read-only rootfs

**Context.** `docker cp` refuses to write into a read-only rootfs unless the destination is a
volume, and a tmpfs at the work dir is invisible to `put_archive` (the daemon writes to the
rootfs path, which the tmpfs shadows). Both were confirmed by the integration test.

**Decision.** Leave the rootfs writable and rely on the container's own ephemeral layer,
which is discarded on removal. Keep a size-limited `tmpfs` at `/tmp`. Isolation comes from
no host bind mounts, no network, dropped capabilities, `no-new-privileges`, a non-root user,
and resource limits.

**Alternatives.** Read-only rootfs plus a named volume for the work dir — stronger, but adds
volume lifecycle and cleanup risk; tmpfs work dir — incompatible with `put_archive`.

**Consequences.** (+) Copying works and cleanup stays trivial. (−) Writes are bounded by the
container layer rather than by a tmpfs quota; revisit if a read-only rootfs is required.
*Reversible:* yes.

### D-050 — Resource telemetry by sampling the stats API

**Context.** Docker's stats API is a stream and returns nothing once a container exits, so a
post-mortem read is unreliable.

**Decision.** A `StatsSampler` background thread polls the stats API every 0.5 s and keeps
the peak memory and cumulative CPU seconds. It takes an injected callable, so it is testable
without Docker.

**Alternatives.** Read cgroup files from the host — more precise, but path- and
version-dependent; rely on the exit code — no resource data at all.

**Consequences.** (+) Portable, no host paths, unit-testable. (−) Sampling can miss very
short-lived peaks (the interval bounds resolution). *Reversible:* yes.

### D-051 — A Janus-owned base image

Superseded by D-056.

**Context.** Runs must be repeatable and reasonably fast.

**Decision.** Default to a Janus-owned image (`janus/python:3.12`) with pytest, coverage.py
and mutmut preinstalled, configurable in the TOML file.

**Alternatives.** Pull `python:3.12-slim` and install per run — slower and less repeatable.

**Consequences.** (+) Faster, reproducible runs. (−) The image must be built and published;
`pull_image = false` supports locally built images. *Reversible:* yes.

### D-052 — Fail closed on an unenforceable host allowlist

Superseded by D-059.

**Context.** The egress allowlist proxy is not built, but `allow_hosts` exists in the config
and reads as an enforced restriction. `network = true` produces an unrestricted bridge.

**Decision.** Reject `network = true` combined with a non-empty `allow_hosts` at settings
validation. Callers must either clear the host list (accepting unrestricted egress) or keep
the network off.

**Alternatives.** Silently ignore the host list — leaves a false sense of restriction.
Build the proxy now — out of scope for this phase.

**Consequences.** (+) The configuration cannot overstate its isolation; the limitation is
explicit. (−) Expressing a host list is deferred until enforcement exists. *Reversible:* yes
(when the proxy lands, the validator is relaxed).

### D-053 — Config discovery searches upward and fails loudly

**Context.** Config discovery was relative to the process working directory, so launching
from a subdirectory silently loaded defaults; and a malformed file was swallowed, silently
reverting every setting.

**Decision.** Search the working directory and its parents for `janus.toml`, then for a
`pyproject.toml` carrying `[tool.janus]`. An explicit `JANUS_CONFIG_FILE` must exist. A
found file that cannot be read or parsed raises `ConfigError` at startup.

**Alternatives.** Keep cwd-only discovery — silently environment-dependent. Ignore bad
files — hides typos.

**Consequences.** (+) The same checkout behaves the same from any subdirectory; a typo is a
startup error rather than a silent default. (−) Discovery does a little I/O up the tree.
*Reversible:* yes.

### D-054 — Work-dir permissions without capabilities

**Context.** The container drops all capabilities, so root inside it cannot `chown`
(capability-less root is subject to normal DAC). The earlier `chown -R` always failed with
`EPERM`, and `/work` is created `root:root 755`, so the run user could not write caches.

**Decision.** Make the work dir writable from the root keepalive (`chmod 0777`, which the
owner can do without capabilities) and write the copied tar entries with the run user's
uid/gid via the archive filter. Drop the runtime `chown` entirely.

**Alternatives.** Keep `CAP_CHOWN` — weakens the isolation for one operation. Run as root —
defeats the non-root user.

**Consequences.** (+) The run user can read and write the work dir; the tar path stays
capability-free. (−) `chmod 0777` makes the work dir writable by any container user, which
is acceptable in a single-tenant ephemeral container. *Reversible:* yes.

### D-055 — Bounded output, streamed source archive, async entry point

**Context.** Review found three resource hazards: the whole repo was tarred twice in memory,
command output was unbounded, and the sync `run()` would block the async worker loop.

**Decision.** Build the source archive into a spooled temp file (rolling to disk past 8 MiB)
and stream it to `put_archive`; truncate captured output to `max_output_bytes` before
redaction; add `SandboxRunner.run_in_thread` (`asyncio.to_thread`) as the entry point for
event-loop callers.

**Alternatives.** Keep in-memory tar — OOM risk on large checkouts. Bound capture inside the
container — needs output streaming, deferred.

**Consequences.** (+) Worker memory and redaction time are bounded; the loop stays
responsive when wired. (−) Output is still buffered by `exec_run` before truncation.
*Reversible:* yes.

### D-056 — Build a fresh image per sandbox run

**Context.** D-051 defaulted to a Janus-owned base image (`janus/python:3.12`), but no such
image is published anywhere, so every run failed to pull it. Runs need an environment that
exists without a build-and-publish pipeline we do not have.

**Decision.** Drop the published base image. Each run resolves its own image and, when it is
not `prebuilt`, builds it fresh: from a `Dockerfile` at the repository root when one exists,
otherwise from a Janus-generated Dockerfile (base `python:3.12-slim` plus configurable
tooling and optional dependency install). The tag is content-addressed from the Dockerfile,
the base image, and the build-context manifests, so identical inputs reuse a tag. The image
is used for that one run and removed afterwards. The source is still copied in at run time
with `docker cp` (D-047); the image never bakes in the source. `prebuilt` remains an escape
hatch that uses `settings.image` unchanged and is never removed.

**Alternatives.** Publish and maintain a Janus base image — needs a registry and a release
pipeline, and it still cannot know a repo's dependencies. Pull `python:3.12-slim` and install
per run without building — same cost, but loses the repo-Dockerfile path and content-addressed
caching. Bind-mount the source into the build — bakes the source into a layer and exposes a
host path.

**Consequences.** (+) No external image dependency; the environment is derived from the repo
and the config, and is reproducible via the content-addressed tag. (+) `prebuilt` keeps a fast
path for a known image. (−) Every run pays the build cost when the tag is not cached, and a
Docker daemon with build support is required. *Reversible:* yes (set `image_source =
"prebuilt"`).

### D-057 — Rootless Docker is the target, plain Docker is the local default

**Context.** Running the daemon as root means a container escape is a host-root escape, so the
intended deployment is rootless Docker. Local development, however, usually has a plain daemon
reachable at the default socket, and we do not want to break it.

**Decision.** Add a `docker_host` setting: when set, the runner builds its client with
`docker.DockerClient(base_url=docker_host)`; when unset it uses `docker.from_env()`. Add
`require_rootless` (default `false`): when true, `run()` checks the daemon's `SecurityOptions`
for a `rootless` entry and, if absent, returns `error="not_rootless"` without creating a
container — it never raises. Host setup (enabling rootless, cgroup v2 delegation) is not
automated.

**Alternatives.** Assume rootless and read `DOCKER_HOST` only — makes local plain-Docker runs
awkward and hides the choice. Refuse to run at all without rootless — blocks local development.
Detect rootless and silently downgrade — hides a security-relevant difference.

**Consequences.** (+) The same code targets a rootless daemon in production and a plain daemon
locally, with the choice made in config. (+) `require_rootless` fails closed where it matters.
(−) Rootless detection depends on the daemon reporting `SecurityOptions`; a daemon that omits
it is treated as rootful, which is the safe default. *Reversible:* yes.

### D-058 — Bound sandbox output while streaming, not after capture

**Context.** D-055 truncated output to `max_output_bytes`, but only *after* `exec_run` had
buffered the command's entire output in the daemon and SDK. A command that prints gigabytes
would still be buffered in full before any bound applied, so the cap did not actually bound
memory.

**Decision.** Run the command with streaming exec (`exec_create` + `exec_start(stream=True,
demux=True)`), accumulate each stream up to `max_output_bytes`, then keep draining the stream
and discard the excess so the command cannot block on a full pipe. A truncated stream gets the
existing `[... output truncated ...]` marker. The exec is inspected after the stream ends to
recover the real exit code, because `container.exec_run(..., stream=True)` returns
`exit_code=None`. `_truncate` stays as a final safety net. The wall-clock cap is unchanged:
kill the container on overrun and keep the output already emitted.

**Alternatives.** Keep buffering then truncate — the daemon holds unbounded output (the bug).
Use `container.exec_run(..., stream=True)` — the SDK drops the exit code when streaming.
Kill the stream at the cap — a full pipe would block the command.

**Consequences.** (+) Memory and redaction time are bounded by `max_output_bytes` regardless of
how chatty the command is; the stream is always drained so the command never stalls. (−) The
exec must go through the low-level API to keep the exit code, and a killed command keeps only
its already-emitted output. *Reversible:* yes.

### D-059 — Enforce the host allowlist with an internal network plus an allowlisting proxy

**Context.** `allow_hosts` was recorded but not enforced, so D-052 rejected `network = true`
with a non-empty host list rather than let the config overstate its isolation. A host list is
only useful if a listed run physically cannot reach anything else.

**Decision.** When a run has `network = true` and a non-empty `allow_hosts`, the runner opens a
`ProxySession` that creates two user-defined Docker networks owned by the run:
`janus-sbx-<id>` with `internal=True` (no route to the outside) and `janus-sbx-up-<id>`, a
normal bridge. An allowlisting proxy container is attached to **both**; the sandbox is attached
**only** to the internal network, with `HTTP_PROXY`/`HTTPS_PROXY`/`http_proxy`/`https_proxy`
pointing at `http://<proxy-name>:8888`. The proxy accepts plain HTTP and `CONNECT`, checks the
target host against the allow list, and answers `403` otherwise. Because the sandbox is not on
the upstream network, direct egress fails even if it ignores the proxy variables — the topology,
not the proxy alone, enforces the policy. The proxy image is shared and content-addressed over
the Dockerfile and the script template (not the per-run host list), built once and never
removed; the per-run list is passed to the container as `JANUS_PROXY_ALLOW_HOSTS`. Teardown is
sandbox → proxy → both networks, always, in a `finally`. The D-052 fail-closed validator is
removed: `network = true` with a host list is now valid and enforced. The host-matching rule
(exact, case-insensitive, trailing-dot-insensitive, leading-dot wildcard, empty = any) lives in
`janus.sandbox.allowlist.host_allowed` and is embedded as data in the proxy script.

**Alternatives.** Rely on the proxy alone (proxy environment variables) — a sandbox that clears
them escapes the allow list. A per-run proxy image embedding the host list — no image reuse. A
transparent iptables/DOCKER-USER ruleset — needs host privileges and is not portable to
rootless Docker.

**Consequences.** (+) A host-listed run cannot reach a non-listed host, including directly; the
proxy and both networks are removed after the run. (+) The proxy image is built once and shared.
(−) The proxy is HTTP/HTTPS-only, so non-HTTP egress through it is not possible and other
protocols simply fail. *Reversible:* yes.

---

## Adapter & processor decisions (Phase 3)

### D-060 — Read artifacts back from the container before removal

**Context.** A test run's useful result is often a *structured file*, not stdout: a pytest JSON
report, a coverage map, a mutation report. Only stdout/stderr is captured by ``exec_run``, and the
container is ephemeral — once it is removed the file is gone. Scraping a report out of the command's
printed output is fragile (the tool may not print it, and the output is bounded by
``max_output_bytes``, so a large report would be truncated and unparsable).

**Decision.** A ``SandboxSpec`` may name ``artifacts`` (absolute container paths) and a per-file
``max_artifact_bytes`` cap. After the command finishes and **before** the container is removed, the
runner reads each path back with ``get_archive`` (the API behind ``docker cp``) into
``SandboxResult.artifacts`` (``path -> raw bytes``); a path that cannot be read is recorded in
``SandboxResult.artifact_errors`` (``not_found``, ``too_large``, ``unreadable: ...``) and never
raised. An artifact larger than the cap is reported ``too_large`` and omitted, **never truncated**:
truncating structured data (a JSON document, a coverage map) corrupts it. Extraction also runs on
the timeout path (the container is killed first; a partial artifact beats none). Artifacts are
returned **raw, not redacted** — a deliberate, documented carve-out from the "all sandbox output is
redacted" rule, because redacting a structured report could corrupt it; the adapter configures its
tools to omit captured output and redacts anything it later surfaces. Artifacts must be written to a
path on the container's writable **root filesystem** (the adapter uses ``/var/tmp/janus``), because
``/tmp`` is a tmpfs and the daemon's ``get_archive`` endpoint cannot read files from that mount — a
report written under ``/tmp`` comes back ``not_found``.

**Alternatives.** Parse the report from stdout — loses large/structured reports to output bounding,
and couples the core to each tool's print format. Keep the container alive and read it out of band —
lifecycle and cleanup risk, and it breaks the "one ephemeral container per run" model. Redact
artifacts like other output — would corrupt the very data the core needs to parse. Truncate an
oversized artifact — produces a report that parses to garbage.

**Consequences.** (+) The core gets the tool's real structured output without scraping stdout, and a
missing or oversized file is a reported, total outcome rather than a crash or a silent loss. (+)
Extraction is bounded and Docker-free to unit-test (a fake container drives the whole path). (−)
Every artifact must be declared on the command and written to a rootfs path, so a tool that only
writes under ``/tmp`` needs the adapter to redirect it. *Reversible:* yes.

### D-061 — The adapter is a pure translator, and a command declares its artifacts

**Context.** The core (reproducer, signals, triage, SBFL, fix loop) must stay language-agnostic, so
every Python-specific detail has to sit behind one boundary. That boundary also has to carry the
structured artifacts D-060 introduced, or the core cannot get a report back.

**Decision.** ``janus/core/adapter.py`` defines an ``Adapter`` Protocol and the language-neutral
types (``Command``, ``TestTarget``, ``TestOutcome``, ``CoverageContext``, ``MutationResult``). An
adapter is a **pure translator**: it converts CI logs, sandbox results and artifacts into those
types, and the core's intent back into commands. It holds **no sandbox and no policy** — the core
owns the runner, the K=5 loop, retries and caps. A ``Command`` is ``argv`` (an argv list) plus
``artifacts`` (the absolute container paths it writes that the runner reads back). Three contract
rules bind every adapter, and the conformance suite in ``tests/conformance/`` enforces all three:
(1) **commands are argv lists, never shell strings** — the runner execs them with no shell, so no
element may contain a shell metacharacter; (2) **every parser is total** — malformed, empty or
truncated input yields ``[]`` or a zeroed result and never raises; (3) **the adapter holds no
policy** — it never decides whether or how often to run, which profile to use, or what a result
means for triage. The Python adapter (``janus/adapters/python/``) is the first implementation; a
Node adapter is later "one new folder" plus a conformance case.

**Alternatives.** Let the adapter own the sandbox/runner — drags language specifics into the core
and makes the conformance suite untestable without Docker. Parse reports from stdout instead of
declaring artifacts — see D-060. Put policy (retries, profiles) in the adapter — splits the
reproduction decision across languages.

**Consequences.** (+) The core stays language-agnostic; a new language is a folder plus a
conformance case, and the suite is non-vacuous (a ``FakeAdapter`` must pass every check). (+) The
artifact channel is part of the interface, so a structured report is declared, not guessed. (−)
Adapters must keep parsers total and commands argv-only; the conformance suite is the guardrail.
*Reversible:* yes.

### D-062 — Per-test coverage from a single ``--cov-context=test`` run

**Context.** SBFL (Phase 5) needs per-test coverage: which lines each test executed, so failing and
passing tests can be compared per file/line. The naive way to get that is one instrumented run per
test, which multiplies sandbox time by the number of tests.

**Decision.** The Python adapter produces per-test coverage from **one** pytest run with
``--cov-context=test`` (via ``pytest-cov``), writing a JSON report that carries a ``contexts``
mapping (test id → per-file executed lines). ``parse_coverage`` reads that artifact into
``CoverageContext`` objects; non-positive line numbers are dropped and malformed input yields no
context. The command declares the report path as its artifact (D-060), and the report is written
under ``/var/tmp/janus``, not ``/tmp``.

**Alternatives.** One run per test with plain coverage — N sandbox runs for one suite, far slower
and capped by the same time limits. No coverage — SBFL would have no input, blocking the S2
localizer.

**Consequences.** (+) Per-test coverage costs one extra run, not one per test; the whole SBFL input
arrives as a single declared artifact. (−) It depends on ``pytest-cov`` being in the image, so
``pytest-cov`` was added to ``tooling_packages`` (the generated-image default and ``janus.toml``).
*Reversible:* yes.

### D-063 — Source is cloned with an environment-supplied credential header

**Context.** The worker must check out the exact revision a failing run points at, from a private
repository, using a GitHub App installation token. ``PLAN.md`` §4 requires that the token never
leak — a token in a clone URL or in argv shows up in process listings, logs and error messages.

**Decision.** ``GitSourceProvider`` clones over HTTPS and passes the installation token as an
``http.extraHeader`` git config value supplied through the environment (``GIT_CONFIG_COUNT`` /
``GIT_CONFIG_KEY_0=http.extraHeader`` / ``GIT_CONFIG_VALUE_0=<Authorization: Basic …>``), so the
token is absent from the clone URL and from every argv. A bad token fails fast instead of prompting
(``GIT_TERMINAL_PROMPT=0``), and the environment is minimal and deterministic (``GIT_CONFIG_NOSYSTEM=1``
plus a temp ``HOME``) so the host's git config cannot leak in or change behaviour. ``credential_header``
builds the ``x-access-token:<token>`` Basic value. The checkout is created in a temp dir and removed
when the context manager exits; ``LocalSourceProvider`` wraps an existing directory for tests.

**Alternatives.** Put the token in the clone URL (``https://x-access-token:<token>@…``) — leaks into
process listings and logs. Pass it via ``-c http.extraHeader=…`` on the argv — same leak. Rely on a
credential helper or the host's git config — non-deterministic and host-dependent.

**Consequences.** (+) The token exists only in the child process's environment for the duration of
one command, never in a URL, an argv or an error message. (+) A missing ``git``, a timeout, or a
non-zero exit becomes a ``SourceError`` that names the argv and return code but never the token.
(−) The provider must construct the environment explicitly, so a git invocation that needs another
variable has to add it. *Reversible:* yes.

### D-064 — A sandbox processor replaces the stub behind ``processor_mode``

**Context.** Phase 2 shipped ``stub_processor`` — the worker's mechanics (claims, leases, retries,
caps) were proven, but the processor did nothing. With the sandbox and the adapter in place, the
worker can run real work, but the triage brain (Phase 4) does not exist yet, so the processor must
stay explicitly evidence-gathering, not verdict-making.

**Decision.** Add ``SandboxProcessor`` (``janus/jobs/processor.py``) and a ``build_processor``
factory; select it with ``processor_mode`` (``stub`` default, ``sandbox``) in ``Settings`` and
``janus.toml``; ``janus.jobs.worker._serve`` picks the processor from that setting. For one job the
processor loads the repository and run rows, acquires the source **off the event loop** (one
``asyncio.to_thread`` covers acquire → run → parse, and the source context is always exited), builds
the sandbox phase specs from the adapter's commands (setup only when the sandbox allows the network;
the test phase offline), runs them with ``SandboxRunner``, parses the test result with the adapter,
and reports through ``JobResult``. ``RunPhase``/``RunPlan`` carry each phase's artifacts, and a
disabled sandbox yields the stub. This is **evidence gathering only**: there is no triage and no
scoring, and **no new tables** — a failing suite is the expected input and a *successful* job that
gathered evidence, so only an infrastructure failure (no daemon, image build failure, timeout) is a
retryable failure. Phase 4 adds the reproducer (the K=5 loop and profiles) and the persistence for
its evidence.

**Alternatives.** Make the stub the only processor until Phase 4 — leaves the sandbox and adapter
unwired and unexercised end to end. Let the processor triage too — premature; the signals and scorer
are Phase 4. Add persistence now — the reproducer owns the schema for its evidence.

**Consequences.** (+) ``processor_mode = "sandbox"`` makes the worker run real work in a container
while the default stays a safe no-op; the processor is fully injectable (session factory, adapter,
source provider, runner) so it is unit-tested with fakes. (+) The blocking sequence never blocks the
event loop and never leaks a checkout. (−) Until Phase 4 there is no reproducer and no stored
evidence, so a job's result is only counts and a short summary in ``JobResult``. *Reversible:* yes
(set ``processor_mode = "stub"``).

---

## Reproduce & triage decisions (Phase 4 / S1)

### D-065 — The reproducer runs the failing-target set K times plus one full-suite run

**Context.** Triage has to know whether a red build is a stable, reproducible failure (a code
bug) or a nondeterministic one (a flaky test), and it needs a measurement cheap enough to fit
inside the per-failure sandbox-minute cap. Rerunning each failing target K times individually
would multiply sandbox time by the number of failing targets; a single rerun cannot separate
deterministic from intermittent at all.

**Decision.** ``janus/core/reproducer.py`` reruns the **failing-target set** K times — one
sandbox run per attempt that exercises every failing target together — then makes **one
full-suite run** at the end. K comes from ``.janus.yml`` (``k_reruns``) or the process setting
(``Settings.k_reruns``, default 5); a value ``<= 0`` is clamped to 1 with a warning. Each rerun
is flattened into one :class:`~janus.core.evidence.RunAttempt` per parsed test outcome, and the
full suite is a single ``RunAttempt`` with ``target_id = None``. When the sandbox itself fails
(no test-phase result, a ``SandboxResult.error``, or a timeout) the reproducer records **one
failed attempt per requested target** with ``sandbox_error`` set and ``passed = False``, so the
determinism signal can see that the sandbox — not the code — was unhealthy; a normal red suite
(non-zero exit, no infra error) is *not* a sandbox failure. ``cost_minutes`` is the sum of every
sandbox run's ``duration_s`` divided by 60, which is what the per-repo cost cap accounts
against. The reproducer makes no triage decision and persists nothing.

**Alternatives.** Rerun each failing target K times separately — K×N sandbox runs, unbounded by
the number of failing tests. Rerun the set once — cannot distinguish deterministic from
intermittent. Skip the full suite — loses the "is the whole suite red" fact and the control
baseline. Let an infrastructure failure raise — turns an unhealthy sandbox into a job failure
instead of evidence.

**Consequences.** (+) Cost is K + 1 sandbox runs regardless of how many targets failed, so it is
predictable against the cap. (+) The K attempts give the determinism signal its always-fail /
intermittent / passes-always classification, and the single full-suite run gives a whole-suite
outcome. (+) A sandbox-level failure is recorded as evidence, never an exception. (−) A target
that only misbehaves under the full suite is seen K times in isolation; the one full-suite run is
the only cross-check. *Reversible:* yes.

### D-066 — Evidence is a separate type from the verdict

**Context.** The reproducer, the seven collectors and the scorer must be built and tested
independently, and the pipeline must never let a measurement quietly become a claim. ``PLAN.md``
§4 requires "no claim without a gate", and the schema already separates facts
(``reproductions``) from judgement (``diagnoses``).

**Decision.** ``janus/core/evidence.py`` carries **measured facts only**: ``RunAttempt`` (one
run of one target), ``Reproduction`` (the K attempts plus the full-suite run, with the derived
``failure_rate()`` / ``always_fails()`` / ``intermittent()`` / ``passes_always()``),
``CommitSummary`` and ``ChangeSummary`` (the last-green → first-red range, with ``available``
false when there is no baseline). None carries a route or a confidence. The **judgement** lives
in ``janus/core/triage/routes.py``: ``Route`` and ``TriageDecision``, produced by ``decide``. A
collector's ``SignalEvidence`` sits between the two — it carries per-route **support votes** (a
measurement of how much it supports each route), never a chosen route, and ``clamp_votes``
guarantees ``UNSURE`` is never voted. The scorer is the only place votes become scores, and
``decide`` is the only place a route is chosen.

**Alternatives.** Let the reproducer return a route — couples measurement to judgement and makes
the reproducer untestable in isolation. Put a confidence on ``Reproduction`` — a fact type would
carry an opinion. Let collectors pick the route — seven places to get the decision rule wrong.

**Consequences.** (+) The three layers can be built and unit-tested against one frozen surface;
each fact row in ``reproductions`` / ``signals`` maps to a pure dataclass while only
``diagnoses`` holds a route, so a reviewer can point at exactly where a claim is made. (−) The
scorer must re-derive everything from votes, so a collector cannot express a hard veto — it can
only vote strongly. *Reversible:* yes.

### D-067 — Signals vote per route and aggregate by weighted average over present signals

**Context.** Seven collectors contribute evidence of very different strength, and not every
collector applies to every failure (there may be no baseline diff, or no flake history).
Aggregation must not punish a failure for the signals that *could not* be measured, and
``UNSURE`` must be the decision rule's output alone — not something a collector can vote for.

**Decision.** Each collector returns ``SignalEvidence.votes``, a mapping of the three real
routes (``TRIAGE_ROUTES``) to support in ``[0, 1]``; a route absent from the mapping means
support ``0``, and ``clamp_votes`` drops any ``UNSURE`` key and clamps the rest.
``janus/core/triage/scorer.py::aggregate`` computes, for each real route,
``sum(weight × vote) / sum(weight of the signals that reported)`` — the denominator covers only
the **present** signals, so a collector that returns ``None`` (abstains) does not dilute the
score toward zero, and a signal whose name is not a key in the weights is ignored entirely.
``UNSURE`` is never a key of the aggregate; it is produced only by ``decide`` (``top score <
tau`` or ``top2_gap < epsilon``). The default weights (``DEFAULT_WEIGHTS``, sum 1.0) are:
determinism ``0.22``, signatures ``0.18``, change_attribution ``0.16``, exit_codes ``0.14``,
correlation ``0.10``, flake_history ``0.10``, resources ``0.10`` — the two strongest
discriminators weigh most, and change attribution (the direct code evidence) is third.

**Alternatives.** Fix the denominator at the sum of all seven weights — a missing signal drags
every score toward zero and biases the run to ``UNSURE``. Majority vote across collectors —
ignores that the signals are not equally informative and cannot express partial support. Let a
collector vote ``UNSURE`` — gives seven ways to pre-empt the decision rule. An unnormalised
weighted sum — the score would not be comparable to ``tau`` in ``[0, 1]``.

**Consequences.** (+) An absent signal is neutral, not contradicting, so a sparse failure can
still reach a real route on the evidence it does have. (+) The decision rule and the tie-break
live in exactly one place (``routes.decide``). (−) Because the denominator adapts, the same
absolute votes yield different scores depending on which signals reported; the per-signal
``reasons`` in the comment make the composition visible. *Reversible:* yes (weights and
``tau``/``epsilon`` are configurable).

### D-068 — The verdict is a Check run, with a PR comment when the run has a PR

**Context.** S0's ingest posts an ``in_progress`` Check run merely to acknowledge a red build,
before any work exists. The verdict — the route, its confidence and its evidence — must be
visible on the commit, and, when the failure is on a pull request, in the PR thread where a
developer is already looking. Posting to GitHub is an external call that can fail and must not
endanger the durable result.

**Decision.** After persisting the evidence and the diagnosis, ``SandboxProcessor`` posts a
**distinct Check run named "Janus triage"** (``Settings.triage_check_name``) with ``status =
"completed"`` and a ``conclusion`` from ``check_conclusion`` (``CODE_BUG`` →
``action_required``; ``FLAKY`` / ``ENV`` / ``UNSURE`` → ``neutral``), whose output is
``render_check_output``. When the run belongs to a pull request (``runs.pr_number`` is set) and
``comment_on_pr`` is on, it also posts an issue comment with ``render_evidence``.
``runs.pr_number`` is captured at ingest from ``workflow_run.pull_requests[0].number``
(``set_run_pr_number``); a missing or malformed field yields ``None`` rather than failing
ingest. Posting is **best-effort** — a failure is logged and never fails the job (the verdict is
already persisted), and the post step is separate from the persist step.

**Alternatives.** Update the ingest acknowledgement Check run in place — couples ingress to the
brain, and the acknowledgement is posted before the job is even claimed. Post only a comment — a
Check run is the surface that attaches to a commit regardless of a PR. Fail the job when a post
fails — retries a job whose verdict is already durable and can double-post.

**Consequences.** (+) The verdict is attached to the commit and, for a PR, mirrored into the
thread; the acknowledgement and the verdict stay separate surfaces. (+) A GitHub outage cannot
lose the verdict, which lives in ``diagnoses`` / ``signals`` / ``reproductions``. (−) Until the
output-idempotency marker (``FUTURE.md`` §D) exists, an at-least-once retry that crashes between
post and commit can duplicate the Check run/comment. *Reversible:* yes.

### D-069 — `.janus.yml` is per-repo config parsed without a new dependency

**Context.** Per-repository behaviour (comment vs PR, K, ``tau``/``epsilon``, cost caps,
setup/test commands) is committed alongside the code, so it must be readable from an untrusted
repository checkout and must not pull a heavy dependency into the worker for a handful of keys.

**Decision.** ``janus/config/janus_yml.py`` defines ``RepoConfig`` (frozen; every field optional,
``None`` meaning "fall back to the process-wide setting") and
``parse_repo_config``/``load_repo_config``. Parsing is dependency-light and ordered: **PyYAML
when it is importable**, else **JSON** (a valid YAML subset), else a small documented parser that
handles exactly the forms this file uses — top-level ``key: value`` scalars, block lists, inline
lists, and ``#`` comments. A **missing** file yields defaults; a **malformed** file raises
``RepoConfigError`` (the caller logs and uses the defaults) rather than silently reverting to
defaults, so a misconfiguration is visible. Validation rejects an unknown ``mode``, a
non-integer ``k_reruns``, a ``tau``/``epsilon`` outside ``[0, 1]``, and a non-positive cap. The
file **must not carry secrets** — it is read as untrusted repository content; tokens live only
in the process environment. Unknown keys are preserved in ``raw`` for forward compatibility.

**Alternatives.** Add PyYAML as a hard dependency — a whole parser for a handful of keys. Fall
back to defaults silently on a parse error — hides a broken config. Allow secrets in the file — a
committed token is a leak. Vendor a full YAML parser — more surface than the schema needs.

**Consequences.** (+) No new dependency, and the same parser works with or without PyYAML
installed. (+) A malformed file fails loud; a missing one is simply defaults. (−) The no-PyYAML
fallback supports only a documented subset, so an exotic ``.janus.yml`` (anchors, nested maps)
needs PyYAML present. *Reversible:* yes.

### D-070 — `git bisect` is deferred; SBFL is the localizer

**Context.** S2 must name the change that introduced a ``CODE_BUG``. The obvious tool is ``git
bisect`` in the sandbox, but ``git bisect`` needs commit history, and the source provider
(D-063) fetches the checkout with ``git fetch --depth 1``, so a cloned repository has a single
commit and no ancestry to bisect. The Python adapter already produces per-test coverage from one
run (D-062), which is the input SBFL needs.

**Decision.** SBFL (Ochiai/DStar over the per-test coverage contexts) is the localizer. ``git
bisect`` is **deferred**: ``Settings.bisect_enabled`` defaults to ``false``, and the deferred
work is recorded in ``docs/FUTURE.md``. ``suspects.introducing_commit`` exists in the schema (the
Phase 4–6 migration) and stays nullable until a history-aware localizer fills it.

**Alternatives.** Unshallow the clone (``git fetch --unshallow``) — multiplies clone time and
disk for every job and re-couples the source provider to a specific localizer. Fetch full history
only for ``CODE_BUG`` jobs — a second fetch path and more sandbox time; deferred with the
feature. Keep bisect on a shallow clone — bisect on a one-commit history cannot work.

**Consequences.** (+) The localizer works on the checkout the pipeline already produces, with no
extra fetch. (+) The deferred item is explicit rather than a silent gap. (−) Until bisect lands,
Janus can rank ``file:line`` suspects but cannot name the introducing commit, so
``introducing_commit`` stays ``NULL``. *Reversible:* yes (flip ``bisect_enabled`` and add the
fetch).

### D-071 — SBFL over per-test coverage, Ochiai by default

**Context.** The fix loop needs to name the source lines worth changing. The adapter's
coverage run already records which lines each test executed (D-062). Using file-level
coverage would only say that some test touched a file; it cannot recover which *failing*
tests covered a line, so the classic SBFL counters — ``ef`` (failing tests covering the
line) and ``ep`` (passing tests covering it) — are not computable. The choice of formula
and of which lines count as suspects is therefore part of the localizer's contract.

**Decision.** ``janus/core/localize/sbfl.py::localize`` consumes one ``CoverageContext`` per
test and treats a context whose ``test_id`` is in the failing set as failing, every other
context as passing. It computes ``ef`` / ``ep`` / ``nf`` / ``np`` per ``(file, line)`` and
ranks with **Ochiai** by default: ``ef / sqrt((ef + nf) * (ef + ep))``. **DStar**
(exponent ``*`` = 2) is available as ``SbflFormula.DSTAR``. A line with **``ef == 0``** is
covered only by passing tests and is **not a suspect**, so it is omitted even when its
formula score is defined. Degenerate denominators score ``0.0``; duplicate contexts and
repeated pairs count once; the result is capped at ``max_suspects`` (default 5) with ties
broken by ``file`` then ``line``.

**Alternatives.** File-level coverage — cannot distinguish failing from passing coverage,
so no SBFL formula applies. DStar as the default — more sensitive to a single failing test
and less stable on small spectra; kept as an option rather than the default. Include
``ef == 0`` lines with score 0 — they can never outrank a covered-by-a-failure line and
would only pad the suspect list with innocent code. A learned ranker — no training data in
S2.

**Consequences.** (+) Suspects are grounded in the failing tests themselves, not in a
heuristic over changed files. (+) Ochiai's bounded ``[0, 1]`` score is easy to display and
to reason about. (+) Deferring DStar to an explicit enum keeps the default deterministic.
(−) SBFL needs per-test coverage, which is slower than a single file-level run; that cost is
already paid by the adapter and shared with S1 evidence. *Reversible:* yes (pass a
different ``SbflFormula``).

### D-072 — Retrieval is git-log grep, not a vector index

**Context.** ``PLAN.md`` §1.3 asks the generator to learn from past fixes with a grep of
``git log -p`` and explicitly rejects a vector database for v1 (§2: "Vector DB / RAG →
``git log -p`` grep; good enough for v1"). A repository's own history is already on disk in
the checkout, needs no ingestion pipeline, and cannot leak code to an embedding provider.

**Decision.** ``janus/core/retrieve/git_grep.py`` is the whole retriever. ``build_terms``
turns failing test ids, suspect basenames and error-line tokens into a small ordered,
case-insensitively de-duplicated set, dropping timestamps, paths, line numbers, addresses
and hashes. ``git_log_argv`` builds a shell-free `git log --oneline -p --max-count=50
-G<term>` argv (one predicate per term, regex-escaped); ``parse_git_log`` parses commits
into ``PriorFix`` rows with a bounded diff excerpt; ``rank`` scores by the number of
distinct matching terms. The processor keeps the top five and the prompt uses at most
three. There are **no embeddings** and no index.

**Alternatives.** A vector index over commits — a new store, an embedding dependency, a
leak surface, and an ingestion job for a corpus that is usually shallow. Feeding the whole
log into the prompt — unbounded and mostly irrelevant. Grepping only the current source —
misses the historical fix that shows the intended change shape.

**Consequences.** (+) Zero new infrastructure; retrieval works offline in the checkout.
(+) Deterministic terms make the prompt and its tests reproducible. (−) ``-G`` matches text,
not meaning, so a fix described with different vocabulary is missed; a semantic retriever
can replace this later without changing the processor's interface. *Reversible:* yes.

### D-073 — Generation goes through an OpenAI-compatible HTTP client with a NullLLM fallback

**Context.** The generator needs one text completion per attempt and must not marry the
pipeline to a vendor SDK, a browser login or a particular hosted product. CI and tests must
run with no key at all, and the key is a secret that must never leak into logs, argv or a
URL.

**Decision.** ``janus/core/generate/llm.py`` defines the minimal ``LLMClient`` protocol
(``available`` and ``complete``). ``OpenAICompatibleClient`` calls
``POST {base_url}/chat/completions`` over ``httpx`` with an injectable transport;
``temperature=0.0`` and a bounded ``max_tokens``. The API key travels **only** as a
``Bearer`` header — never in the URL, never in an error message, never logged. ``build_llm``
requires **both** ``llm_api_key`` and ``llm_base_url``; with either missing it returns
``NullLLM`` (``available = False``). In that case the loop stops at ``status="no_llm"``
with its suspects and prior fixes, and ``_post_fix`` degrades to the suggest comment rather
than inventing a diff.

**Alternatives.** A vendor SDK — a heavy dependency for one endpoint and a moving API
surface. Make the key optional and call a default endpoint — an implicit network call and
an ambiguous failure mode. Raise when unconfigured — turns a deployment choice into a
failed job. Log the request for debugging — would leak the key and repository source.

**Consequences.** (+) Any OpenAI-compatible endpoint works, including a local one. (+)
Tests use ``httpx.MockTransport`` and need no network or key. (+) An unconfigured
deployment degrades visibly and safely. (−) The client implements only the chat-completions
shape, so a provider with a different protocol needs an adapter. *Reversible:* yes.

### D-074 — The four proof gates fail closed; mutation is opt-out but visibly disabled

**Context.** A generated patch is a claim that the suite is green, mutation and coverage did
not regress, and no test file changed. Measurements can be absent for mundane reasons (the
sandbox failed, a tool wrote no artifact, coverage produced no contexts). Treating absence
as success would let an unmeasured diff become a PR, which is exactly the "no claim without
a gate" guardrail (``PLAN.md`` §4). Mutation is also by far the most expensive gate, so an
escape hatch is needed for development.

**Decision.** ``janus/core/prove/gates.py::evaluate`` always returns exactly four
``GateResult``s in a stable order: ``suite_green``, ``mutation_non_regression``,
``coverage_non_regression``, ``test_files_unchanged``. The suite gate tracks the boolean;
mutation passes only when killed mutants **and** the mutant total are non-decreasing;
coverage passes when ``after >= before - 1e-9``; the test-hash gate compares the SHA-256
map of ``test_*.py`` / ``*_test.py`` under ``tests/``. A **missing** suite result, mutation
measurement or coverage value **fails closed** (``measured_value="unavailable"``), and
``all_passed`` requires at least one result and every result to pass. The only opt-out is
``mutation_gate_enabled = false``: the processor skips ``mutmut`` and rewrites that row to
``passed=True``, ``measured_value="disabled"``, ``baseline_value=None``, so the PR body and
comment show the gate as **disabled**, not as measured. The setting defaults to enabled.

**Alternatives.** Treat a missing measurement as a pass — an unmeasured claim becomes a PR.
Skip the mutation gate silently when it errors — indistinguishable from a real
non-regression. Remove mutation entirely — loses the strongest signal against a
test-shaped patch. Run mutation always — doubles sandbox time per attempt with no opt-out
for local work.

**Consequences.** (+) Every PR's verification table maps to a measurement or an explicit
``disabled``/``unavailable`` state. (+) A broken tool can only block a fix, never wave one
through. (−) A flaky or misconfigured coverage/mutation command fails the fix even when the
patch is correct; the operator must fix the measurement. *Reversible:* yes (flip
``mutation_gate_enabled``).

### D-075 — A static policy gate rejects cheating diffs

**Context.** The measured gates answer "did the numbers hold?", not "did the patch cheat?".
A diff can keep the suite green by editing the test, adding ``|| true``, skipping the
failing case or lowering a coverage threshold. Those shapes are cheap to detect from the
diff text, before spending time on a sandbox run, and they must never reach a PR.

**Decision.** ``janus/core/prove/policy.py::check_policy`` parses the unified diff and
emits deterministic ``PolicyViolation``s, in a fixed rule order:
``edits_tests`` (a touched path looks like a test or is in the supplied test-file set),
``edits_protected`` (a path in ``protected_files``, e.g. CI or coverage config),
``weakens_ci`` (an added ``continue-on-error``, ``|| true`` or ``set +e``),
``skips_tests`` (an added skip/xfail marker across pytest, unittest, Jest/Vitest, Go and
JUnit), ``lowers_threshold`` (an added coverage threshold below one the diff removes),
``removes_assertions`` (a removed ``assert`` with no corresponding added one) and
``weakens_tests`` (more deleted than added test lines). It is **static**: no tests, mutators
or coverage tools run. The processor checks it before applying a diff; a violation is fed
back to the next attempt, and a diff that only ever violates the policy ends as
``rejected`` with **no PR**. Violations are rendered on the suggest surface but are not
persisted in ``gate_results`` (that table is for measured gates).

**Alternatives.** Rely on the four measured gates — a test edit that keeps coverage and
mutation flat can still pass them. Use an LLM to judge the diff — nondeterministic, and the
cheater is the same model. Add a new sandbox tool — more surface and more time for a
text-only check. Reject any diff touching a test — the policy already does that; the other
rules cover non-test cheating.

**Consequences.** (+) Known cheating shapes are refused cheaply and before any measurement
is trusted. (+) Stable rule ids make the feedback and the tests precise. (−) A legitimate
change that touches a test file (for example fixing a genuinely wrong test) is refused; S2
is source-only by design. (−) A novel cheating shape not in the rule set still depends on
the measured gates. *Reversible:* yes.

### D-076 — PR mode is draft-only and never merges or force-pushes

**Context.** Janus writes to a user's repository. The product guardrail is "PR-only: no
auto-merge, never force-push" (``PLAN.md`` §4), and a generated fix is unproven until a
human reviews it, however many gates passed. The default must be the least destructive
surface, and a failed push must not lose the analysis.

**Decision.** ``fix_mode`` defaults to **``suggest``**: the outcome is a comment on the PR,
or a neutral fix Check run when the run has no PR. A repository's ``.janus.yml`` may set
``mode: pr``; then only a **proven** fix is pushed to ``janus/fix-<job_id>`` (a normal
branch, no ``--force``) and opened as a **draft** PR via
``GitHubClient.create_pull_request(..., draft=True)``. There is no merge, approve or
force-push method in the client or the push helper, and tests assert the push argv contains
no force flag. Installation tokens travel only in the child git environment as an
``http.extraHeader`` (D-063), never in a URL or argv. A push or PR failure is logged and
falls back to the suggest comment. The PR body and suggest comment include the guardrail
line that Janus never merges and never force-pushes.

**Alternatives.** Open a ready-for-review PR — invites merging an unreviewed machine patch.
Auto-merge on all gates green — no human in the loop and no defence against a gate blind
spot. Force-push to keep the branch name stable across retries — destroys user work and is
explicitly forbidden. Make ``pr`` the default — the more destructive surface should be
opt-in.

**Consequences.** (+) The default cannot write a branch or open a PR. (+) Even in ``pr``
mode a human must mark the draft ready and merge it. (+) A push failure still yields the
analysis as a comment. (−) A proven fix needs one extra human action (mark ready) before it
can land. *Reversible:* yes (change the default), but the merge/force-push omission is
intentional.
