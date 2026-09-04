# Janus — Architecture, from first principles

This document explains *why* Janus is shaped the way it is. It deliberately contains no
code: it is a study of the problem, the causal chain that solves it, and the trade-offs
behind each layer. Concrete, reversible choices live in [`DECISIONS.md`](DECISIONS.md);
the build order lives in [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md).

---

## 1. The problem, decomposed

A red CI build is a **signal with an ambiguous cause**. Starting from first principles,
ask: what can make a test fail?

1. **The code is wrong** — a real defect in the source under test.
2. **The test is wrong** — the test is non-deterministic or the assertion is stale
   (a "flake").
3. **The environment/pipeline is wrong** — the runner ran out of memory, the network
   reset, a dependency registry rate-limited, a disk filled up.

These three causes are **mutually exclusive as responses**, because the correct action
for each is different and, crucially, the *wrong* action is harmful:

- "Fixing" a flaky test by editing it **destroys the test's value**.
- "Fixing" an infra outage by changing code **makes the code worse**.
- Editing tests to make a real code bug pass is **the cardinal sin** — it hides the bug.

Therefore the first-principles requirement is:

> **Classify before you mutate.** Never change anything until the fault class is known,
> and only mutate for the one class where mutation is provably safe to attempt (a code
> bug), under a proof obligation.

This single sentence generates most of the architecture.

### 1.1 Corollary: uncertainty must have a safe default

Classification is imperfect. When the evidence is weak, the system must **not guess**.
The safe default is to report what was observed and change nothing (a comment). This is
why `UNSURE`, `FLAKY` and `ENV/PIPELINE` are all comment-only, and only `CODE_BUG`
proceeds. Failure of any later stage likewise degrades to a comment rather than a
speculative change.

### 1.2 Corollary: every claim needs a gate

An automated change is a **hypothesis**: "this diff fixes the failure without weakening
the tests." A hypothesis is only trustworthy if it can be falsified. Janus therefore
attaches a *gate* to every claim it makes — a measured check (full suite green, mutation
non-regression, coverage non-regression, tests unchanged). If a claim has no gate, it is
not made. This is the meaning of "no claim without a gate."

---

## 2. The causal chain

The system is a pipeline where each stage's output is the next stage's input. Reading it
as cause → effect:

```
red event ──▶ reproduce ──▶ triage ──▶ [CODE_BUG] ──▶ localize ──▶ retrieve
   (cause)      (observe)    (decide)                    (where)      (precedent)
                                                             │
                                                             ▼
                                        report ◀── prove ◀── generate
                                      (surface)  (verify)   (hypothesis)
```

- **Reproduce** answers *"is this real and repeatable?"* — the same test is run K times.
  If it always fails, it is deterministic; if it flickers, it is a flake; if the failure
  signature is infrastructural, it is ENV.
- **Triage** answers *"which of the three causes is most supported by evidence?"* — it
  scores seven independent signals and routes only when confident.
- **Localize / Retrieve / Generate** only run for `CODE_BUG`, and they answer *where* the
  fault is, *whether it happened before*, and *what the fix should be*.
- **Prove** answers *"is the hypothesis actually good?"* — the gates.
- **Report** is the only stage that talks to a human or opens a PR.

Each arrow is also a **failure boundary**: if a stage cannot produce a trustworthy
output, the chain stops and falls back to a comment. The pipeline is designed to fail
*safe*, not to fail *loud*.

---

## 3. Why the layers are split the way they are

### 3.1 Ingress vs. Worker vs. Core

- **Ingress** is fast and dumb: verify a signature, persist an event, enqueue a job. It
  must return quickly because GitHub retries slow webhooks and expects a fast 2xx.
- **Worker/Orchestrator** owns the slow, expensive, failure-prone work: retries, cost
  caps, quotas, the kill switch. Separating it means a slow fix attempt never blocks
  incoming events.
- **Core** is the *method* — the reproduce/triage/fix logic. It is deliberately isolated
  so it can be reasoned about, tested and replaced without touching transport concerns.

