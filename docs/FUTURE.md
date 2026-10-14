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

- **Sandbox runner (Phase 3):** *done* — `janus/sandbox/{settings,spec,image,allowlist,`
  `proxy,redaction,profiles,telemetry,runner}.py` with TOML-configured limits and two-phase
  runs. Every run builds a fresh image from the repository Dockerfile or a Janus-generated
  one, content-addressed and removed afterwards (ADR D-056); output is streamed and bounded
  (D-058); rootless Docker is supported via `docker_host`/`require_rootless` (D-057); the host
  allowlist is enforced with an internal network plus an allowlisting proxy (D-059); and
  secrets are redacted.
- **Adapter interface (Phase 3):** *done* — `core/adapter.py` Protocol, the adapter
  conformance suite (`tests/conformance/`), and the Python adapter (pytest JSON report,
  coverage.py contexts, mutmut); the sandbox is wired into `JobProcessor` and selected by
  `processor_mode` (ADR D-061…D-064).
- **Reproducer (Phase 4):** *done* — `janus/core/reproducer.py` reruns the failing-target
  set K times (one sandbox run per attempt) then runs the full suite once; `clean` and
  `ambient` profiles (ambient currently falls back to clean specs and records
  `ambient_reconstructed = False`). See ADR D-065.
- **7 signal collectors + scorer (Phase 4):** *done* — `janus/core/signals/`
  (determinism, signatures, exit codes, resources, change attribution, correlation, flake
  history) and `janus/core/triage/scorer.py` (weighted average over the **present**
  signals) → routes `CODE_BUG`/`FLAKY`/`ENV`/`UNSURE` with `tau`/`epsilon`. See ADR
  D-066/D-067.
- **Evidence comment + `.janus.yml` (Phase 4):** *done* — `janus/report/comment.py`
  (`render_evidence`, `render_check_output`, `check_conclusion`) and
  `janus/config/janus_yml.py` (`RepoConfig`, `parse_repo_config`, `load_repo_config`). See
  ADR D-068/D-069.
- **Pipeline wiring (Phase 4):** *done* — `janus/jobs/processor.py` fetches the CI log,
  parses the failing tests, checks out the revision, reproduces, resolves the change,
  collects and scores, persists, and posts a "Janus triage" Check run (+ a PR comment when
  the run has a PR); `_maybe_fix` is the documented S2 seam.
- **Sample fixture repo (Phase 4):** *done* — `tests/fixtures/sample_repo/` holds a
  deterministic logic bug, a driven flake, an ENV case and an always-green control, plus a
  `requirements.txt` and an example `.janus.yml`; `norecursedirs` keeps pytest from
  collecting it as part of Janus's own suite.
- **`git bisect` (Phase 5 / S2):** *deferred* — `Settings.bisect_enabled` defaults to
  `false`. The source provider clones with `git fetch --depth 1`, so the checkout has no
  history to bisect; SBFL over the per-test coverage contexts is the localizer. See ADR
  D-070.
- Plugs into: `janus/jobs/worker.py` `JobProcessor` (replaces `stub_processor`).

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
