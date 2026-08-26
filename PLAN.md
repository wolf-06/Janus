# Janus — Final Build Plan (S0–S2)

**One-liner:** When CI goes red, Janus decides whether the fault is your code, your tests, or your pipeline. For code bugs it localizes the fault, writes a fix, proves it with gates, and opens a PR.

This plan replaces the earlier Janus plan and the old healing-agent repo. Start from scratch. No mention of the old project.

---

## 1. What the demo is

A red build comes in. Janus reruns it 5 times in a sandbox, says "code bug, 87% confidence, here is the line and the commit that introduced it," and opens a PR with a patch that survived mutation testing.

**Scope: S0 + S1 + S2.**

### 1.1 GitHub App spine (S0)
- GitHub App via manifest flow (one-click install).
- Webhook signature verification.
- Fetch failing run logs and the last-green to first-red diff.
- Post a Check run. State in Postgres.

### 1.2 Reproduce + Triage Brain (S1)
- Rerun failing targets K=5 times in a rootless Docker sandbox. Run the full suite once at the end.
- Two profiles: clean (deterministic) and ambient (matches the failed run's image/env).
- Rules-based scoring over 7 signals:
  1. Determinism profile (always-fail, intermittent, infra-signature)
  2. Signature library (timeout, ECONNRESET, 429/503, ENOSPC, OOMKilled, SIGKILL, resolver errors)
  3. Exit-code taxonomy (137, 124, 1, 2)
  4. Resource telemetry (cgroup CPU/mem/IO, wall-clock vs baseline)
  5. Change attribution (diff last-green to first-red)
  6. Correlation (matrix leg, cache miss, runner)
  7. Flake history (per-test rate)
- Routes scored: CODE_BUG, FLAKY, ENV/PIPELINE.
- Decision: pick argmax if score >= tau (0.80), else UNSURE. If top two are within epsilon (0.05), also UNSURE.
- **UNSURE = comment only.** FLAKY and ENV diagnoses are also comment only in this scope. Only CODE_BUG proceeds to a fix.
- Diagnosis comment includes the evidence: per-run results, matched signals, scores.
- All of K, tau, epsilon are configurable.

### 1.3 CODE_BUG loop (S2)
- **Localize:** per-test coverage contexts, SBFL (Ochiai/DStar) for ranked file:line suspects, then automated `git bisect` in the sandbox for the introducing commit.
- **Retrieve:** grep `git log -p` for past commits with similar fault signature (error type, location, change shape). No vector DB.
- **Generate:** LLM emits a minimal unified diff, **source files only, never tests**. One attempt per suspect, up to 3 retries using verifier feedback.
- **Proof gates (all must pass):**
  1. Full suite green
  2. Mutation score non-regression (`mutmut`, targeted to changed lines)
  3. Coverage non-regression
  4. Test-file hash lock (tests unchanged)
- **Policy gate:** reject any diff that edits tests, adds `continue-on-error` / `|| true` / `set +e` / unexplained skip or xfail, lowers coverage thresholds, removes assertions, or weakens tests.
- **PR mode:** `.janus.yml` with `mode: pr` opens a PR with diagnosis, root cause, diff, and a verification table. **Default mode is suggest (comments only).** Failed proof after retries falls back to comment with evidence.

---

## 2. Simplifications from the original plan

| Original | Now | Why |
|---|---|---|
| Temporal | Postgres job table + plain worker | Retry logic is about 50 lines. The method is the moat, not the infra. |
| Vector DB / RAG | `git log -p` grep for similar fixes | Good enough for v1. Upgrade later. |
| React dashboard | Checks + PRs are the surface. One static replay page at most. | Cuts a whole frontend. |
| gVisor | Rootless Docker | Documented as the production-hardening upgrade. |
| Full BugsInPy | Subset of about 25-30 bugs | Enough for a credible table, small enough to finish. |
| Three-way demo (S4 target) | CODE_BUG fix demo (S2 target) | Ships weeks sooner. |

### Benchmark (the publishable table)
- Run Janus on a BugsInPy subset of about 25-30 bugs.
- Report: triage confusion matrix, fix rate, **false-fix rate** (target near 0), mutation/coverage delta, median time-to-green.
- Demo repos: one purpose-built seed repo plus one real repo.

---

## 3. Deferred / cut (README "What's next" section)

Frame each as a roadmap bullet and an interview talking point.

| Item | One-line rationale |
|---|---|
| Flake path (quarantine PR + expiry + tracking issue) | Needs determinism gating and issue plumbing; triage already detects flakes and comments. |
| Pipeline self-heal + chaos harness | Separate proof plane (chaos replay); large build, not needed to show the method. |
| Graduation mechanic + learning | Needs real merge/reject history; rules-first triage collects labels meanwhile. |
| Trained triage classifier | Swap in once labeled outcomes exist. |
| Node adapter (Vitest, Stryker) | Proves pluggable design, but only after one adapter is solid. |
| Temporal | Worth it at scale; a job table is enough for one repo. |
| Vector RAG | Retrieval upgrade once grep stops finding similar fixes. |
| Dashboard | Checks + PRs already carry the story. |
| gVisor | Production hardening; rootless Docker is fine for demo. |
| Hosted deployment | Run locally (smee.io/ngrok for webhooks). Proof plane needs container-capable compute (Cloud Run / Fargate / Fly), not Lambda. |

Keep the adapter interface clean (`python` adapter behind a language-agnostic core) so the Node adapter is a credible next step.

---

## 4. Guardrails from day one

- **Hard cost caps per failure:** max sandbox minutes, max fix retries, max LLM tokens. Exceeding a cap means comment only. Coding agents will burn both if unchecked.
- **PR-only.** No auto-merge. Never force-push.
- **Least-privilege GitHub App scopes:** Contents RW, Pull requests RW, Checks RW, Actions read, Metadata read. Add Issues only when the flake path ships.
- **Installation tokens only.** Never put tokens in clone URLs.
- **Secret redaction** before logs reach the LLM or any UI: secrets, `.env`, tokens, high-entropy strings. Only redacted logs and relevant source files go to the LLM.
- **Sandbox:** no `docker.sock` in the backend. Ephemeral, network-isolated with allowlist, read-only mounts, CPU/mem/time caps.
- **Global kill switch** and per-repo quotas.
- **No claim without a gate.** Every statement in a PR or comment maps to a measured result.

---

## 5. Milestones

### S0 — App spine
Build: manifest install, webhook verify, log + diff fetch, Check run, Postgres schema (installations, runs, jobs, diagnoses).
**Demo:** push a failing commit to the seed repo. Janus posts a Check run on the real red build.
**Done when:** webhook to Check run works end to end, locally.

### S1 — Reproduce + triage (suggest mode)
Build: sandbox runner, K=5 reruns, 7-signal rules scoring, tau/epsilon decision, evidence comment, cost caps.
**Demo:** triage-only. Seed repo with a logic bug, a flaky test, and an infra break. Janus labels each differently with evidence. UNSURE cases comment only.
**Done when:** confusion matrix runs on the seed set and results are sensible.

### S2 — CODE_BUG fix + proof + PR
Build: SBFL + bisect, git-log retrieval, LLM source-only diff, 4 proof gates, policy gate, `.janus.yml` PR mode, BugsInPy subset harness.
**Demo:** red build to diagnosis ("code bug, 87%, this line, this commit") to a PR with a verification table.
**Done when:** seed repo and one real repo both produce a proven PR, and the benchmark table is generated.

---

## 6. Build notes (spec-driven with coding agents)

- Write one spec file per milestone from the sections above. Hand the agent one milestone at a time.
- Each spec lists acceptance tests; the agent does not move on until they pass.
- Keep the core language-agnostic; Python specifics live in the adapter (pytest, coverage.py, mutmut).
- Stack: Python + FastAPI, Postgres, Docker (rootless).
- Do not add any deferred item before S2 is demoed.
