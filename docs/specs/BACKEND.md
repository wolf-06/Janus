# Janus — Backend Pipeline Spec (D1–D6)

Source: `JANUS_BACKEND_SPEC-7def8ad9.md.txt`. This spec covers the durable, at-least-once,
idempotent pipeline. The triage brain is unchanged and sits behind
`janus.jobs.worker.JobProcessor`.

Status: **D1–D3 implemented** (this document reflects the built system). D4–D6 are
outlined in [`../FUTURE.md`](../FUTURE.md).

---

## 1. Goal & non-goals

**Goal:** handle webhook bursts from many repos with no lost events, no duplicate work,
bounded cost, and honest observability.

**Non-goals:** no microservices/Kafka/Kubernetes; two deployables (`api`, `worker`) from
one codebase; no exactly-once (at-least-once + idempotent effects); one Postgres.

---

## 2. Ingest (`api`)

`POST /webhooks/github`:

1. Verify `X-Hub-Signature-256` over the raw body (constant-time). Bad → **401**.
2. Only `workflow_run` with `action=completed` and a failure conclusion proceeds;
   everything else → **202 ignored**.
3. Backpressure: if queued depth ≥ `JANUS_QUEUE_DEPTH_LIMIT` → **503 + `Retry-After`**.
4. In **one transaction**: `INSERT ... ON CONFLICT DO NOTHING` into `deliveries`, then
   create the `run` and `enqueue` the job. Duplicate delivery → **200** (no enqueue).
   Accepted → **202**.
5. Baseline resolution and the acknowledgement Check run happen **after** the commit,
   best-effort, on a worker thread (the GitHub SDK is sync).

No triage work runs in the request path.

---

## 3. Queue & worker

**Claim** (`janus/jobs/queue.py`):

```sql
SELECT ... WHERE state='queued' AND (run_after IS NULL OR run_after <= now())
ORDER BY run_after NULLS FIRST, id
FOR UPDATE SKIP LOCKED LIMIT 1
```

The same statement moves the row to `running` and stamps `locked_by` +
`lease_expires_at`.

**Transaction discipline** (`janus/jobs/worker.py`): claim commits in a short
transaction; processing runs **outside** any transaction; a background task heartbeats
the lease; finalization is another short transaction. A `kill -9` mid-processing therefore
leaves a `running` row whose lease expires — never a half-open transaction or a lost job.

**Leases** (`janus/jobs/lease.py`): heartbeat extends `lease_expires_at` every
`JANUS_WORKER_HEARTBEAT_INTERVAL_SECONDS`; `reap_expired` returns running jobs whose lease
expired to `queued` (without consuming an attempt). `SKIP LOCKED` keeps two reapers from
double-requeuing.

**Retries:** on a retryable failure with `attempts < max_attempts`, the job returns to
`queued` with `run_after = now + rand(0, min(cap, base·2^attempts))` (full jitter). Any
other failure → `dead`.

**States** (`janus/jobs/state.py`): `queued → running → succeeded | failed | dead |
skipped_budget`, plus `running → queued` (retry / reaped). Terminal: succeeded, failed,
dead, skipped_budget.

**Guardrails** (`janus/jobs/caps.py`): per-job cost caps → `skipped_budget`; per-repo
rolling-window quota → `skipped_budget`; global kill switch (DB `system_flags`, plus a
`JANUS_KILL_SWITCH` env hard override).

**Dead-letter:** `GET /admin/dead`, `POST /admin/jobs/{id}/requeue`.

---

## 4. Data

- `deliveries(delivery_id PK, repo_id, event, received_at)` — ingest idempotency.
- `jobs(id, delivery_id, repo_id, run_id, fingerprint, state, attempts, max_attempts,
  run_after, locked_by, lease_expires_at, last_error, cost_minutes, llm_tokens,
  created_at, updated_at, finished_at)`.
- Partial unique index `uq_jobs_active_fingerprint` on `fingerprint` where
  `state IN ('queued','running')` (used by D5).
- Async engine (`asyncpg`); Alembic uses a sync `psycopg` URL derived from
  `JANUS_DATABASE_URL`.

---

## 5. Data-integrity invariants

| # | Invariant | Where it is proven |
|---|---|---|
| 1 | A delivery is processed at most once to enqueue | `deliveries` PK + `record_delivery`; `test_webhook_flow::test_redelivery_is_idempotent` |
| 2 | A job is never run concurrently by two workers | `SKIP LOCKED`; `test_queue_concurrency` (Postgres) |
| 3 | A worker crash leaves exactly one visible effect | lease + reaper; `test_lease::test_reaper_requeues_expired_lease` |
| 4 | A retry never posts a duplicate comment/check | output marker — **deferred**, see FUTURE §D |
| 5 | A Redis flush loses no jobs | Redis is not on the critical path (deferred until D4) |

---

## 6. Acceptance (D1–D3)

- Replay the same delivery 100× → one job. ✔ (`test_redelivery_is_idempotent`)
- `kill -9` a worker mid-job → job completes once, no duplicate output. ✔ (lease + reaper;
  output marker deferred)
- Transient faults recover; permanent faults land in `dead` with a reason. ✔
  (`test_worker` retry/dead tests)
- `GET /admin/dead` lists dead jobs; requeue returns one to `queued`. ✔

---

## 7. Verification commands

```bash
uv sync
docker compose up -d postgres
uv run alembic upgrade head
uv run ruff check . && uv run ruff format --check .
uv run mypy janus
uv run pytest -q
JANUS_TEST_DATABASE_URL=postgresql+asyncpg://janus:janus@localhost:5432/janus_test uv run pytest -q
uv run uvicorn janus.app.main:app --reload    # api
uv run python -m janus.jobs.worker            # worker
```
