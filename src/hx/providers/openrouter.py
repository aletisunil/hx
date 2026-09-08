"""OpenRouter provider.

Speaks the OpenAI-compatible chat-completions API over SSE. Two details matter
more than the rest:

1. ``usage: {"include": true}`` on the request body makes OpenRouter return
   ``usage.cost``, ``usage.cache_discount`` and
   ``usage.prompt_tokens_details.cached_tokens``. Without it the status bar
   cache and cost fields are guesses.
2. Anthropic and Gemini models need explicit ``cache_control`` markers (max 4
   per request); OpenAI/DeepSeek/Grok cache implicitly off a stable prefix.
   :class:`~hx.providers.models.CacheMode` decides which path is taken.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import httpx

from hx.core.messages import (
    Message,
    StopReason,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from hx.core.usage import TurnUsage
from hx.paths import auth_file
from hx.providers.base import ProviderError, ProviderRequest, StreamDelta, StreamEnd, StreamItem

API_BASE = "https://openrouter.ai/api/v1"
CHAT_COMPLETIONS = f"{API_BASE}/chat/completions"
MODELS = f"{API_BASE}/models"

DEFAULT_HEADERS = {
    "HTTP-Referer": "https://github.com/sunilaleti/hx",
    "X-Title": "HX",
}

MAX_CACHE_CONTROL_MARKERS = 4
"""Anthropic rejects a request carrying more than four. Not a soft limit."""

_STOP_REASONS = {
    "stop": StopReason.END_TURN,
    "end_turn": StopReason.END_TURN,
    "tool_calls": StopReason.TOOL_USE,
    "length": StopReason.MAX_TOKENS,
    "content_filter": StopReason.STOP_SEQUENCE,
}


class OpenRouterProvider:
    name = "openrouter"

    RETRY_STATUSES: ClassVar[frozenset[int]] = frozenset({408, 409, 429, 500, 502, 503, 504})

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = API_BASE,
        timeout: float = 600.0,
        max_retries: int = 3,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=15.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                **DEFAULT_HEADERS,
            },
        )

    async def astream(self, request: ProviderRequest) -> AsyncIterator[StreamItem]:
        """Stream one completion, retrying retryable failures with jittered backoff."""
        payload = self.build_payload(request)
        url = f"{self.base_url}/chat/completions"

        last_error: ProviderError | None = None
        for attempt in range(self.max_retries + 1):
            started = time.monotonic()
            try:
                async for item in self._stream_once(url, payload, started):
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
        payload: dict[str, Any],
        started: float,
    ) -> AsyncIterator[StreamItem]:
        state = _StreamState()
        try:
            async with self._client.stream("POST", url, json=payload) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode(errors="replace")
                    raise ProviderError(
                        _error_message(response.status_code, body),
                        status=response.status_code,
                        retryable=response.status_code in self.RETRY_STATUSES,
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    for item in state.consume(chunk):
                        yield item
        except httpx.HTTPError as exc:
            raise ProviderError(str(exc), retryable=True) from exc

        usage = state.usage
        usage.latency_ms = (time.monotonic() - started) * 1000
        yield StreamEnd(stop_reason=state.stop_reason, usage=usage)

    async def aclose(self) -> None:
        await self._client.aclose()

    def build_payload(self, request: ProviderRequest) -> dict[str, Any]:
        """Serialise the assembled context into the request body.

        Applies cache markers according to the model's cache mode and sets
        ``usage.include``. Key ordering is deterministic so the serialised
        prefix is byte-identical between turns.
        """
        context = request.context
        explicit_cache = bool(context.breakpoints)

        messages: list[dict[str, Any]] = [
            _system_message(context.system_text(), structured=explicit_cache)
        ]
        for message in context.messages:
            messages.extend(_encode_message(message, structured=explicit_cache))

        if explicit_cache:
            self._apply_cache_control(messages, context.breakpoints)

        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "stream": True,
            "usage": {"include": True},
        }
        if context.tools:
            payload["tools"] = [_encode_tool(tool) for tool in context.tools]
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.stop_sequences:
            payload["stop"] = list(request.stop_sequences)
        payload.update(request.extra)
        return payload

    def _apply_cache_control(
        self,
        messages: list[dict[str, Any]],
        breakpoints: tuple[int, ...],
    ) -> None:
        """Attach ``cache_control: {"type": "ephemeral"}`` at the given message
        indices, in place. Silently caps at four markers - exceeding the limit is
        a hard API error."""
        applied = 0
        for index in sorted(set(breakpoints)):
            if applied >= MAX_CACHE_CONTROL_MARKERS:
                return
            if not 0 <= index < len(messages):
                continue
            content = messages[index].get("content")
            if not isinstance(content, list) or not content:
                continue
            content[-1]["cache_control"] = {"type": "ephemeral"}
            applied += 1

    def _parse_usage(self, raw: dict[str, Any]) -> TurnUsage:
        """Map OpenRouter's usage object to :class:`~hx.core.usage.TurnUsage`.

        ``prompt_tokens`` already includes ``cached_tokens``, so the cached
        portion is subtracted out to keep the cost maths honest.
        """
        return parse_usage(raw)


class _StreamState:
    """Accumulates SSE chunks into stream items.

    Tool-call arguments arrive as JSON fragments spread over many chunks, so
    they are buffered per index and only emitted once the stream ends.
    """

    def __init__(self) -> None:
        self.stop_reason = StopReason.END_TURN
        self.usage = TurnUsage()
        self._tools: dict[int, dict[str, str]] = {}
        self._finished_tools = False

    def consume(self, chunk: dict[str, Any]) -> list[StreamItem]:
        items: list[StreamItem] = []

        if raw_usage := chunk.get("usage"):
            self.usage = parse_usage(raw_usage)

        choices = chunk.get("choices") or []
        if not choices:
            return items

        choice = choices[0]
        delta = choice.get("delta") or {}

        if text := delta.get("content"):
            items.append(StreamDelta(text=text))
        if reasoning := (delta.get("reasoning") or delta.get("reasoning_content")):
            items.append(StreamDelta(thinking=reasoning))

        for call in delta.get("tool_calls") or []:
            index = int(call.get("index", 0))
            slot = self._tools.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if call_id := call.get("id"):
                slot["id"] = call_id
            function = call.get("function") or {}
            if name := function.get("name"):
                slot["name"] = name
            if arguments := function.get("arguments"):
                slot["arguments"] += arguments

        if finish := choice.get("finish_reason"):
            self.stop_reason = _STOP_REASONS.get(finish, StopReason.END_TURN)
            items.extend(self._flush_tools())

        return items

    def _flush_tools(self) -> list[StreamItem]:
        if self._finished_tools:
            return []
        self._finished_tools = True
        items: list[StreamItem] = []
        for index in sorted(self._tools):
            slot = self._tools[index]
            items.append(
                StreamDelta(
                    tool_use_id=slot["id"] or f"call_{index}",
                    tool_name=slot["name"],
                    tool_input_json=slot["arguments"] or "{}",
                )
            )
        if items:
            self.stop_reason = StopReason.TOOL_USE
        return items


def parse_usage(raw: dict[str, Any]) -> TurnUsage:
    """Map an OpenRouter usage object onto :class:`~hx.core.usage.TurnUsage`."""
    prompt = int(raw.get("prompt_tokens") or 0)
    details = raw.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0)
    completion_details = raw.get("completion_tokens_details") or {}
    cost = raw.get("cost")
    return TurnUsage(
        # prompt_tokens counts cached tokens too; bill them once, at the cache rate.
        input_tokens=max(prompt - cached, 0),
        output_tokens=int(raw.get("completion_tokens") or 0),
        cache_read_tokens=cached,
        cache_write_tokens=int(details.get("cache_creation_tokens") or 0),
        reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
        cost_usd=float(cost) if cost is not None else None,
    )


def _system_message(text: str, *, structured: bool) -> dict[str, Any]:
    if structured:
        return {"role": "system", "content": [{"type": "text", "text": text}]}
    return {"role": "system", "content": text}


def _encode_message(message: Message, *, structured: bool) -> list[dict[str, Any]]:
    """Convert one transcript message into wire messages.

    Tool results become ``role: "tool"`` entries, which is why one transcript
    message can expand into several wire messages.
    """
    if results := [b for b in message.content if isinstance(b, ToolResultBlock)]:
        return [
            {
                "role": "tool",
                "tool_call_id": block.tool_use_id,
                "content": block.content,
            }
            for block in results
        ]

    text_parts = [b.text for b in message.content if isinstance(b, TextBlock | ThinkingBlock)]
    text = "".join(b.text for b in message.content if isinstance(b, TextBlock))
    tool_uses = [b for b in message.content if isinstance(b, ToolUseBlock)]

    if message.role == "assistant":
        entry: dict[str, Any] = {"role": "assistant", "content": text or None}
        if tool_uses:
            entry["tool_calls"] = [
                {
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(block.input, sort_keys=True),
                    },
                }
                for block in tool_uses
            ]
        return [entry]

    body = text or "".join(text_parts)
    if structured:
        return [{"role": message.role, "content": [{"type": "text", "text": body}]}]
    return [{"role": message.role, "content": body}]


def _encode_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Wrap a bare tool schema in the OpenAI function-calling envelope."""
    if tool.get("type") == "function":
        return tool
    return {
        "type": "function",
        "function": {
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema") or tool.get("parameters") or {},
        },
    }


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter, so retries from concurrent subagents do
    not synchronise into a thundering herd."""
    return float(min(2**attempt, 8)) * (0.5 + random.random())


def _error_message(status: int, body: str) -> str:
    try:
        parsed = json.loads(body)
        message = parsed.get("error", {}).get("message") or parsed.get("message")
    except json.JSONDecodeError:
        message = None
    return f"OpenRouter {status}: {message or body[:400]}"


async def fetch_models(api_key: str, base_url: str = API_BASE) -> list[dict[str, Any]]:
    """GET ``/models``. Used by :class:`~hx.providers.models.ModelRegistry`."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}", **DEFAULT_HEADERS},
        )
        if response.status_code >= 400:
            raise ProviderError(_error_message(response.status_code, response.text))
        data = response.json()
    models = data.get("data", [])
    return list(models) if isinstance(models, list) else []


def load_api_key() -> str:
    """Resolve the key: ``HX_OPENROUTER_API_KEY``, then ``OPENROUTER_API_KEY``,
    then ``~/.hx/auth.json``.

    Raises:
        MissingAPIKey: so the TUI can show the onboarding prompt.
    """
    for name in ("HX_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"):
        if key := os.environ.get(name):
            return key

    path = auth_file()
    if path.is_file():
        try:
            stored = json.loads(path.read_text()).get("openrouter_api_key")
        except (OSError, json.JSONDecodeError):
            stored = None
        if stored:
            return str(stored)

    raise MissingAPIKey(
        "No OpenRouter API key found. Set OPENROUTER_API_KEY, or run `hx` interactively "
        "to save one. Get a key at https://openrouter.ai/keys"
    )


def save_api_key(key: str) -> None:
    """Persist to ``~/.hx/auth.json`` with mode 0600."""
    path = auth_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
    data["openrouter_api_key"] = key

    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    # Restrict before the rename so the key is never briefly world-readable.
    tmp.chmod(0o600)
    tmp.replace(path)


class MissingAPIKey(Exception):
    pass
