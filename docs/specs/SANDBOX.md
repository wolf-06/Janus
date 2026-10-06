# Janus — Sandbox Spec (Phase 3)

Source: `PLAN.md` §1.2 and §4, implemented via `docs/IMPLEMENTATION_PLAN.md` §2 (Phase 3).
This spec covers the **sandbox** half of Phase 3: running one command in an ephemeral,
locked-down container and returning trustworthy facts about it.

Status: **sandbox core implemented.** The adapter interface and the Python adapter are the
remaining Phase 3 work (see §7).

---

## 1. Scope

**In scope:**

- A container runner that executes one command with no host bind mounts, no Docker socket,
  no network by default, dropped capabilities, and CPU/memory/PID/time limits.
- Source copied in with `docker cp` and owned by the run user.
- Two-phase runs: setup (network allowed) then tests (always offline). Network disabled in
  configuration skips setup entirely.
- Resource telemetry (peak memory, cumulative CPU seconds).
- Secret redaction of all sandbox output.
- Configuration from the TOML config file.

**Non-goals (still Phase 3, but not this spec):**

- The `Adapter` Protocol and its conformance suite.
- The Python adapter (pytest, coverage.py contexts, mutmut).
- Wiring the sandbox into `JobProcessor`.
- The network allowlist *proxy* (the policy exists; enforcement does not).

---

## 2. Configuration

Read from `janus.toml` (top level), or `[tool.janus]` in `pyproject.toml`; the path can be
overridden with `JANUS_CONFIG_FILE`. Environment variables win over the file, with nested
keys using a double underscore (`JANUS_SANDBOX__IMAGE`).

```toml
[sandbox]
enabled = true
image = "janus/python:3.12"   # Janus-owned base image
pull_image = true             # false for locally built images
workdir = "/work"
user = "1000:1000"
timeout_seconds = 900.0       # wall-clock cap for one command
memory_mb = 2048
cpus = 2.0
pids_limit = 256
max_output_bytes = 1048576    # per-command stdout/stderr kept
network = false               # false => no network AND setup is skipped
allow_hosts = []              # rejected while network = true (not enforceable yet)
setup_command = []            # phase A (network on); empty = skipped
test_command = []             # phase B (always offline)
```

Invalid values (blank image, relative `workdir`, non-positive limits, or `network = true`
together with a non-empty `allow_hosts`) fail at startup.

---

## 3. Mechanism

1. **Image** — `images.get`, else pull when `pull_image` is true (from the config file).
2. **Create** — `working_dir`, `network_mode`, `tmpfs /tmp`, `mem_limit`, `nano_cpus`,
   `pids_limit`, `cap_drop=["ALL"]`, `security_opt=["no-new-privileges"]`. No `privileged`,
   no volumes, no binds, no Docker socket.
3. **Start**, then **copy** the source in with `put_archive` (`docker cp`) from a spooled
   temp file, so the archive is never fully held in memory. Entries are written with the run
   user's uid/gid, and the `sleep infinity` keepalive creates and `chmod 0777`s the work dir
   — capabilities are dropped, so a runtime `chown` cannot work.
4. **Run** the command with `exec_run` on a worker thread while a `StatsSampler` polls the
   stats API.
5. **Time cap** — if the command outlives `timeout_seconds`, kill the container and mark
   `timed_out`.
6. **Collect** — truncate output to `max_output_bytes`, redact it, remove the container,
   return a `SandboxResult`.

Every failure — no daemon, missing image, copy failure, overrun — returns a
`SandboxResult` with an `error`; `run()` does not raise. Callers on an event loop use
`SandboxRunner.run_in_thread`, which runs the blocking work off the loop.

---

## 4. Data

- `SandboxSpec` — image, command, workdir, user, limits, network, phase.
- `SandboxResult` — exit code, stdout, stderr, duration, `timed_out`, `oom_killed`,
  `cpu_seconds`, `peak_mem_mb`, `error`, `command`. `ok` is true only for a clean, in-time,
  zero-exit run with no infrastructure error.
