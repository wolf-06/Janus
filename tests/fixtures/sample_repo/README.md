# S1 sample repository

This is the **S1 seed repository**: a tiny, stdlib-only Python repo Janus runs inside its
sandbox. It exists so the reproduce + triage path (and the Python-adapter integration test)
have a real repository to build, copy and test — not a synthetic string of source.

It is deliberately small so the sandbox image build is fast: no third-party dependencies.

## Layout

```
sample_repo/
  pyproject.toml      # metadata + pytest config (pythonpath = ["."], testpaths = ["tests"])
  requirements.txt    # intentionally empty — stdlib only
  .janus.yml          # example per-repository Janus config (see janus/config/janus_yml.py)
  src/calc.py         # the module under test (carries one logic bug)
  tests/              # one test per triage route, plus a control
```

Run the whole suite from this directory:

```
python -m pytest -q
```

## The four tests, and what each demonstrates

| Test | Route exercised | Behaviour |
|---|---|---|
| `tests/test_logic.py::test_sum_to_includes_the_last_term` | **CODE_BUG** | Fails deterministically: `src/calc.py::sum_to` is off by one (`range(1, n)` drops `n`). |
| `tests/test_flaky.py::test_intermittent_switch` | **FLAKY** | Intermittent *without being random*: passes unless the caller flips an explicit switch. |
| `tests/test_env.py::test_requires_service_endpoint` | **ENV/PIPELINE** | Fails with an infrastructure signature (missing config / connection refusal), not an assertion. |
| `tests/test_passing.py::test_always_passes` | control | Always passes; the baseline that must stay green. |

### Driving the "flaky" test

`test_intermittent_switch` reads the environment variable `JANUS_SAMPLE_FLAKE`:

```
JANUS_SAMPLE_FLAKE=pass python -m pytest tests/test_flaky.py -q   # passes (the default)
JANUS_SAMPLE_FLAKE=fail python -m pytest tests/test_flaky.py -q   # fails
```

Any other value is rejected by the test itself. The point is that the outcome is chosen by
the caller, so a reproduction loop sees a stable result per run — a genuinely random flake
would make the seed set non-deterministic and is intentionally avoided.

### Driving the "env" test

`test_requires_service_endpoint` reads `JANUS_SAMPLE_ENDPOINT` (`host:port`). With the
variable unset it raises a misconfiguration error (an infrastructure signature); with a
value it attempts a TCP connection, so an unreachable endpoint surfaces as a connection
refusal rather than an assertion failure.

## The logic bug

`src/calc.py::sum_to(n)` should return `1 + 2 + ... + n` but iterates `range(1, n)`, so it
drops the final term (`sum_to(3) == 3`, not `6`). `tests/test_logic.py` asserts the correct
value and therefore fails deterministically. Fixing `range(1, n + 1)` turns the suite green —
that is the change the S2 fix loop is expected to discover.
