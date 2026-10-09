# Janus — Sandbox Spec (Phase 3)

Source: `PLAN.md` §1.2 and §4, implemented via `docs/IMPLEMENTATION_PLAN.md` §2 (Phase 3).
This spec covers the **sandbox** half of Phase 3: running one command in an ephemeral,
locked-down container and returning trustworthy facts about it.

Status: **sandbox core implemented.** The adapter interface and the Python adapter are now
implemented and specified in [`ADAPTER.md`](ADAPTER.md); the sample fixture repo is the remaining
Phase 3 work and is deferred to Phase 4.

---

## 1. Scope

**In scope:**

- A container runner that executes one command with no host bind mounts, no Docker socket,
  no network by default, dropped capabilities, and CPU/memory/PID/time limits.
- A per-run image build: from a Dockerfile at the repository root when one exists, otherwise
  from a Janus-generated Dockerfile. The image is used for one run and removed afterwards;
  the source is never baked into it.
- Source copied in with `docker cp` and owned by the run user.
- Two-phase runs: setup (network allowed) then tests (always offline). Network disabled in
  configuration skips setup entirely. A non-empty `allow_hosts` is enforced by an
  allowlisting egress proxy on an internal network.
- Resource telemetry (peak memory, cumulative CPU seconds).
- Secret redaction of all sandbox output.
- Artifact extraction: absolute container paths named by the caller (a pytest JSON report, a
  coverage file) are read back before the container is removed, so a structured result written
  *inside* the container is not lost when it is destroyed. Artifacts are returned raw, not
  redacted — a deliberate carve-out (see §7).
- Configuration from the TOML config file.

**Non-goals (still Phase 3, but not this spec):**

- The `Adapter` Protocol and its conformance suite.
- The Python adapter (pytest, coverage.py contexts, mutmut).
- Wiring the sandbox into `JobProcessor`.

---

## 2. Configuration

Read from `janus.toml` (top level), or `[tool.janus]` in `pyproject.toml`; the path can be
overridden with `JANUS_CONFIG_FILE`. Environment variables win over the file, with nested
keys using a double underscore (`JANUS_SANDBOX__IMAGE`).

```toml
[sandbox]
enabled = true
# auto | repo | generated | prebuilt. "auto" uses a repo Dockerfile when present, else
# generates one; every built image is used once and removed afterwards.
image_source = "auto"
image = "python:3.12-slim"    # the "prebuilt" image, and the generated base by default
# base_image = "..."          # override the generated base (unset => use `image`)
tooling_packages = ["pytest", "pytest-json-report", "coverage", "mutmut"]
pull_base_image = true        # pull the base while building
build_network = true          # allow the build network (needed to install packages)
build_timeout_seconds = 900.0 # wall-clock cap for one image build
image_tag_prefix = "janus-sandbox"  # content-addressed tags are <prefix>:<sha256[:12]>
remove_image = true           # remove the per-run image; "prebuilt" is never removed
pull_image = true             # pull the "prebuilt" image when absent
telemetry_sample_interval_seconds = 0.2  # how often resource usage is sampled
# docker_host = "..."         # Docker endpoint URL; unset => docker.from_env()
require_rootless = false      # true => fail closed unless the daemon is rootless
workdir = "/work"
user = "1000:1000"
timeout_seconds = 900.0       # wall-clock cap for one command
memory_mb = 2048
cpus = 2.0
pids_limit = 256
max_output_bytes = 1048576    # per-command stdout/stderr kept
max_artifact_bytes = 4194304  # per-artifact size cap; larger => too_large, never truncated
network = false               # false => no network AND setup is skipped
allow_hosts = []              # enforced by the egress proxy; empty = any host
proxy_port = 8888             # port the sandbox's proxy env vars point at
proxy_memory_mb = 256         # memory cap for the proxy container
proxy_pids_limit = 128        # process cap for the proxy container
setup_command = []            # phase A (network on); empty = skipped
test_command = []             # phase B (always offline)
```

`allow_hosts` is enforced: a run with `network = true` and a non-empty list can only reach
the listed hosts, through an allowlisting proxy on an internal network (see §3). Matching is
case-insensitive and ignores a trailing dot; a leading-dot entry (`.github.com`) is a wildcard
matching the apex (`github.com`) and any subdomain (`api.github.com`); an empty list means
"any host".

Invalid values (blank `image`, blank or whitespace-bearing `image_tag_prefix`, a blank
`base_image` or `docker_host`, relative `workdir`, non-positive limits including
`build_timeout_seconds`, `telemetry_sample_interval_seconds`, `max_artifact_bytes`,
`proxy_memory_mb`, and `proxy_pids_limit`, or a `proxy_port` outside 1..65535) fail at startup.