- `RunPlan` — profile (`clean`/`ambient`), phases, `ambient_reconstructed`.
- `NetworkPolicy` — `enabled`, `hosts`, `allows(host)`, `docker_network_mode`.

---

## 5. Invariants

| # | Invariant | Where it is proven |
|---|---|---|
| 1 | The sandbox never receives the Docker socket | `test_no_docker_socket_inside_the_box`; `test_container_options_never_mount_the_docker_socket` |
| 2 | No host path is bind-mounted | `container_options` has no `volumes`/`binds` |
| 3 | Tests run with no network by default | `test_network_is_off_by_default`; `test_network_default_follows_the_phase` |
| 4 | Network disabled in config skips the setup phase | `test_network_disabled_skips_setup` |
| 5 | A command cannot outlive the time cap | `test_timeout_kills_the_container` |
| 6 | Secrets never leave the sandbox unredacted | `test_redaction.py`; `test_output_is_redacted` |
| 7 | Resource limits reach the container | `test_container_options_carry_the_limits`; `test_memory_limit_reaches_the_container` |
| 8 | A failed sandbox run never raises | `SandboxResult.error`; `test_run_user_can_write_in_the_workdir` (copy path) |
| 9 | The run user can write in the work dir | `test_run_user_can_write_in_the_workdir`; `test_copied_files_are_owned_by_the_run_user` |
| 10 | Captured output is bounded | `test_truncate_bounds_output` |
| 11 | An unenforceable host allowlist is rejected | `test_network_with_allow_hosts_is_rejected` |
| 12 | A malformed config fails startup | `test_malformed_toml_is_an_error` |

---

## 6. Acceptance tests

| Command | Expected |
|---|---|
| `uv run pytest tests/unit/test_sandbox_config.py` | TOML loading, `[tool.janus]` fallback, upward search, env override, fail-loud errors, validation. |
| `uv run pytest tests/unit/test_redaction.py` | Tokens, `.env`/JSON keys, high-entropy strings redacted; paths and ordinary text untouched. |
| `uv run pytest tests/unit/test_allowlist.py` | Policy semantics. |
| `uv run pytest tests/unit/test_profiles.py` | Two-phase planning; setup skipped when network is off. |
| `uv run pytest tests/unit/test_sandbox_spec.py` | Spec building and result semantics. |
| `uv run pytest tests/unit/test_sandbox_runner.py` | `tar_directory` and `container_options` (no Docker). |
| `uv run pytest tests/integration/test_sandbox_runner.py` | Docker-gated: run, copy, no socket, no network, timeout, limits. |
| `uv run ruff check .` / `uv run ruff format --check .` / `uv run mypy janus` | Clean. |

Docker-gated tests skip themselves when no daemon is reachable, so CI without Docker still
passes.

---

## 7. Known limits / next

- **Rootless Docker is not yet targeted.** The runner talks to the standard daemon; the
  isolation settings are compatible with rootless, but rootless setup is not automated.
- **Allowlist enforcement.** `NetworkPolicy` records the hosts; the enforcing proxy is not
  built. Until then, enabling the network means unrestricted egress, so the configuration
  **fails closed**: `network = true` with a non-empty `allow_hosts` is rejected rather than
  silently ignored.
- **Memory OOM timing is host-dependent.** The configured limit reaches the container's
  cgroup (verified), but whether the kernel OOM-kills a given overrun is not deterministic
  on this host, so the test asserts the limit rather than the kill.
- **Sampling resolution.** A 0.5 s interval can miss very short-lived peaks.
- **Output capture is capped after the fact.** `exec_run` buffers the command's full output
  before truncation to `max_output_bytes`; bounding the capture itself needs streaming.
- **Redaction is best-effort.** Filesystem paths are preserved on purpose (they are
  evidence), which can let a path-shaped secret through.
- **No read-only rootfs.** `docker cp` cannot write into one; see `DECISIONS.md` D-049.

---

## 8. Verification commands

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy janus
uv run pytest -q
uv run pytest tests/integration/test_sandbox_runner.py -q   # requires Docker
```
