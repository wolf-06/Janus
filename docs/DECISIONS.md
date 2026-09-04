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

## Phase 1 decisions

Added as Phase 1 is built (schema, webhook verification, auth, client, manifest,
baseline diff). See the entries below this line as they land.