---

## 3. Mechanism

0. **Resolve & build** — `resolve_image` picks the source: `prebuilt` uses `image` as-is;
   `repo` requires a root Dockerfile; `generated` writes one; `auto` prefers a repo
   Dockerfile and generates otherwise. A generated Dockerfile is built from a tiny temp
   context holding only the manifest files it `COPY`s (never the source). The tag is
   content-addressed from the Dockerfile, base image, and manifests. The build runs on a
   worker thread and is abandoned after `build_timeout_seconds` (best-effort). The context is
   removed after the build; the image is removed after the run.
1. **Image present** — a built image is already local; a `prebuilt` image is `images.get`-ed,
   else pulled when `pull_image` is true.
2. **Create** — `working_dir`, `network_mode`, `tmpfs /tmp`, `mem_limit`, `nano_cpus`,
   `pids_limit`, `cap_drop=["ALL"]`, `security_opt=["no-new-privileges"]`. No `privileged`,
   no volumes, no binds, no Docker socket.
3. **Start**, then **copy** the source in with `put_archive` (`docker cp`) from a spooled
   temp file, so the archive is never fully held in memory. Entries are written with the run
   user's uid/gid, and the `sleep infinity` keepalive creates and `chmod 0777`s the work dir
   — capabilities are dropped, so a runtime `chown` cannot work.
4. **Run** the command on a worker thread, **streaming** stdout/stderr. The exec is created,
   started, and inspected through the low-level API so the streamed output *and* the real exit
   code are both kept (`container.exec_run(..., stream=True)` discards the exit code). Each
   stream is bounded at `max_output_bytes` as chunks arrive; once a stream is full the excess
   is drained and discarded, so a chatty command cannot block on a full pipe. A `StatsSampler`
   polls the stats API meanwhile.
5. **Time cap** — if the command outlives `timeout_seconds`, kill the container, mark
   `timed_out`, and keep whatever output was already emitted.
6. **Extract artifacts** — when the spec names absolute container paths, read each one back with
   `get_archive` (the API behind `docker cp`) *after* the command finishes and *before* the
   container is removed. The tar stream's first regular member is the file's bytes. A missing
   path (`not_found`), a member over `max_artifact_bytes` (`too_large`), a directory, an empty
   archive, or any daemon error is recorded in `artifact_errors`, never raised and never
   truncated. Extraction also runs on the timeout path (the container is killed first; a
   partially written artifact is better than none).
7. **Collect** — redact the bounded output, remove the container, return a `SandboxResult`.

**Allowlist enforcement (network on with a non-empty `allow_hosts`).** The runner opens a
`ProxySession` (`janus.sandbox.proxy`), which creates `janus-sbx-<id>` (`internal=True`, no
route out) and `janus-sbx-up-<id>` (a normal bridge). An allowlisting proxy container is
attached to both; the sandbox is created with `network_mode=janus-sbx-<id>` and
`HTTP_PROXY`/`HTTPS_PROXY`/`http_proxy`/`https_proxy=http://<proxy-name>:8888`. The proxy
accepts plain HTTP (absolute-URI request lines) and `CONNECT`, checks the target host against
the allow list, and answers `403` otherwise; it is standard-library-only Python running in
`python:3.12-slim`. Because the sandbox is not attached to the upstream network, direct egress
fails even if it ignores the proxy variables — the topology, not the proxy alone, enforces the
policy. The proxy image is shared and content-addressed over the Dockerfile and the script
template (not the per-run host list), so it is built once and never removed. Teardown is
sandbox → proxy → both networks, always, in a `finally`; a proxy failure ends the run with
`error="proxy_failed: ..."`, never an exception.

The client comes from `docker_host` when set, else `docker.from_env()`. With
`require_rootless = true`, a daemon that is not rootless ends the run with
`error="not_rootless"` before any container is created.

Every failure — no daemon, image resolution or build failure, missing image, copy failure,
overrun, a non-rootless daemon when required — returns a `SandboxResult` with an `error`;
`run()` does not raise. Callers on an event loop use `SandboxRunner.run_in_thread`, which runs
the blocking work off the loop.

---

## 4. Data

- `SandboxSpec` — image, command, workdir, user, limits, network, phase, `artifacts` (absolute
  container paths to read back), and `max_artifact_bytes` (per-artifact cap).
