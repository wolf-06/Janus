"""The telemetry sampler: high-water marks, error tolerance, context manager."""

from __future__ import annotations

import time
from collections.abc import Callable

from janus.sandbox.telemetry import ResourceSample, StatsSampler


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)


def test_sampler_keeps_high_water_marks() -> None:
    counter = {"n": 0}

    def sample() -> ResourceSample:
        counter["n"] += 1
        return ResourceSample(cpu_seconds=float(counter["n"]), peak_mem_mb=counter["n"] * 10.0)

    sampler = StatsSampler(sample, interval=0.005).start()
    _wait_until(lambda: counter["n"] >= 3)
    result = sampler.stop()

    assert result.cpu_seconds >= 3.0
    assert result.peak_mem_mb >= 30.0


def test_sampler_survives_a_failing_sample() -> None:
    def boom() -> ResourceSample:
        raise RuntimeError("no stats")

    sampler = StatsSampler(boom, interval=0.005).start()
    time.sleep(0.02)

    assert sampler.stop() == ResourceSample()


def test_sampler_context_manager() -> None:
    with StatsSampler(lambda: ResourceSample(2.0, 5.0), interval=0.005) as sampler:
        _wait_until(lambda: sampler.result.cpu_seconds > 0)

    assert sampler.result == ResourceSample(2.0, 5.0)
