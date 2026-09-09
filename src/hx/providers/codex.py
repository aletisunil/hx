"""Codex provider - ChatGPT Plus/Pro subscription over the Responses API.

Talks to ``chatgpt.com/backend-api/codex/responses``, the endpoint OpenAI
exposes for OSS harnesses. Wire encoding lives in
:mod:`hx.providers.responses_codec`; this module is transport only.

Unlike :class:`~hx.providers.openrouter.OpenRouterProvider`, the credential is
*not* baked into the client. OAuth access tokens expire in the middle of a long
session, so the token is fetched per request through a resolver that refreshes
it when needed.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, ClassVar

import httpx

from hx import __version__
from hx.auth.oauth.codex import ORIGINATOR
from hx.auth.resolve import ResolvedAuth
from hx.providers.base import ProviderError, ProviderRequest, StreamItem
from hx.providers.responses_codec import StreamState, build_body, stream_end

API_BASE = "https://chatgpt.com/backend-api"
RESPONSES_PATH = "/codex/responses"

MODEL_PREFIX = "openai-codex/"
"""HX namespaces model ids by provider; the wire wants the bare id."""

TokenSource = Callable[[], Awaitable[ResolvedAuth]]


class CodexProvider:
    name = "openai-codex"

    RETRY_STATUSES: ClassVar[frozenset[int]] = frozenset({408, 409, 429, 500, 502, 503, 504})

    def __init__(
        self,
        auth: TokenSource,
        *,
        base_url: str = API_BASE,
        session_id: str | None = None,
        reasoning_effort: str | None = "medium",
        timeout: float = 600.0,
        max_retries: int = 3,
    ) -> None:
        self._auth = auth
        self.base_url = base_url.rstrip("/")
        self.session_id = session_id
        self.reasoning_effort = reasoning_effort
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0))

    def set_session_id(self, session_id: str) -> None:
        """The session id is the prompt-cache key, so it must follow /clear."""
        self.session_id = session_id

    async def astream(self, request: ProviderRequest) -> AsyncIterator[StreamItem]:
        body = build_body(
            request,
            session_id=self.session_id,
            reasoning_effort=self.reasoning_effort,
        )
        body["model"] = wire_model(request.model)
        url = f"{self.base_url}{RESPONSES_PATH}"

        last_error: ProviderError | None = None
        for attempt in range(self.max_retries + 1):
            started = time.monotonic()
            try:
                async for item in self._stream_once(url, body, started):
                    yield item
                return
            except ProviderError as exc:
                # Only retry before anything was yielded; a mid-stream restart
                # would duplicate text the consumer has already rendered.
                if not exc.retryable or attempt == self.max_retries:
                    raise
                last_error = exc
                await asyncio.sleep(_backoff(attempt))
        if last_error is not None:  # pragma: no cover - defensive
            raise last_error

    async def _stream_once(
        self,
        url: str,
        body: dict[str, Any],
        started: float,
    ) -> AsyncIterator[StreamItem]:
        headers = await self._headers()
        state = StreamState()
        try:
            async with self._client.stream("POST", url, json=body, headers=headers) as response:
                if response.status_code >= 400:
                    raw = (await response.aread()).decode(errors="replace")
                    raise ProviderError(
                        _error_message(response.status_code, raw),
                        status=response.status_code,
                        retryable=response.status_code in self.RETRY_STATUSES,
                    )
                async for event in _iter_sse(response):
                    for item in state.consume(event):
                        yield item
        except httpx.HTTPError as exc:
            raise ProviderError(str(exc), retryable=True) from exc

        yield stream_end(state, (time.monotonic() - started) * 1000)

    async def _headers(self) -> dict[str, str]:
        """Built per request so a token refreshed mid-session takes effect."""
        auth = await self._auth()
        account_id = auth.extra.get("account_id")
        if not account_id:
            raise ProviderError(
                "No ChatGPT account id on the stored login. Run `hx auth login openai-codex`."
            )

        headers = {
            "Authorization": f"Bearer {auth.token}",
            "chatgpt-account-id": str(account_id),
            "originator": ORIGINATOR,
            "User-Agent": f"hx/{__version__}",
            "OpenAI-Beta": "responses=experimental",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if self.session_id:
            headers["session-id"] = self.session_id
            headers["x-client-request-id"] = self.session_id
        return headers

    async def aclose(self) -> None:
        await self._client.aclose()


async def _iter_sse(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Yield decoded ``data:`` payloads.

    Event names arrive on their own ``event:`` lines and are ignored - every
    payload already carries its own ``type``.
    """
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            yield parsed


def wire_model(model_id: str) -> str:
    """Strip the HX provider namespace: ``openai-codex/gpt-5.3-codex`` -> ``gpt-5.3-codex``."""
    return model_id[len(MODEL_PREFIX) :] if model_id.startswith(MODEL_PREFIX) else model_id


def _error_message(status: int, body: str) -> str:
    if status in (401, 403):
        return (
            "Codex rejected the credential (HTTP "
            f"{status}). Run `hx auth login openai-codex` to sign in again."
        )
    try:
        parsed = json.loads(body)
        detail = parsed.get("error")
        message = detail.get("message") if isinstance(detail, dict) else detail
        message = message or parsed.get("detail") or parsed.get("message")
    except (json.JSONDecodeError, AttributeError):
        message = None
    return f"Codex {status}: {message or body[:400]}"


def _backoff(attempt: int) -> float:
    """Exponential with jitter, matching the OpenRouter provider."""
    return min(2.0**attempt, 8.0) * (0.5 + random.random() / 2)
