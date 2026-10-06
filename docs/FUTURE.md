# Janus — Deferred Work (Future TODO)

Everything intentionally **not** built yet, so the full product plan can be completed
later without re-discovery. The current build is the **backend pipeline** (durable,
at-least-once, idempotent) with the triage brain stubbed behind
`janus.jobs.worker.JobProcessor`.

Ordering note: the backend track (D4–D6) is independent of the triage brain; the triage
brain (S1/S2) plugs into the worker without changing the pipeline.

---

## A. Backend track — remaining milestones

### D4 — Fairness, rate limits, budgets (Phase 2F)
- Fairness in the claim query: prefer the repo with the **fewest running jobs**; cap
  concurrent jobs per repo (default 2).
- Outbound GitHub **token bucket per installation** in Redis (Lua, atomic); honour
  `X-RateLimit-Remaining` / `Retry-After`; a 429 requeues with `run_after` **without**
  burning an attempt.
- Per-repo **daily budget** for sandbox minutes and LLM tokens → `skipped_budget` +
  comment. Tables/columns already exist (`jobs.cost_minutes`, `jobs.llm_tokens`); add a
  `budget_usage` table.
- **Accept:** a 500-job noisy repo does not delay a quiet repo beyond a stated bound; a
  429 storm loses zero jobs.
- Plugs into: `janus/jobs/queue.py` (claim ordering), new `janus/jobs/ratelimit.py`,
  `janus/jobs/budgets.py`, `janus/github/ratelimit.py`.

### D5 — Failure dedup (Phase 4D)
- `fingerprint = sha256(repo_id + workflow + normalized failing test ids + normalized top
  error lines)`; normalization strips timestamps, paths, hashes, line numbers.
- Reuse a cached verdict when the same fingerprint was triaged within T hours and the
  commit range is unchanged; post a short "same failure as <run>" comment.
- The **partial unique index already exists** (`uq_jobs_active_fingerprint`,
  `jobs.fingerprint`); populate `fingerprint` at ingest and add the cache lookup + metric.
- **Accept:** 100 identical failures → 1 triage + 99 cache hits; no false merges.
- Depends on: log parsing (needs failing test ids / error lines) — can be stubbed with a
  regex parser before the full triage brain exists.

### D6 — Observability + load test (Phase 7O)
- OpenTelemetry traces (one per delivery; spans ingest/queue-wait/claim/log-fetch/
  sandbox/LLM/post; trace id on the job row — add `jobs.trace_id`).
- Prometheus metrics (`jobs_enqueued`, `jobs_by_state`, `queue_age_seconds`,
  `job_duration_seconds` by stage, `retries_total`, `dead_total`, `dedup_hits`,
  `github_429_total`, `lease_reaped_total`) + `janus/observability/`.
- Structured JSON logs already exist; add `delivery_id`/`job_id`/`repo_id` context.
- Grafana dashboard JSON + screenshot.
- `locust`/`k6` load test with mock GitHub + stub triage → `bench/RESULTS.md`.
- **Accept:** measured ingest p50/p95/p99, queue-age p95, time-to-drain,
  throughput-vs-workers, **0 lost / 0 duplicated jobs**.
- Compose services to add: `api`, `worker`, `redis`, `prometheus`, `grafana`.

### Backend infrastructure still deferred
- **Redis 7** (optional wake-up + rate-limit counters; polling fallback required). Not on
  the critical path — a flush must lose no jobs.
- **testcontainers** for DB-touching integration tests (currently SQLite for units and a
  Postgres-gated test for SKIP LOCKED).
- **`LISTEN/NOTIFY`** wake-up to avoid tight polling.
- **`docs/DESIGN.md`** (decisions, "Known limits", interview-defense answers).

---

## B. Product brain (S1) — reproduce + triage

- **Sandbox runner (Phase 3):** *done* — `janus/sandbox/{settings,spec,allowlist,redaction,`
  `profiles,telemetry,runner}.py` with TOML-configured limits, two-phase runs, `docker cp`,
  resource sampling, and secret redaction. Remaining for Phase 3: rootless-Docker support
  (currently plain Docker; see `docs/specs/SANDBOX.md` §7), the network allowlist *proxy*
  (the policy exists, enforcement does not), and the Janus base image build.
- **Adapter interface (Phase 3):** `core/adapter.py` Protocol, the adapter conformance
  suite, and the Python adapter (pytest, coverage.py contexts, mutmut); then wire the
  sandbox into `JobProcessor`.
- **Reproducer (Phase 4):** K=5 reruns of failing targets + one full-suite run; `clean`
  and `ambient` profiles.
- **7 signal collectors + scorer:** determinism, signatures, exit codes, resources, change
  attribution, correlation, flake history → routes `CODE_BUG`/`FLAKY`/`ENV`/`UNSURE` with
  `tau`/`epsilon`.
- **Evidence comment** (`report/comment.py`) and `.janus.yml` per-repo config
  (`config/janus_yml.py`).
- Plugs into: `janus/jobs/worker.py` `JobProcessor` (replace `stub_processor`).

## C. Fix loop (S2) — localize, retrieve, generate, prove, PR

- **Localize:** SBFL (Ochiai/DStar) over per-test coverage → ranked `file:line`; sandbox
  `git bisect`.
- **Retrieve:** `git log -p` grep on the fault signature.
- **Generate:** LLM minimal source-only unified diff (≤3 retries with verifier feedback).
- **Prove:** suite green, mutation non-regression, coverage non-regression, test-file hash
  lock + policy gate.
- **PR mode:** `.janus.yml` `mode: pr|suggest` (default suggest); PR-only, no auto-merge,
  no force-push.
- **Benchmark (Phase 8):** BugsInPy subset harness + metrics table.

## D. Output idempotency marker (needed by D2/D3 in production)
- Check runs/comments must carry a hidden marker with the job's idempotency key, and the
  reporter must look for it and **update instead of duplicate** — this closes the "crash
  between post and mark-succeeded" window. The `report/` layer currently posts
  unconditionally; add `report/markers.py`.

---

## E. Explicitly out of scope (product `PLAN.md` §3 — do not start before S2 demo)
Flake quarantine path, pipeline self-heal/chaos harness, graduation mechanic, trained
triage classifier, Node adapter, Temporal, vector RAG, dashboard, gVisor, hosted
deployment.
