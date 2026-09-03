# Janus

When CI goes red, Janus decides whether the fault is your code, your tests, or your
pipeline. For code bugs it localizes the fault, writes a fix, proves it with gates, and
opens a PR.

> Full plan: [`PLAN.md`](PLAN.md) · Architecture & build order:
> [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) · Milestone spec:
> [`docs/specs/S0.md`](docs/specs/S0.md) · Design notes:
> [`docs/DECISIONS.md`](docs/DECISIONS.md)

## Status

Phase 0 (scaffold) is complete; Phase 1 (S0 GitHub App spine) is in progress. S0 ends at
a local webhook → Check run on a real red build. Reproduce, triage and fix (S1/S2) are
out of scope until the S0 demo passes.

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

## Quality

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy janus
uv run pytest -q
uv run pre-commit install        # optional: run the same checks on commit
```

## Receiving webhooks locally

GitHub cannot reach `localhost`, so expose the app with a tunnel and point the GitHub
App's webhook URL at `POST /webhooks/github`:

- [smee.io](https://smee.io/) — create a channel and forward events to the local port.
- [ngrok](https://ngrok.com/) — `ngrok http 8000` and use the HTTPS URL.

The Phase 1 manifest flow (`/manifest/start`) creates the App and stores its
credentials; the webhook secret must match `JANUS_GITHUB_WEBHOOK_SECRET`.

## Scope & non-goals (S0)

**In scope:** manifest install, HMAC-verified webhooks, log + last-green→first-red diff
fetch, Check run, Postgres schema for installations/repositories/runs/jobs/diagnoses.

**Non-goals until S0 is demoed:** sandbox reproduction, triage scoring, the CODE_BUG
fix loop, PR mode, and every deferred item in `PLAN.md` §3.
