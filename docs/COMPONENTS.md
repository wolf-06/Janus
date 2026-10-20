# Janus — Component Reference

How each component works, why it is shaped that way, and how the pieces flow into one
another. For the first-principles framing see [`ARCHITECTURE.md`](ARCHITECTURE.md); for
the reasoning behind individual choices see [`DECISIONS.md`](DECISIONS.md). This document
is the "working manual": it explains behaviour, not just structure.

Scope: **S0 (spine) + Phase 2 (orchestration) + Phase 3 (the sandbox, the adapter protocol and
the Python adapter) + Phase 4 / S1 (reproduce + triage) + Phases 5–7 / S2 (the CODE_BUG fix
loop and its surfaces)**. The benchmark harness, `git bisect`, the output-idempotency marker
and the backend D4–D6 work are still deferred.

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

`max_fix_retries` is carried here for future fix-retry accounting; the S2 loop is bounded
by `max_fix_attempts` instead.

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

`check_run.start_check_run` posts an `in_progress` Check run acknowledging a red build. It is
the acknowledgement the ingress makes before any work exists. The **triage verdict** is a
separate, later surface: `comment.py` renders the evidence (see §9). `pr.py` renders the S2
suggest comment and draft-PR body from the same measured facts (see §10).

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

## 8. `janus/core` + `janus/adapters` — the adapter layer

The core is language-agnostic; everything language-specific sits behind `janus/core/adapter.py`.
An adapter is a **pure translator** (D-061): it converts CI logs, sandbox results and tool
artifacts into the core's small types, and the core's intent back into commands. It owns no
sandbox and no policy — the core owns the runner, the K=5 loop, retries and caps.

### `janus/core/adapter.py` — the Protocol
Defines `Command(argv, artifacts)` and the language-neutral data (`TestTarget`, `TestOutcome`,
`CoverageContext`, `MutationResult`) plus the `Adapter` Protocol (parse failing tests; build
setup/test/coverage/mutation commands; parse results/coverage/mutation). Three contract rules
bind every adapter, and the conformance suite enforces them: **argv lists, never shell strings**;
**every parser is total** (malformed input yields `[]`/zeroed, never raises); **no policy**. A
command declares the absolute container paths it writes (`Command.artifacts`), so a structured
report leaves the container through the artifact channel (D-060), not stdout scraping.

### `janus/adapters/python/**` — the Python adapter
`PythonAdapter` composes three translators: `pytest_runner.py` (parse `FAILED`/`ERROR` node ids
from a CI log; a `--json-report` test command; parse outcomes from the report artifact),
`coverage.py` (one `--cov-context=test` run producing a JSON report; parse its `contexts` into
`CoverageContext`s — D-062), and `mutmut.py` (a mutation command; parse killed/survived from
stdout, since the cache path is unknown to the adapter). All artifact paths are under
`/var/tmp/janus` on the container's writable rootfs — **not** `/tmp`, which is a tmpfs the
artifact extractor cannot read back. `tests/conformance/python_case.py` registers the adapter
with the reusable conformance suite in `tests/conformance/`.

### `janus/source.py` — acquiring the checkout
`SourceSpec` names the repo, ref and installation; `SourceProvider` is a context-manager factory
yielding a checkout that is removed on exit. `LocalSourceProvider` wraps an existing directory;
`GitSourceProvider` clones over HTTPS with the installation token supplied as an
`http.extraHeader` **through the environment** (`GIT_CONFIG_*`), so the token never appears in a
URL or argv, with `GIT_TERMINAL_PROMPT=0` and a temp `HOME` for a deterministic environment
(D-063).

### `janus/jobs/processor.py` — the sandbox processor
`SandboxProcessor` (built by `build_processor`, selected by `processor_mode`) gathers evidence
for a job: it loads the repo/run rows, **acquires the source off the event loop** (one
`asyncio.to_thread` spans acquire → run → parse, and the source context always exits), builds the
sandbox phase specs from the adapter's commands (setup only when the sandbox allows the network;
the test phase offline), runs them with `SandboxRunner`, parses the test result with the adapter,
and returns a `JobResult`. It is evidence only — no triage, no scoring and no new tables; a
failing suite is the expected input, so only an infrastructure failure is retryable (D-064).

