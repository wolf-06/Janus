"""The reproducer: rerun the failing targets K times, then the whole suite once.

Given a checkout and the failing targets, the reproducer measures what actually happens:
it runs the failing-target set ``K`` times (one sandbox run per attempt), then runs the full
suite once, and returns the collected evidence as a
:class:`~janus.core.evidence.Reproduction`. It makes **no** triage decision, computes no
score and persists nothing — those are the collectors' and the scorer's jobs, built against
the frozen evidence types.

Two profiles exist (see :class:`~janus.sandbox.profiles.Profile`). ``clean`` is a fresh,
repeatable environment and is fully implemented. ``ambient`` is supposed to match the image
and environment the failed CI run used; **that reconstruction is not implemented yet**, so an
ambient reproduction currently runs the *same clean specs* and reports
``ambient_reconstructed = False`` rather than pretending it matched. A downstream collector
reads that flag and leans towards ``UNSURE``.

``reproduce`` is **blocking** (it drives the sandbox synchronously); a caller on an event
loop must run it in a thread. Infrastructure failures never raise: a run whose
:class:`~janus.sandbox.spec.SandboxResult` carries an ``error`` or ``timed_out`` still yields
one :class:`~janus.core.evidence.RunAttempt` per requested target, with ``sandbox_error`` set
and ``passed = False``, so the determinism signal can see the sandbox itself was unhealthy.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import structlog

from janus.app.config import Settings
from janus.core.adapter import Adapter, TestOutcome, TestTarget
from janus.core.evidence import Reproduction, RunAttempt
from janus.core.plan import build_plan, phase_result
from janus.sandbox.profiles import Profile
from janus.sandbox.runner import SandboxRunner
from janus.sandbox.spec import SandboxPhase, SandboxResult, SandboxSpec

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ReproduceRequest:
    """What to reproduce: the failing targets, how many reruns, and which profile."""

    targets: tuple[TestTarget, ...]
    """The failing targets CI reported. Empty means "no reruns, just the full suite"."""

    k: int
    """How many times to rerun the failing-target set. ``<= 0`` is treated as ``1``."""

    profile: Profile = Profile.CLEAN
    """Which environment to reproduce. ``ambient`` currently falls back to clean specs."""


class Reproducer:
    """Measure the K reruns plus the final full-suite run inside the sandbox."""

    def __init__(
        self,
        *,
        adapter: Adapter,
        runner: SandboxRunner,
        settings: Settings,
    ) -> None:
        self._adapter = adapter
        self._runner = runner
        self._settings = settings

    def reproduce(self, source_dir: Path, request: ReproduceRequest) -> Reproduction:
        """Run the reproduction for ``request`` over the checkout at ``source_dir``.

        Blocking. Runs the failing-target set ``K`` times (skipped entirely when there are no
        targets), then the whole suite once. Every sandbox run's ``duration_s`` contributes to
        ``cost_minutes``. Returns the measured evidence; it never raises on a sandbox failure.
        """
        k = request.k
        if k <= 0:
            log.warning("reproducer.k_clamped", requested=request.k, using=1)
            k = 1

        all_results: list[SandboxResult] = []
        attempts: list[RunAttempt] = []

        if request.targets:
            plan = build_plan(self._adapter, self._settings.sandbox, targets=request.targets)
            specs = plan.specs(self._settings.sandbox)
            for index in range(k):
                results = self._run_specs(specs, source_dir)
                all_results.extend(results)
                attempts.extend(self._attempts_for(results, request.targets, index))
        else:
            log.info("reproducer.no_targets_skip_reruns", profile=request.profile.value)

        full_results = self._run_specs(
            build_plan(self._adapter, self._settings.sandbox, targets=()).specs(
                self._settings.sandbox
            ),
            source_dir,
        )
        all_results.extend(full_results)
        full_suite = self._full_attempt(full_results)

        cost_minutes = sum(result.duration_s for result in all_results) / 60.0
        ambient_reconstructed = request.profile is not Profile.AMBIENT
        if not ambient_reconstructed:
            log.warning(
                "reproducer.ambient_not_reconstructed",
                detail="CI-image reconstruction is not implemented; running clean specs",
            )

        reproduction = Reproduction(
            profile=request.profile,
            attempts=tuple(attempts),
            full_suite=full_suite,
            ambient_reconstructed=ambient_reconstructed,
            cost_minutes=cost_minutes,
        )
        log.info(
            "reproducer.complete",
            profile=request.profile.value,
            k=k,
            attempts=len(reproduction.attempts),
            failures=reproduction.failures,
            duration_s=reproduction.total_duration_s,
            cost_minutes=cost_minutes,
        )
        return reproduction

    def _run_specs(self, specs: Sequence[SandboxSpec], source_dir: Path) -> list[SandboxResult]:
        """Run every spec in order, degrading a runner exception to an error result."""
        return [self._run_one(spec, source_dir) for spec in specs]

    def _run_one(self, spec: SandboxSpec, source_dir: Path) -> SandboxResult:
        """Run one spec; an unexpected runner failure becomes an error result, never a raise."""
        try:
            return self._runner.run(spec, source_dir=source_dir)
        except Exception as exc:  # a bad run must degrade, never crash the reproducer
            return SandboxResult(
                image=spec.image,
                phase=spec.phase,
                error=f"runner_error: {exc}",
                command=spec.command,
            )

    def _attempts_for(
        self,
        results: Sequence[SandboxResult],
        targets: Sequence[TestTarget],
        index: int,
    ) -> list[RunAttempt]:
        """Flatten one rerun into one ``RunAttempt`` per target (or per parsed outcome).

        An infrastructure failure (no test result, an ``error``, or a timeout) has no parsed
        outcomes, so one failed attempt is synthesised per *requested* target. A healthy run
        yields one attempt per parsed outcome.
        """
        test_result = phase_result(results, SandboxPhase.TEST)
        if test_result is None or test_result.error is not None or test_result.timed_out:
            error = self._sandbox_error(test_result) or "no_test_result"
            return [self._error_attempt(index, target.id, test_result, error) for target in targets]

        return [
            self._outcome_attempt(index, outcome, test_result)
            for outcome in self._adapter.parse_results(test_result)
        ]

    def _outcome_attempt(
        self, index: int, outcome: TestOutcome, result: SandboxResult
    ) -> RunAttempt:
        return RunAttempt(
            index=index,
            target_id=outcome.target_id,
            passed=outcome.passed,
            duration_s=result.duration_s,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            oom_killed=result.oom_killed,
            cpu_seconds=result.cpu_seconds,
            peak_mem_mb=result.peak_mem_mb,
            stdout_excerpt=self._excerpt(result.stdout),
            stderr_excerpt=self._excerpt(result.stderr),
            sandbox_error=outcome.sandbox_error or result.error,
        )

    def _error_attempt(
        self,
        index: int,
        target_id: str,
        result: SandboxResult | None,
        error: str,
    ) -> RunAttempt:
        return RunAttempt(
            index=index,
            target_id=target_id,
            passed=False,
            duration_s=result.duration_s if result is not None else 0.0,
            exit_code=result.exit_code if result is not None else None,
            timed_out=result.timed_out if result is not None else False,
            oom_killed=result.oom_killed if result is not None else False,
            cpu_seconds=result.cpu_seconds if result is not None else 0.0,
            peak_mem_mb=result.peak_mem_mb if result is not None else 0.0,
            stdout_excerpt=self._excerpt(result.stdout) if result is not None else "",
            stderr_excerpt=self._excerpt(result.stderr) if result is not None else "",
            sandbox_error=error,
        )

    def _full_attempt(self, results: Sequence[SandboxResult]) -> RunAttempt:
        """The single whole-suite run (``target_id = None``), as one attempt."""
        result = phase_result(results, SandboxPhase.TEST)
        if result is None:
            return RunAttempt(
                index=0,
                target_id=None,
                passed=False,
                duration_s=0.0,
                exit_code=None,
                timed_out=False,
                oom_killed=False,
                cpu_seconds=0.0,
                peak_mem_mb=0.0,
                stdout_excerpt="",
                stderr_excerpt="",
                sandbox_error="no_test_result",
            )
        return RunAttempt(
            index=0,
            target_id=None,
            passed=result.ok,
            duration_s=result.duration_s,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            oom_killed=result.oom_killed,
            cpu_seconds=result.cpu_seconds,
            peak_mem_mb=result.peak_mem_mb,
            stdout_excerpt=self._excerpt(result.stdout),
            stderr_excerpt=self._excerpt(result.stderr),
            sandbox_error=self._sandbox_error(result),
        )

    def _sandbox_error(self, result: SandboxResult | None) -> str | None:
        """The infrastructure error string for ``result``, or ``None`` for a healthy run.

        ``None`` is the important case: a normal red suite (a non-zero exit, no infra error)
        is *not* a sandbox failure and must not be recorded as one.
        """
        if result is None:
            return "no_test_result"
        if result.error is not None:
            return result.error
        if result.timed_out:
            return "timed_out"
        return None

    def _excerpt(self, text: str) -> str:
        """Bound an already-redacted stream to the configured evidence size."""
        return text[: self._settings.max_signal_detail_chars]
