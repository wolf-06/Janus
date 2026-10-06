"""Resource sampling for a running sandbox.

Docker's stats API is a stream and returns nothing once a container has exited, so usage
must be sampled *while* the container runs. :class:`StatsSampler` polls a callable on a
background thread and keeps the peak memory and cumulative CPU seconds.

The sampler knows nothing about Docker: the caller passes a function returning a
:class:`ResourceSample`. That keeps it testable without a daemon.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ResourceSample:
    """A point-in-time reading."""

    cpu_seconds: float = 0.0
    peak_mem_mb: float = 0.0


class StatsSampler:
    """Poll ``sample`` in the background and remember the high-water marks."""

    def __init__(
        self,
        sample: Callable[[], ResourceSample],
        *,
        interval: float = 0.5,
    ) -> None:
        self._sample = sample
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._result = ResourceSample()

    def _loop(self) -> None:
        cpu_seconds = 0.0
        peak_mem_mb = 0.0
        while not self._stop.is_set():
            try:
                current = self._sample()
            except Exception:  # noqa: BLE001 - sampling must never break the run
                break
            cpu_seconds = max(cpu_seconds, current.cpu_seconds)
            peak_mem_mb = max(peak_mem_mb, current.peak_mem_mb)
            self._result = ResourceSample(cpu_seconds, peak_mem_mb)
            self._stop.wait(self._interval)

    def start(self) -> StatsSampler:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> ResourceSample:
        """Stop sampling and return the high-water marks."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 1.0)
        return self._result

    @property
    def result(self) -> ResourceSample:
        """The latest reading; safe to call while running."""
        return self._result

    def __enter__(self) -> StatsSampler:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
