# Janus

When CI goes red, Janus decides whether the fault is your code, your tests, or your
pipeline. For code bugs it localizes the fault, writes a fix, proves it with gates, and
opens a PR.

> Full plan: [`PLAN.md`](PLAN.md) · Architecture & build order:
> [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) · Milestone spec:
> [`docs/specs/S0.md`](docs/specs/S0.md) · Component reference:
> [`docs/COMPONENTS.md`](docs/COMPONENTS.md) · First principles:
> [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · Design notes:
> [`docs/DECISIONS.md`](docs/DECISIONS.md)

## Status

The **backend pipeline** (D1–D3 of the backend spec) is built: idempotent ingest,
a Postgres-as-queue with leases/heartbeat/reaper, retries with full jitter, dead-letter
with admin requeue, cost caps, quotas and a kill switch. The triage brain (reproduce,
classify, fix, PR) is stubbed behind `JobProcessor` and tracked in
[`docs/FUTURE.md`](docs/FUTURE.md). Backend spec: [`docs/specs/BACKEND.md`](docs/specs/BACKEND.md).

## Requirements

- [uv](https://docs.astral.sh/uv/) (Python 3.12 is pinned via `.python-version`)
- Docker + Docker Compose (for Postgres)

## Quickstart

```bash
uv sync                          # create .venv and install locked deps
cp .env.example .env             # then edit if needed
docker compose up -d postgres    # start Postgres 16
uv run alembic upgrade head      # apply migrations
uv run uvicorn janus.app.main:app --reload
```

Then check liveness:

```bash
curl -s localhost:8000/health    # -> {"status":"ok"}
```

## Running the worker

The orchestrator consumes the `jobs` table. In a second terminal (run several for a pool):

```bash
uv run python -m janus.jobs.worker
```

Each worker claims with `SELECT ... FOR UPDATE SKIP LOCKED`, stamps a lease, heartbeats
while processing, and a reaper requeues jobs whose lease expired (so `kill -9` is safe).
It enforces full-jitter retries, cost caps, per-repo quotas and the kill switch.

Dead-letter admin endpoints:

```bash
curl -s localhost:8000/admin/dead
curl -s -X POST localhost:8000/admin/jobs/<id>/requeue
```

The processor is a no-op stub; the reproduce/triage/fix brain plugs in at S1/S2
(see `docs/FUTURE.md`).

## Quality

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy janus
uv run pytest -q
uv run pre-commit install        # optional: run the same checks on commit
```

The `SKIP LOCKED` concurrency test needs Postgres:

```bash
JANUS_TEST_DATABASE_URL=postgresql+asyncpg://janus:janus@localhost:5432/janus_test \
  uv run pytest -q
```

## Receiving webhooks locally

GitHub cannot reach `localhost`, so expose the app with a tunnel and point the GitHub
App's webhook URL at `POST /webhooks/github`:

- [smee.io](https://smee.io/) — create a channel and forward events to the local port.
- [ngrok](https://ngrok.com/) — `ngrok http 8000` and use the HTTPS URL.

The Phase 1 manifest flow (`/manifest/start`) creates the App and stores its
credentials; the webhook secret must match `JANUS_GITHUB_WEBHOOK_SECRET`.

To exercise signature verification and persistence without GitHub, post a signed
`workflow_run` payload with the bundled script:

```bash
JANUS_GITHUB_WEBHOOK_SECRET=<secret> \
  uv run python scripts/post_test_webhook.py --url http://localhost:8000/webhooks/github
```

## Installing the GitHub App (manifest flow)

1. Start a tunnel and set `JANUS_PUBLIC_URL` to its HTTPS URL in `.env`.
2. Visit `http://localhost:8000/manifest/start` in a browser. It submits the App
   manifest to GitHub.
3. Approve the App. GitHub redirects to `/manifest/callback`, which writes the private
   key to `JANUS_GITHUB_PRIVATE_KEY_PATH` and records the App id, webhook secret and
   OAuth credentials in `.env`.
4. Restart the app so the new settings load. Point the App's webhook at
   `<JANUS_PUBLIC_URL>/webhooks/github` if GitHub did not set it automatically.

## Scope & non-goals (S0)

**In scope:** manifest install, HMAC-verified webhooks, log + last-green→first-red diff
fetch, Check run, Postgres schema for installations/repositories/runs/jobs/diagnoses.

**Non-goals until S0 is demoed:** sandbox reproduction, triage scoring, the CODE_BUG
fix loop, PR mode, and every deferred item in `PLAN.md` §3.
