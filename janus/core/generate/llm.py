"""OpenAI-compatible chat completions behind a tiny, testable interface.

:class:`LLMClient` is the only surface the fix loop sees. :class:`NullLLM` is the safe
default when no endpoint is configured; :class:`OpenAICompatibleClient` performs the
single network call. The HTTP client is injectable so tests can use an
``httpx.MockTransport`` without a network, and the API key is sent only as a
``Bearer`` header — never in the request URL and never logged.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Protocol, cast

import httpx

from janus.app.config import Settings


class LLMError(RuntimeError):
    """Raised when the endpoint is unreachable or returns an unusable response."""


class LLMClient(Protocol):
    """A minimal chat-completion client."""

    available: bool
    """Whether this client can actually produce completions."""

    def complete(self, prompt: str, *, max_tokens: int, temperature: float = 0.0) -> str:
        """Return the model's text completion for ``prompt``."""
        ...


class NullLLM:
    """A disabled client: reports unavailable and returns an empty completion."""

    available = False

    def complete(self, prompt: str, *, max_tokens: int, temperature: float = 0.0) -> str:
        del prompt, max_tokens, temperature
        return ""


class OpenAICompatibleClient:
    """A client for the OpenAI ``POST /chat/completions`` shape.

    ``base_url`` is the API root (for example ``https://api.openai.com/v1``); the
    ``/chat/completions`` path is appended. The key lives only in the request header.
    """

    available = True

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def close(self) -> None:
        """Close the underlying HTTP client when this instance created it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OpenAICompatibleClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def complete(self, prompt: str, *, max_tokens: int, temperature: float = 0.0) -> str:
        """POST the prompt and return ``choices[0].message.content``.

        Any transport failure, non-2xx status, malformed JSON or missing content is
        reported as :class:`LLMError`. The API key is never included in an error
        message or a URL.
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        try:
            response = self._client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._headers,
            )
        except httpx.HTTPError as exc:
            raise LLMError("LLM request failed") from exc

        if not 200 <= response.status_code < 300:
            raise LLMError(f"LLM request failed with status {response.status_code}")

        try:
            data = cast("dict[str, Any]", response.json())
        except ValueError as exc:
            raise LLMError("LLM returned malformed JSON") from exc

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("LLM response is missing choices[0].message.content") from exc
        if not isinstance(content, str):
            raise LLMError("LLM response is missing choices[0].message.content")
        return content


def build_llm(settings: Settings) -> LLMClient:
    """Build the configured client, falling back to :class:`NullLLM`.

    Both ``llm_api_key`` and ``llm_base_url`` are required: a key without an endpoint
    (or vice versa) cannot make a useful request, so the safe null client is returned.
    """
    if not settings.llm_api_key or not settings.llm_base_url:
        return NullLLM()
    return OpenAICompatibleClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout=settings.llm_timeout_seconds,
    )
