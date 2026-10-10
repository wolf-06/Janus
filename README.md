# Janus

When CI goes red, Janus decides whether the fault is your code, your tests, or your
pipeline. For code bugs it localizes the fault, writes a fix, proves it with gates, and
opens a PR.

> Full plan: [`PLAN.md`](PLAN.md) · Architecture & build order:
> [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) · Milestone specs:
> [`docs/specs/S0.md`](docs/specs/S0.md) · [`docs/specs/BACKEND.md`](docs/specs/BACKEND.md) ·
> [`docs/specs/SANDBOX.md`](docs/specs/SANDBOX.md) · Component reference:
> [`docs/COMPONENTS.md`](docs/COMPONENTS.md) · First principles:
> [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · Design notes:
> [`docs/DECISIONS.md`](docs/DECISIONS.md)

## Status

The **backend pipeline** (D1–D3 of the backend spec) is built: idempotent ingest,
a Postgres-as-queue with leases/heartbeat/reaper, retries with full jitter, dead-letter
with admin requeue, cost caps, quotas and a kill switch. The **sandbox core** (Phase 3) is
built too: TOML-configured, two-phase container runs with `docker cp`, resource limits,
telemetry and secret redaction. The **adapter layer** is built: the `Adapter` protocol, its
conformance suite and the Python adapter. The triage brain (reproduce, classify, fix, PR) is
stubbed and tracked in [`docs/FUTURE.md`](docs/FUTURE.md). Backend spec:
[`docs/specs/BACKEND.md`](docs/specs/BACKEND.md); sandbox spec:
[`docs/specs/SANDBOX.md`](docs/specs/SANDBOX.md); adapter spec:
[`docs/specs/ADAPTER.md`](docs/specs/ADAPTER.md).

## Requirements

- [uv](https://docs.astral.sh/uv/) (Python 3.12 is pinned via `.python-version`)
- Docker + Docker Compose (for Postgres; also required by the sandbox)

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

The processor is a no-op stub by default; the reproduce/triage/fix brain plugs in at S1/S2
(see `docs/FUTURE.md`).

## Adapter and processor

The core is language-agnostic: everything language-specific sits behind the `Adapter` protocol in
`janus/core/adapter.py`, which turns CI logs, sandbox results and tool artifacts into neutral types
and the core's intent into argv commands. An adapter is a **pure translator** — it holds no sandbox
and no policy. The **Python adapter** (`janus/adapters/python/`) is implemented: pytest (JSON
report), coverage.py contexts and mutmut, with structured reports read back as container artifacts
under `/var/tmp/janus`.

By default the worker runs the no-op `stub` processor. Set `processor_mode = "sandbox"` (in
`janus.toml`, or `JANUS_PROCESSOR_MODE=sandbox`) to make the worker run real work in a container:
acquire the source, run the adapter's commands, parse the outcomes. It gathers evidence only —
triage arrives in Phase 4.

## Sandbox configuration

Sandbox behaviour is configured in [`janus.toml`](janus.toml) (or `[tool.janus]` in
`pyproject.toml`; set `JANUS_CONFIG_FILE` to use another path). Environment variables
override the file, with nested keys using a double underscore, e.g.
`JANUS_SANDBOX__IMAGE=python:3.12-slim`.

```toml
[sandbox]
image_source = "auto"          # auto | repo | generated | prebuilt (see ADR D-056)
image = "python:3.12-slim"     # the "prebuilt" image, and the generated base by default
# base_image = "python:3.12-slim"  # override the generated base (unset => use `image`)
tooling_packages = ["pytest", "pytest-json-report", "coverage", "mutmut"]
remove_image = true            # remove the per-run image after the run
build_network = true           # allow the build network (needed to install packages)
build_timeout_seconds = 900.0  # wall-clock cap for one image build
timeout_seconds = 900.0        # wall-clock cap for one command
memory_mb = 2048
cpus = 2.0
pids_limit = 256
max_output_bytes = 1048576     # per-command stdout/stderr kept
telemetry_sample_interval_seconds = 0.2
# docker_host = "unix:///run/user/1000/docker.sock"  # point at a rootless daemon
require_rootless = false       # true => fail closed unless the daemon is rootless
network = false                # false => no network AND the setup phase is skipped
allow_hosts = []               # enforced by the allowlisting egress proxy; empty = any host
setup_command = []             # phase A (network on)
test_command = []              # phase B (always offline)
```

A run is up to two phases: **setup** installs dependencies with the network on, **tests**
always run offline. If `network` is false, setup is skipped and only the offline test phase
runs. Each run builds its own image (from the repository Dockerfile or a Janus-generated one)
and removes it afterwards; `image_source = "prebuilt"` uses `image` as-is. When `network` is
true and `allow_hosts` is non-empty, the run joins an internal network whose only route out is
an allowlisting proxy, so only the listed hosts are reachable (ADR D-059). The container never
receives the Docker socket and no host path is bind-mounted. Output is streamed, bounded at
`max_output_bytes`, and redacted before it leaves the sandbox.

The Docker-gated sandbox tests skip themselves when no daemon is reachable:

```bash
uv run pytest tests/integration/test_sandbox_runner.py -q
```

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