- `SandboxResult` — exit code, stdout, stderr, duration, `timed_out`, `oom_killed`,
  `cpu_seconds`, `peak_mem_mb`, `error`, `command`, `artifacts` (path → raw bytes), and
  `artifact_errors` (path → `not_found` / `too_large` / `unreadable: ...`). `ok` is true only
  for a clean, in-time, zero-exit run with no infrastructure error.
- `RunPlan` — profile (`clean`/`ambient`), phases, `ambient_reconstructed`.
- `NetworkPolicy` — `enabled`, `hosts`, `allows(host)`, `enforced`, `docker_network_mode`;
  `host_allowed(host, allow_hosts)` owns the matching rule.

---

## 5. Invariants

| # | Invariant | Where it is proven |
|---|---|---|
| 1 | The sandbox never receives the Docker socket | `test_no_docker_socket_inside_the_box`; `test_container_options_never_mount_the_docker_socket` |
| 2 | No host path is bind-mounted | `container_options` has no `volumes`/`binds` |
| 3 | Tests run with no network by default | `test_network_is_off_by_default`; `test_network_default_follows_the_phase` |
| 4 | Network disabled in config skips the setup phase | `test_network_disabled_skips_setup` |
| 5 | A command cannot outlive the time cap | `test_timeout_kills_the_container`; `test_exec_times_out_and_kills_a_command_that_never_returns` |
| 6 | Secrets never leave the sandbox unredacted | `test_redaction.py`; `test_output_is_redacted` |
| 7 | Resource limits reach the container | `test_container_options_carry_the_limits`; `test_memory_limit_reaches_the_container` |
| 8 | A failed sandbox run never raises | `SandboxResult.error`; `test_run_user_can_write_in_the_workdir` (copy path) |
| 9 | The run user can write in the work dir | `test_run_user_can_write_in_the_workdir`; `test_copied_files_are_owned_by_the_run_user` |
| 10 | Captured output is bounded while streaming, and the stream is fully drained | `test_exec_bounds_streamed_output_and_drains_the_whole_stream`; `test_truncate_bounds_output` |
| 11 | A host-listed run is enforced, not merely recorded | `test_host_allowed_*`; `test_network_policy_allows_delegates_to_host_allowed`; `test_proxy_refuses_a_disallowed_connect` |
| 12 | A malformed config fails startup | `test_malformed_toml_is_an_error` |
| 13 | The per-run image is removed after the run | `test_a_built_image_is_removed_after_a_successful_run`; `test_generated_image_is_built_then_removed` |
| 14 | The source is never baked into the image | `build_context_files` (manifests only); `test_resolve_generated_creates_a_context` |
| 15 | A prebuilt image is never removed | `test_a_prebuilt_image_is_never_removed` |
| 16 | The image tag is content-addressed and deterministic | `test_image_tag_is_deterministic`; `test_image_tag_changes_with_the_dockerfile` |
| 17 | A rootless requirement fails closed on a rootful daemon | `test_run_returns_not_rootless_when_required_but_absent`; `test_is_rootless_*` |
| 18 | A host-listed run cannot reach a non-listed host, including directly | `test_enforced_hosts_block_direct_egress`; `test_proxy_refuses_a_disallowed_connect`; `test_proxy_does_not_refuse_a_listed_connect` |
| 19 | The proxy and both networks are removed after the run | `test_run_with_enforcement_opens_and_closes_a_proxy_session` |
| 20 | A requested artifact is read back before the container is removed | `test_run_passes_the_spec_artifacts_to_extraction`; `test_extracts_a_written_artifact` |
| 21 | An unreadable or oversized artifact is reported, never silently dropped, and never truncated | `test_extract_artifacts_reports_a_missing_path`; `test_extract_artifacts_reports_an_oversized_member_without_truncating`; `test_reports_a_missing_artifact` |

---

## 6. Acceptance tests

