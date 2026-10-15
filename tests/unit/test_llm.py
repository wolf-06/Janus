"""LLM client: null behaviour, HTTP success/failures and settings wiring."""

from __future__ import annotations

import json

import httpx
import pytest

from janus.app.config import Settings
from janus.core.generate.llm import (
    LLMError,
    NullLLM,
    OpenAICompatibleClient,
    build_llm,
)


def _client(handler: httpx.MockTransport) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        base_url="https://llm.example/v1/",
        api_key="super-secret-key",
        model="test-model",
        client=httpx.Client(transport=handler),
    )


def test_null_llm_is_unavailable_and_returns_empty() -> None:
    client = NullLLM()

    assert client.available is False
    assert client.complete("hello", max_tokens=10) == ""
    assert client.complete("hello", max_tokens=10, temperature=0.7) == ""


def test_success_posts_expected_payload_and_returns_content() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("Authorization")
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "diff --git a/x b/x"}}]}
        )

    client = _client(httpx.MockTransport(handler))
    result = client.complete("fix this", max_tokens=123, temperature=0.2)

    assert result == "diff --git a/x b/x"
    assert seen["url"] == "https://llm.example/v1/chat/completions"
    assert "super-secret-key" not in str(seen["url"])
    assert seen["authorization"] == "Bearer super-secret-key"
    assert seen["payload"] == {
        "model": "test-model",
        "messages": [{"role": "user", "content": "fix this"}],
        "max_tokens": 123,
        "temperature": 0.2,
    }


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="server exploded"),
        httpx.Response(401, text="unauthorized"),
    ],
)
def test_non_2xx_raises_llm_error(response: httpx.Response) -> None:
    client = _client(httpx.MockTransport(lambda request: response))

    with pytest.raises(LLMError):
        client.complete("fix this", max_tokens=10)


def test_malformed_json_raises_llm_error() -> None:
    client = _client(httpx.MockTransport(lambda request: httpx.Response(200, text="not json")))

    with pytest.raises(LLMError):
        client.complete("fix this", max_tokens=10)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"choices": []},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": None}}]},
    ],
)
def test_missing_content_raises_llm_error(body: dict[str, object]) -> None:
    client = _client(httpx.MockTransport(lambda request: httpx.Response(200, json=body)))

    with pytest.raises(LLMError):
        client.complete("fix this", max_tokens=10)


def test_transport_error_raises_llm_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    client = _client(httpx.MockTransport(handler))

    with pytest.raises(LLMError):
        client.complete("fix this", max_tokens=10)


def test_build_llm_falls_back_without_configuration() -> None:
    assert isinstance(build_llm(Settings(_env_file=None)), NullLLM)
    assert isinstance(
        build_llm(Settings(_env_file=None, llm_api_key="key")),
        NullLLM,
    )
    assert isinstance(
        build_llm(Settings(_env_file=None, llm_base_url="https://llm.example/v1")),
        NullLLM,
    )


def test_build_llm_builds_real_client_when_configured() -> None:
    settings = Settings(
        _env_file=None,
        llm_api_key="key",
        llm_base_url="https://llm.example/v1",
        llm_model="little-model",
    )

    client = build_llm(settings)

    assert isinstance(client, OpenAICompatibleClient)
    assert client.available is True
