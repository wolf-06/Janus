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