---

## 9. `janus/core` + `janus/report` (S1) — reproduce + triage

The S1 brain turns a red run into a diagnosis: rerun the failure, measure it, score seven
signals, and render the verdict. It is built on the frozen evidence types and the adapter
layer of §8 and adds the reproducer, the seven collectors, the scorer and the evidence
renderer. A `CODE_BUG` decision proceeds to the S2 fix loop (§10); `FLAKY`, `ENV` and
`UNSURE` stop at the verdict.

### `janus/core/plan.py` — adapter commands → a run plan
Both the processor and the reproducer turn an adapter's commands into the ordered sandbox
phases of one run, so that translation lives here once. `build_plan` emits a setup phase per
`Adapter.setup_commands()` **only when the sandbox allows the network** (a network-less setup
could install nothing), then the single offline test phase for the given targets (an empty
target list means "the whole suite"). Each phase carries its command's `argv` and `artifacts`,
so the plan is the one place a spec is materialised from; `phase_result` picks one phase's
result out of a run.

### `janus/core/reproducer.py` — the K reruns + one full-suite run
`Reproducer.reproduce(checkout, ReproduceRequest(targets, k, profile))` measures what happens
(D-065): it runs the failing-target set **K times** (one sandbox run per attempt), then the
whole suite **once**, and returns a `Reproduction`. K comes from `.janus.yml`/`Settings`
(`<= 0` clamped to 1). Each rerun is flattened to one `RunAttempt` per parsed outcome; the
full suite is one `RunAttempt` with `target_id = None`. A sandbox-level failure (no test
result, an `error`, or a timeout) records one failed attempt per requested target with
`sandbox_error` set — evidence, never a raise — so the determinism signal can see the sandbox
was unhealthy; a normal red suite is *not* a sandbox failure. `cost_minutes` sums every run's
duration. `ambient` currently runs the same clean specs and reports `ambient_reconstructed =
False` (CI-image reconstruction is not implemented). Blocking; a caller on the event loop runs
it in a thread.

### `janus/core/signals/**` — the seven collectors
Each collector is a class with a `name` and `collect(ctx) -> SignalEvidence | None`, returning
`None` to **abstain** (no evidence available) rather than voting zero. `default_collectors()`
returns all seven in `SIGNAL_NAMES` order and `collect_all` runs them, dropping abstentions and
logging-and-skipping a collector that raises, so one bad signal cannot break triage. Each
`collect` reads the `SignalContext` (`reproduction`, `change`, `failing_targets`,
`flake_history`, `metadata`):

| Signal | Reads | Votes |
|---|---|---|
| `determinism` | the K attempts | `CODE_BUG` 0.9 when every rerun failed, `FLAKY` 0.9 when intermittent; abstains when all passed or there were no reruns. |
| `signatures` | stdout/stderr excerpts | `ENV` 0.85 for timeout / `ECONNRESET` / 429-503 / `ENOSPC` / OOM / `SIGKILL` / DNS strings; `CODE_BUG` 0.6 for assertion-style failures; abstains when nothing matches. |
| `exit_codes` | exit codes | `ENV` 0.8 for 137 / 124 (kill/timeout), `CODE_BUG` 0.6 for 1 / 2; abstains when nothing is classifiable. |
| `resources` | cgroup telemetry + duration spread | `ENV` 0.9 on an OOM kill, 0.7 near the memory cap, 0.4 on an order-of-magnitude stall; else weakly `CODE_BUG` 0.3. |
| `change_attribution` | the last-green → first-red diff | `CODE_BUG` 0.85 when a changed file is a failing test's module; `ENV` 0.4 when the diff touches nothing related; abstains with no baseline. |
| `correlation` | `ctx.metadata` CI facts | `ENV` 0.6 for a cache miss, a retry of a failing job, a runner change, or a matrix leg that only fails there; abstains otherwise. |
| `flake_history` | historical failure rates | `FLAKY` 0.8 at ≥ 0.30, `CODE_BUG` 0.5 when every target is ≤ 0.05; abstains in between or with no history. |