| Command | Expected |
|---|---|
| `uv run pytest tests/unit/test_sandbox_config.py` | TOML loading, `[tool.janus]` fallback, upward search, env override, fail-loud errors, validation. |
| `uv run pytest tests/unit/test_redaction.py` | Tokens, `.env`/JSON keys, high-entropy strings redacted; paths and ordinary text untouched. |
| `uv run pytest tests/unit/test_allowlist.py` | Policy semantics, `host_allowed` matching (exact, case, trailing dot, wildcard, empty), `enforced`. |
| `uv run pytest tests/unit/test_sandbox_proxy.py` | Proxy script rendering, embedded JSON round-trip, deterministic host-independent image tag, session env (no Docker). |
| `uv run pytest tests/unit/test_profiles.py` | Two-phase planning; setup skipped when network is off. |
| `uv run pytest tests/unit/test_sandbox_spec.py` | Spec building and result semantics. |
| `uv run pytest tests/unit/test_sandbox_image.py` | Dockerfile generation, content-addressed tags, image resolution (no Docker). |
| `uv run pytest tests/unit/test_sandbox_runner.py` | `tar_directory`, `container_options` (incl. network/env overrides), per-run build/remove, the proxy-session open/close path, streamed-and-bounded output with a full drain, the timeout kill, the sample-interval pass-through, artifact extraction with a fake container (regular file, `not_found`, `too_large`, directory, empty tar, unreadable, never raises), the artifact pass-through, and rootless detection (no Docker). |
| `uv run pytest tests/integration/test_sandbox_runner.py` | Docker-gated: run, copy, no socket, no network, timeout, limits, per-run build, artifact read-back and missing-artifact reporting, and allowlist enforcement (direct egress blocked, proxy 403 for a disallowed host). |
| `uv run ruff check .` / `uv run ruff format --check .` / `uv run mypy janus` | Clean. |

Docker-gated tests skip themselves when no daemon is reachable, so CI without Docker still
passes.

---

## 7. Known limits / next

- **Rootless Docker is supported via configuration.** Set `docker_host` to the rootless daemon
  socket (e.g. `unix:///run/user/1000/docker.sock`); the isolation settings are compatible with
  rootless. Host setup (enabling rootless, cgroup v2 delegation) is manual and not automated,
  and `require_rootless = true` makes a rootful daemon fail closed rather than being used.
- **Allowlist enforcement is HTTP/HTTPS-only.** A host-listed run reaches only the listed
  hosts, and only through the egress proxy (`CONNECT` and plain HTTP). Other protocols (raw
  TCP, UDP, ICMP) have no route through the proxy and simply fail. The proxy image is shared
  and content-addressed; it is built once and never removed.
- **Memory OOM timing is host-dependent.** The configured limit reaches the container's
  cgroup (verified), but whether the kernel OOM-kills a given overrun is not deterministic
  on this host, so the test asserts the limit rather than the kill.
- **Sampling resolution is configurable.** `telemetry_sample_interval_seconds` sets the poll
  period; a long interval can still miss very short-lived peaks.
- **Output is bounded while streaming.** Each stream is capped at `max_output_bytes` as it
  arrives and the rest is drained and discarded, so the daemon never buffers unbounded output.
  A killed command keeps only the output it had already emitted.
- **Redaction is best-effort.** Filesystem paths are preserved on purpose (they are
  evidence), which can let a path-shaped secret through.
- **Artifacts are returned raw by design.** A requested artifact is returned as raw bytes and
  is *not* redacted: it is structured data (a JSON report, a coverage map) that the core
  parses, and redaction could corrupt it. This is a deliberate carve-out from the "all sandbox
  output is redacted" rule. The adapter must therefore configure its tools to exclude captured
  output from these files and must redact anything it surfaces from them.
- **Artifacts must be written outside the `/tmp` tmpfs.** The container mounts `/tmp` as a
  tmpfs, and the daemon's archive API the extractor uses (`get_archive`) cannot read files from
  that mount, so a path under `/tmp` comes back `not_found`. Adapters therefore write artifacts
  to a path on the writable root filesystem — the Python adapter uses `/var/tmp/janus`.
- **Artifacts have a per-file cap.** Each artifact is capped at `max_artifact_bytes`; a larger
  file is reported as `too_large` and omitted, never truncated, because truncating structured
  data corrupts it. A path that does not exist is reported as `not_found`; any other failure as
  `unreadable: ...`. Failures are always reported in `artifact_errors`, never silently dropped.
- **No read-only rootfs.** `docker cp` cannot write into one; see `DECISIONS.md` D-049.
- **Build time per run.** Every run builds its own image when the content-addressed tag is
  not cached; there is no published base image (see `DECISIONS.md` D-056). `image_source =
  "prebuilt"` is the escape hatch for a known, already-present image.
- **The daemon needs build support.** A Docker daemon that cannot build images makes every
  non-`prebuilt` run fail with `image_build_failed`.
- **The build timeout is best-effort.** The wait is abandoned after `build_timeout_seconds`,
  but the daemon may keep building in the background.

---

## 8. Verification commands

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy janus
uv run pytest -q
uv run pytest tests/integration/test_sandbox_runner.py -q   # requires Docker
```
