"""Pure helpers of the allowlisting egress proxy (no Docker daemon required)."""

from __future__ import annotations

import json
import re

from janus.sandbox.proxy import (
    PROXY_PORT,
    ProxySession,
    proxy_image_tag,
    render_proxy_script,
)
from janus.sandbox.settings import SandboxSettings

_ALLOW_HOSTS_RE = re.compile(r"^ALLOW_HOSTS = (\[.*\])$", re.MULTILINE)


def _embedded_hosts(script: str) -> list[str]:
    match = _ALLOW_HOSTS_RE.search(script)
    assert match is not None, "the script must embed ALLOW_HOSTS as a JSON literal"
    parsed = json.loads(match.group(1))
    assert isinstance(parsed, list)
    return parsed


def test_render_proxy_script_embeds_hosts_as_json() -> None:
    script = render_proxy_script(["pypi.org", ".github.com"])
    assert _embedded_hosts(script) == ["pypi.org", ".github.com"]


def test_render_proxy_script_round_trips_an_empty_list() -> None:
    assert _embedded_hosts(render_proxy_script([])) == []


def test_render_proxy_script_contains_the_deny_and_connect_branches() -> None:
    script = render_proxy_script(["pypi.org"])
    assert "403 Forbidden" in script
    assert "CONNECT" in script
    assert "200 Connection Established" in script


def test_render_proxy_script_embeds_the_proxy_port() -> None:
    script = render_proxy_script(["pypi.org"])
    assert f"_PORT = {PROXY_PORT}" in script


def test_render_proxy_script_uses_only_the_standard_library() -> None:
    script = render_proxy_script(["pypi.org"])
    assert "import asyncio" in script
    assert "import json" in script


def test_proxy_image_tag_is_deterministic() -> None:
    assert proxy_image_tag() == proxy_image_tag()


def test_proxy_image_tag_is_independent_of_the_run_host_list() -> None:
    """The image is shared and content-addressed, so the host list must not change the tag."""
    before = proxy_image_tag()
    render_proxy_script(["pypi.org", "github.com"])
    render_proxy_script([".internal.example"])
    assert proxy_image_tag() == before


def test_proxy_image_tag_has_the_prefix() -> None:
    assert proxy_image_tag().startswith("janus-sandbox-proxy:")


def test_proxy_session_env_points_at_the_proxy_host_and_port() -> None:
    settings = SandboxSettings(proxy_port=9999)
    session = ProxySession(object(), settings, allow_hosts=["pypi.org"], run_id="abc123")

    env = session.env()

    expected = f"http://{session.proxy_host}:9999"
    assert env == {
        "HTTP_PROXY": expected,
        "HTTPS_PROXY": expected,
        "http_proxy": expected,
        "https_proxy": expected,
    }


def test_proxy_session_names_are_owned_by_the_run_id() -> None:
    session = ProxySession(object(), SandboxSettings(), allow_hosts=["pypi.org"], run_id="abc123")
    assert session.internal_network == "janus-sbx-abc123"
    assert session.upstream_network == "janus-sbx-up-abc123"
    assert session.proxy_host == "janus-sbx-proxy-abc123"