Every collector's `votes` pass through `clamp_votes`, so a vote on a route outside the three
real ones (`UNSURE`) is dropped and the rest are clamped to `[0, 1]`.

### `janus/core/triage/scorer.py` — votes → scores → a route
`aggregate` is a **weighted average over the signals that reported**: for each real route it
divides `sum(weight × vote)` by the sum of the **present** signals' weights, so an abstaining
collector does not dilute the score, and a signal whose name is not a weight is ignored
(D-067). `UNSURE` is never a key of the aggregate. `score` aggregates, builds one bounded
reason per signal (plus "no signal evidence was collected" when there is none), and delegates
to `routes.decide`. `decide` takes the argmax over `TRIAGE_ROUTES` (ties resolve to the earlier
route), and returns `UNSURE` when the top score is below `tau` or the top two are within
`epsilon`; otherwise the argmax route. `confidence` is the top score, `top2_gap` the top-two
difference.

### `janus/report/comment.py` — the evidence renderer
Pure and total, no I/O. `render_evidence` builds the Markdown comment: the route/confidence
heading, the per-attempt table, the full-suite line, the change-attribution section (or an
explicit "baseline could not be resolved" note), the per-signal summary/votes table, the rule's
reasons, and a per-route "next step" line — bounded by `MAX_COMMENT_CHARS` with a truncation
note. `render_check_output` builds the Check-run `output` (`title`/`summary`/`text`) from the
same body minus the change section. `check_conclusion` maps a route to the Check-run
conclusion: `CODE_BUG` → `action_required`, everything else → `neutral`.

### Pipeline order in `janus/jobs/processor.py`
`SandboxProcessor.__call__` runs the S1 pipeline for one claimed job (D-068):

1. Load the repository and run rows; a missing row is a non-retryable failure.
2. Fetch the failing run's CI log, unzip it (`janus/github/logs.py::extract_log_text`) and
   parse the failing targets with the adapter — a fetch/parse failure degrades to "no targets".
3. Read the per-target `flake_history` in a short-lived session.
4. In **one** `asyncio.to_thread` (the source context is entered and exited inside it):
   acquire the checkout, load the repo's `.janus.yml`, reproduce, resolve the last-green →
   first-red change, build the `SignalContext`, run the collectors, and score — returning a
   plain `_PipelineResult`.
5. A sandbox-level failure (no full-suite result, or a `sandbox_error` on it) is a **retryable**
   failure carrying the measured cost; a normal red suite is not.
6. Persist the `reproductions` rows, the `signals` rows and the `diagnoses` row in one
   transaction; a persistence failure is retryable.
7. Post the "Janus triage" Check run, and a PR comment when `runs.pr_number` is set and
   `comment_on_pr` is on. Posting is best-effort.
8. A `CODE_BUG` decision runs the S2 fix loop (§10) inside the same source context; its
   outcome is persisted and surfaced after the triage post.

### `tests/fixtures/sample_repo/` — the S1 seed repository
A tiny stdlib-only Python repo the sandbox builds and runs: one deterministic logic bug
(`src/calc.py::sum_to` off by one → `CODE_BUG`), a driven flake (`JANUS_SAMPLE_FLAKE` chooses
pass/fail → `FLAKY`), an ENV case (`JANUS_SAMPLE_ENDPOINT` unset → an infrastructure signature
→ `ENV`), and an always-green control. It carries a `requirements.txt` (empty) and an example
`.janus.yml`. Its tests are meant to fail, so `norecursedirs` in `pyproject.toml` keeps pytest
from collecting them as part of Janus's own suite (asserted by `test_fixture_isolation.py`).
`tests/integration/test_sample_repo.py` runs the logic bug and the control through the adapter
in the sandbox (Docker-gated).

---

## 10. `janus/core` + `janus/report` (S2) — the CODE_BUG fix loop