**Cause → effect:** because the expensive work is asynchronous and rate-limited by cost
caps, a burst of red builds cannot melt the system or the budget. The queue (a Postgres
table with `SKIP LOCKED`) is the shock absorber.

### 3.2 Why the core is language-agnostic

The plan requires the core to hold no Python specifics. The reason is **causal, not
aesthetic**: the method (reproduce, score signals, localize, prove) is *not* specific to
any language — only the mechanics of running tests, measuring coverage, and mutating code
are. If Python specifics leak into the core, the method becomes entangled with one
toolchain and a second language becomes a rewrite instead of an adapter.

So the boundary is drawn at `core/adapter.py`, a `Protocol` describing the operations
the core needs: *discover test targets, run tests, parse results, collect per-test
coverage, mutate code*. `adapters/python/` implements it with pytest, coverage.py and
mutmut. The core calls the protocol; it never imports pytest.

**Caveat:** the abstraction is only as good as the first adapter's honesty. If the
Python adapter needs an operation the protocol does not express, the protocol must grow —
a real risk, managed by building the Python adapter first and letting it pressure-test
the interface.

### 3.3 Surfaces are output-only

GitHub Checks, comments and PRs are **surfaces**, not logic. Keeping them in `report/`
means the same diagnosis renders identically as a comment or a PR body, and no surface
can silently change a decision. The system has no auto-merge and no force-push path by
construction.

---

## 4. The data model as the system's memory

Every stage writes its evidence to Postgres. This is not bookkeeping — it is what makes
the system auditable and self-improving:

- `runs` records the observed event and the **baseline** (last-green) used for the diff.
- `reproductions` records the K reruns and their telemetry — the raw material of triage.
- `signals` records each signal's raw and normalized value — *why* a route was chosen.
- `diagnoses` records the route, confidence and the top-two gap.
- `suspects`, `fixes`, `gate_results` record the fix hypothesis and its proof.

**Cause → effect:** because evidence is persisted per stage, a comment or PR can cite
measured results ("no claim without a gate"), and the same rows later become training
labels for a learned classifier — the deferred "trained triage classifier" becomes
possible *for free* because the rules-first system already records its inputs and
outcomes.

---

## 5. Invariants and where they are enforced

| Invariant | First-principles reason | Enforcement point |
|---|---|---|
| PR-only, no auto-merge/force-push | A bot must never silently change `main`. | Surface layer has no such API call. |
| Installation tokens only, never in URLs | A token in a URL leaks into logs and process lists. | Clone uses a credential helper. |
| Secret redaction before LLM/UI | The LLM is an external service; logs may hold secrets. | Redaction stage before any LLM/UI egress. |
| No `docker.sock` in sandbox | A container with the socket can own the host. | Sandbox mounts exclude the socket. |
| Network allowlist + read-only mounts + resource caps | Untrusted repo code runs during tests. | Sandbox runner. |
| Cost caps per failure | Agents burn unbounded time/tokens if unchecked. | Worker accounting; exceeding a cap → comment only. |
| Global kill switch + per-repo quotas | One bad repo must not starve the system. | Worker claim loop. |

The pattern: **the reason an invariant exists determines where it must be enforced.**
Safety properties live at the boundary closest to the danger (the sandbox, the egress),
not in the core.

---

## 6. How S0 (Phase 0 + 1) fits the whole

S0 builds only the **spine**: ingress, the GitHub client, persistence, and the Check-run
surface. It proves the hardest-to-fake integration — *a signed webhook from a real red
build produces a real Check run* — before any method (reproduce/triage/fix) is built.

**Cause → effect:** building the spine first means every later stage has a trustworthy
transport and a place to record evidence. If the spine were built last, the method would
be developed against mocks that might not match GitHub's real behaviour. The phase
ordering is therefore a de-risking strategy: *integrate the uncertain external boundary
first, then layer the method on top.*

The reproduce/triage/fix stages are intentionally absent from S0. They begin only after
the spine is demonstrated, so that effort is never spent on a method that cannot yet be
delivered to a user.
