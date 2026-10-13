"""The ENV/PIPELINE case: a failure with an infrastructure signature.

Unlike the logic bug, this test does not fail an assertion — it fails because its
environment is not configured. That is the shape the triage ENV route looks for (a missing
configuration value, a refused connection), as opposed to a wrong-result assertion.

* ``JANUS_SAMPLE_ENDPOINT`` unset — raises a misconfiguration error immediately.
* ``JANUS_SAMPLE_ENDPOINT=host:port`` — dials the endpoint; an unreachable endpoint
  surfaces as a connection error (e.g. ``ConnectionRefusedError``).

See ``README.md`` for how to drive it.
"""

from __future__ import annotations

import os
import socket


def test_requires_service_endpoint() -> None:
    endpoint = os.environ.get("JANUS_SAMPLE_ENDPOINT", "").strip()
    if not endpoint:
        raise RuntimeError(
            "infrastructure misconfigured: JANUS_SAMPLE_ENDPOINT is not set "
            "(the service this test depends on was never provisioned)"
        )

    host, _, port = endpoint.partition(":")
    with socket.create_connection((host, int(port)), timeout=1.0):
        pass