S2 turns a `CODE_BUG` diagnosis into a source-only diff and tries to prove it. Only
`CODE_BUG` enters: `FLAKY`, `ENV` and `UNSURE` are comment-only per S1. The loop runs
**inside the source context**, in the same `asyncio.to_thread` block as reproduction, since
every step needs the checkout on disk. The spec is [`specs/S2.md`](specs/S2.md); the ADRs are
D-071…D-076.

### `janus/core/localize/` — SBFL (and a deferred bisect planner)
`sbfl.py::localize(contexts, failing_test_ids, formula=OCHIAI, max_suspects=5)` ranks
`file:line` suspects from **per-test** coverage. A context whose `test_id` is failing
contributes `ef`; every other contributes `ep`; `nf`/`np` are the totals minus coverage.
Ochiai is the default (`ef / sqrt((ef + nf) * (ef + ep))`), DStar is an option. A line with
`ef == 0` is not a suspect, duplicate contexts count once, ties break by file then line, and
the list is capped. `bisect.py` is a **pure** planner/parser (`plan_bisect`,
`parse_bisect_output`, `available`); it executes nothing, and `bisect_enabled` is off
because the checkout has `--depth 1` (D-070). `introducing_commit` therefore stays `NULL`.

### `janus/core/retrieve/git_grep.py` — git-log retrieval
No vector index (D-072). `build_terms` turns failing test ids, suspect basenames and
error-line tokens into a small deterministic term set, dropping timestamps, paths, line
numbers and hashes. `git_log_argv` builds a shell-free `git log --oneline -p
--max-count=50 -G<term>` argv; `parse_git_log` yields `PriorFix` rows with a bounded diff
excerpt; `rank` scores by distinct matching terms. The processor keeps the top five and the
prompt at most three. A git failure degrades to no prior fixes.

### `janus/core/generate/` — prompt and LLM
`prompt.py::build_fix_prompt` assembles diagnosis, failure excerpts, suspects, relevant
source, prior fixes and the hard rules, capped at 24,000 characters with source truncated
first. `extract_diff` accepts a fenced diff or a bare unified diff and returns `None` for
prose. `llm.py` defines the `LLMClient` protocol, `NullLLM` and
`OpenAICompatibleClient` (`POST {base_url}/chat/completions`). The key is a `Bearer` header
only — never in a URL, argv, error or log (D-073). `build_llm` needs both `llm_api_key` and
`llm_base_url`; otherwise the loop returns `no_llm` and the result degrades to a comment.

### `janus/core/prove/` — policy and the four gates
`policy.py::check_policy` is the **static** gate: it rejects test/protected-file edits,
`continue-on-error` / `|| true` / `set +e`, skip/xfail markers, lowered coverage thresholds,
removed assertions and net test-line deletion (D-075). `gates.py::evaluate` always returns
four `GateResult`s: `suite_green`, `mutation_non_regression`, `coverage_non_regression`,
`test_files_unchanged`. A missing suite/mutation/coverage measurement **fails closed**;
`all_passed` requires at least one result and every result to pass. With
`mutation_gate_enabled = false`, the mutation row is `passed=True` with
`measured_value="disabled"` so it is visibly disabled (D-074).

### `janus/report/pr.py` — the surfaces
Pure renderers: `render_pr_body` (diagnosis, reproduction, change, root cause/suspects,
diff, four-gate verification table, policy result, guardrail), `render_suggest_comment`
(the same facts plus an explicit "why no PR" for suggest mode or a failed proof),
`render_pr_title` and `branch_name` (`janus/fix-<job_id>`). Both documents are bounded at
20,000 characters.

### `janus/github/push.py` — commit and push
`commit_and_push` runs `checkout -B`, `add -A`, `commit`, `push` with a bounded timeout. The
installation token is passed only through `GIT_CONFIG_*` in the child environment; it never
appears in argv, a remote URL or an error. Unsafe branch names are rejected. There is no
force flag.

### Exact order inside `SandboxProcessor._run_fix`
Called only for `Route.CODE_BUG`, with the checkout, targets, reproduction, decision and
repo config:

