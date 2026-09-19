"""Post a signed ``workflow_run`` webhook to a running Janus instance.

Exercises the real signature-verification and persistence path without GitHub. Pair it
with a tunnel when you want the full smee.io/ngrok end-to-end flow.

Usage::

    JANUS_GITHUB_WEBHOOK_SECRET=... \\
        uv run python scripts/post_test_webhook.py --url http://localhost:8000/webhooks/github

Exit code is non-zero unless the endpoint answers 202.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from hashlib import sha256
from hmac import new as hmac_new

import httpx


def build_payload() -> dict[str, object]:
    return {
        "action": "completed",
        "installation": {"id": 99, "app_id": 123},
        "repository": {
            "id": 555,
            "name": "widgets",
            "owner": {"login": "acme"},
            "default_branch": "main",
        },
        "workflow_run": {
            "id": 777,
            "head_sha": "deadbeef",
            "head_branch": "main",
            "conclusion": "failure",
            "status": "completed",
            "event": "push",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default="http://localhost:8000/webhooks/github",
        help="webhook endpoint to POST to",
    )
    parser.add_argument(
        "--delivery",
        default="local-smoke-1",
        help="X-GitHub-Delivery id (same id twice is deduplicated)",
    )
    args = parser.parse_args()

    secret = os.environ.get("JANUS_GITHUB_WEBHOOK_SECRET")
    if not secret:
        print("JANUS_GITHUB_WEBHOOK_SECRET is not set", file=sys.stderr)
        return 2

    body = json.dumps(build_payload()).encode()
    signature = "sha256=" + hmac_new(secret.encode(), body, sha256).hexdigest()

    response = httpx.post(
        args.url,
        content=body,
        headers={
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": "workflow_run",
            "X-GitHub-Delivery": args.delivery,
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )
    print(f"{response.status_code} {response.text}")
    return 0 if response.status_code == 202 else 1


if __name__ == "__main__":
    raise SystemExit(main())