1. Run the adapter's coverage command on the checkout and parse per-test contexts; none →
   `no_localization`.
2. Collect failing ids from the CI targets and the rerun attempts, then `localize`; no
   suspects → `no_localization`.
3. `build_terms` + `git_log_argv` + `_run_git` + `parse_git_log` + `rank`, keeping the top
   five prior fixes (best-effort).
4. Read the suspect source files, bounded and confined to the checkout.
5. Build the LLM (`self._llm` in tests, else `build_llm(settings)`); unavailable →
   `no_llm` with suspects and prior fixes.
6. For up to `max_fix_attempts` (default 3): build the prompt, append the last three
   feedback messages, call the model, `extract_diff`, run `check_policy`, copy the checkout
   to a temp directory, `git apply --check` then `git apply`, and run the four gates. All
   four passing → **`proven`**, retaining the patched copy for the async push. Otherwise
   keep the best attempt by gates-passed count and feed the failure back.
7. Return `proven`, `gates_failed`, `rejected` (only policy refused every diff), `no_diff`,
   `no_llm`, `no_localization` or `error`.

### Persisted and posted per outcome
The blocking thread returns a `FixOutcome`; the async half persists it in the S1
transaction and then posts. `_persist_fix` writes:

- `suspects` (`job_id`, `file`, `line`, `score`, `rank`) whenever localization found any;
- `fixes` (`job_id`, top `suspect_id`, `attempt`, `diff`, `status`) whenever a diff exists,
  even if it was rejected or failed its gates;
- `gate_results` (`fix_id`, `gate`, `passed`, `measured_value`, `baseline_value`) for the
  recorded fix's measured gates.

Policy violations and prior fixes are surface evidence and are not persisted in those
tables. `no_localization` writes nothing; `no_diff`, `no_llm` and `no_localization` have no
`fixes` row (suspects may still exist).

| Outcome | Rows | Surface |
|---|---|---|
| `proven` | suspects, one fix (`proven`), four gate rows | `pr` mode: push `janus/fix-<job>` and open a **draft** PR; otherwise the suggest comment |
| `gates_failed` | suspects, best fix, its gate rows | suggest comment (why the proof failed) |
| `rejected` | suspects, last fix | suggest comment (policy violations) |
| `no_diff` | suspects only | suggest comment ("No diff was produced.") |
| `no_llm` | suspects only | suggest comment |
| `no_localization` | none | none beyond the triage verdict |
| `error` | suspects, plus a fix only if a diff existed | suggest comment |

In suggest mode the comment is posted on the run's PR when it has one; otherwise the same
body goes into a neutral Check run named `"<triage check name>: fix"`. In `pr` mode a
push/PR failure is logged and falls back to the suggest comment. The triage Check run and
evidence comment are posted first and unconditionally; posting is best-effort. The retained
patched checkout is removed in `_cleanup_fix_workdir` on every path. Branch pushes are
normal (never forced), PRs are draft-only, and no merge/approve path exists (D-076).

---

## 11. Cross-cutting behaviours

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

## 12. Still stubbed / deferred

See [`FUTURE.md`](FUTURE.md) for the full ledger. In short:

- **Benchmark harness (Phase 8):** the BugsInPy subset harness and metrics table (`bench/`).
- **`git bisect`:** `janus/core/localize/bisect.py` is only a planner/parser; the source
  clone is `--depth 1`, so there is no history to bisect (D-070) and
  `suspects.introducing_commit` stays `NULL`.
- **Output idempotency marker:** `janus/report/markers.py` (needed before the brain posts
  comments/checks/PRs without risking duplicates).
- **Backend D4–D6:** fairness/rate limits/budgets (`janus/jobs/{ratelimit,budgets}.py`),
  dedup, observability (`janus/observability/`), load test (`bench/`), Redis, and
  `docs/DESIGN.md`.

The worker calls the brain via `JobProcessor`; S0–S2 are implemented, and these remaining
items are the tracked follow-ups.
