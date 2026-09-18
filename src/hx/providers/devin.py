"""Devin provider - a Devin subscription over Codeium's Cascade backend.

One turn is up to three calls:

1. ``GetUserJwt`` trades the stored session token for a short-lived user JWT.
   Cached until shortly before it expires, so a session pays for it rarely.
2. ``AssignModel``, only for a router model (``adaptive``): the router id is
   not a model the chat call accepts, so the server picks one and signs the
   choice.
3. ``GetChatMessage``, a Connect server stream.

Wire encoding lives in :mod:`hx.providers.devin_wire`; this module maps HX's
transcript onto Cascade's three history channels and the stream back onto
:class:`~hx.providers.base.StreamDelta`.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

import httpx

from hx.auth.oauth.devin import claims_of
from hx.auth.resolve import ResolvedAuth
from hx.core.messages import (
    Message,
    StopReason,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from hx.core.usage import TurnUsage
from hx.net import async_client
from hx.providers import devin_wire as wire
from hx.providers.base import ProviderError, ProviderRequest, StreamDelta, StreamEnd, StreamItem
from hx.providers.devin_catalogue import wire_model
from hx.providers.protowire import WireError
from hx.providers.responses_codec import encode_tool

if TYPE_CHECKING:
    from hx.providers.models import ModelInfo

TokenSource = Callable[[], Awaitable[ResolvedAuth]]
EffortSource = Callable[[str], str | None]
InfoSource = Callable[[str], "ModelInfo"]

JWT_LEEWAY = 60.0
"""Seconds before its expiry that a cached user JWT is replaced."""

JWT_FALLBACK_TTL = 300.0
"""How long to trust a user JWT whose expiry cannot be read."""

DEFAULT_TEMPERATURE = 0.4
"""What the Devin CLI sends. Cascade models are tuned against it."""

_CASCADE_NAMESPACE = uuid.UUID("5b1f0c3e-8a4d-4f0e-9a51-0d3c6e1d7a42")
"""Seeds the ids derived for history entries, so they are stable across turns."""

_ERRORS_TO_RETRY = frozenset({"unavailable", "deadline_exceeded", "aborted"})


@dataclass(slots=True)
class _CachedJwt:
    session_token: str
    jwt: str
    base_url: str
    expires: float


class DevinProvider:
    name = "devin"

    RETRY_STATUSES: ClassVar[frozenset[int]] = frozenset({408, 429, 500, 502, 503, 504})

    def __init__(
        self,
        auth: TokenSource,
        *,
        base_url: str = wire.DEFAULT_BASE_URL,
        session_id: str | None = None,
        effort: EffortSource | None = None,
        model_info: InfoSource | None = None,
        timeout: float = 600.0,
        max_retries: int = 3,
    ) -> None:
        self._auth = auth
        self.base_url = base_url.rstrip("/")
        self.session_id = session_id
        self.effort: EffortSource = effort if effort is not None else _no_effort
        self._model_info = model_info
        self.max_retries = max_retries
        self._jwt: _CachedJwt | None = None
        self._client = async_client(timeout=httpx.Timeout(timeout, connect=15.0))

    def set_session_id(self, session_id: str) -> None:
        """The session id names the Cascade thread, so it must follow /clear."""
        self.session_id = session_id

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- the turn ----------------------------------------------------------

    async def astream(self, request: ProviderRequest) -> AsyncIterator[StreamItem]:
        reminted = False
        attempt = 0
        while True:
            started = time.monotonic()
            yielded = False
            try:
                async for item in self._turn(request, started):
                    yielded = True
                    yield item
                return
            except ProviderError as exc:
                # Only retry before anything was yielded; a mid-stream restart
                # would duplicate text the consumer has already rendered.
                if yielded:
                    raise
                if exc.status == 401 and self._jwt is not None and not reminted:
                    # The cached user JWT may have been revoked before its
                    # expiry. Minting another settles it; a second 401 is real.
                    self._jwt = None
                    reminted = True
                    continue
                if not exc.retryable or attempt >= self.max_retries:
                    raise
                await asyncio.sleep(_backoff(attempt))
                attempt += 1

    async def _turn(self, request: ProviderRequest, started: float) -> AsyncIterator[StreamItem]:
        auth = await self._auth()
        jwt = await self._user_jwt(auth.token)
        info = self._info(request.model)
        cascade_id = cascade_id_for(self.session_id)
        prompts = encode_history(request.context.messages, cascade_id, request.model)

        model_uid = wire_model(info, self.effort(request.model))
        assignment_jwt = ""
        if info.model_router:
            assignment = await self._assign(
                auth.token, jwt.base_url, model_uid, cascade_id, prompts
            )
            model_uid, assignment_jwt = assignment.model_uid, assignment.jwt

        chat = wire.ChatRequest(
            token=auth.token,
            user_jwt=jwt.jwt,
            system_prompt=request.context.system_text(),
            prompts=prompts,
            model_uid=model_uid,
            cascade_id=cascade_id,
            execution_id=str(uuid.uuid4()),
            max_tokens=request.max_tokens,
            tools=encode_tools(request.context.tools, gemini=_is_gemini(model_uid)),
            model_assignment_jwt=assignment_jwt,
            temperature=(
                request.temperature if request.temperature is not None else DEFAULT_TEMPERATURE
            ),
            stop_patterns=(*wire.DEFAULT_STOP_PATTERNS, *request.stop_sequences),
        )
        async for item in self._stream(jwt.base_url, chat, started):
            yield item

    async def _stream(
        self, base_url: str, chat: wire.ChatRequest, started: float
    ) -> AsyncIterator[StreamItem]:
        state = _StreamState()
        reader = wire.FrameReader()
        body = wire.frame(chat.encode())
        try:
            async with self._client.stream(
                "POST", f"{base_url}{wire.CHAT_PATH}", content=body, headers=wire.STREAM_HEADERS
            ) as response:
                if response.status_code >= 400:
                    raise _http_error("chat", response.status_code, await response.aread())
                async for chunk in response.aiter_raw():
                    for frame in reader.feed(chunk):
                        if frame.end_stream:
                            error = wire.trailer_error(frame.payload)
                            if error is not None:
                                raise _stream_error(error)
                            continue
                        for item in state.consume(wire.parse_chat_chunk(frame.payload)):
                            yield item
        except httpx.HTTPError as exc:
            raise ProviderError(str(exc), retryable=True) from exc
        except WireError as exc:
            raise ProviderError(f"Devin sent an unreadable response: {exc}") from exc
        if reader.leftover:
            raise ProviderError("Devin's response ended mid-frame.", retryable=True)

        for item in state.finish():
            yield item
        usage = state.usage
        usage.latency_ms = (time.monotonic() - started) * 1000
        yield StreamEnd(stop_reason=state.stop_reason(), usage=usage)

    # -- supporting calls --------------------------------------------------

    async def _user_jwt(self, token: str) -> _CachedJwt:
        cached = self._jwt
        now = time.time()
        if cached is not None and cached.session_token == token and now < cached.expires:
            return cached
        answer = await request_user_jwt(self._client, self.base_url, token)
        exp = claims_of(answer.jwt).get("exp")
        expires = (
            float(exp) - JWT_LEEWAY
            if isinstance(exp, int | float) and exp > 0
            else now + JWT_FALLBACK_TTL
        )
        self._jwt = _CachedJwt(
            session_token=token,
            jwt=answer.jwt,
            base_url=answer.base_url or self.base_url,
            expires=expires,
        )
        return self._jwt

    async def _assign(
        self,
        token: str,
        base_url: str,
        router_uid: str,
        cascade_id: str,
        prompts: list[wire.ChatPrompt],
    ) -> wire.Assignment:
        """Resolve a router into a concrete model for this turn.

        The router scores the current request alone, sent without an id - the
        chat call that follows is what mints one.
        """
        current = next((p for p in reversed(prompts) if p.source == wire.SOURCE_USER), None)
        if current is not None:
            current = wire.ChatPrompt(message_id="", source=wire.SOURCE_USER, prompt=current.prompt)
        payload = await self._unary(
            base_url,
            wire.ASSIGN_MODEL_PATH,
            wire.assign_model_request(token, router_uid, cascade_id, current),
            "model assignment",
        )
        try:
            assignment = wire.parse_assignment(payload)
        except WireError as exc:
            raise ProviderError(f"Devin sent an unreadable model assignment: {exc}") from exc
        if not assignment.jwt or not assignment.model_uid:
            # The router id is never a legal chat model, so there is nothing
            # sensible to fall back to.
            raise ProviderError(f"Devin could not assign a model for {router_uid}.", retryable=True)
        return assignment

    async def _unary(self, base_url: str, path: str, body: bytes, operation: str) -> bytes:
        return await unary_call(self._client, base_url, path, body, operation)

    def _info(self, model_id: str) -> ModelInfo:
        if self._model_info is not None:
            return self._model_info(model_id)
        from hx.providers.models import ModelInfo, ModelPricing

        return ModelInfo(
            id=model_id,
            name=model_id,
            context_window=0,
            max_output_tokens=0,
            pricing=ModelPricing(),
            provider_id="devin",
            is_subscription=True,
        )


async def unary_call(
    client: httpx.AsyncClient, base_url: str, path: str, body: bytes, operation: str
) -> bytes:
    """One unary Connect call; failures become :class:`ProviderError`."""
    try:
        response = await client.post(f"{base_url}{path}", content=body, headers=wire.UNARY_HEADERS)
    except httpx.HTTPError as exc:
        raise ProviderError(str(exc), retryable=True) from exc
    if response.status_code >= 400:
        raise _http_error(operation, response.status_code, response.content)
    return response.content


async def request_user_jwt(client: httpx.AsyncClient, base_url: str, token: str) -> wire.UserJwt:
    """Trade the stored session token for a user JWT, and learn where to send turns.

    An enterprise tenant can be served from its own API server rather than the
    shared one; that host comes back here, so everything after this call goes
    to :attr:`~hx.providers.devin_wire.UserJwt.base_url` when it is set.
    """
    payload = await unary_call(
        client, base_url, wire.AUTH_PATH, wire.user_jwt_request(token), "sign-in"
    )
    try:
        answer = wire.parse_user_jwt(payload)
    except WireError as exc:
        raise ProviderError(f"Devin sent an unreadable sign-in response: {exc}") from exc
    if not answer.jwt:
        raise ProviderError(
            "Devin accepted the login but issued no user token. "
            "Run `hx auth login devin` to sign in again."
        )
    return answer


class _StreamState:
    """Accumulates one streamed turn.

    Tool calls arrive as argument fragments under an id that later fragments may
    omit; they are flushed whole at the end, since the loop builds a call only
    from a delta carrying id, name and arguments together.
    """

    def __init__(self) -> None:
        self.usage = TurnUsage()
        self._calls: dict[str, dict[str, str]] = {}
        self._active: str | None = None
        self._signature = ""
        self._stop = 0

    def consume(self, chunk: wire.ChatChunk) -> list[StreamItem]:
        items: list[StreamItem] = []
        if chunk.delta_thinking:
            items.append(StreamDelta(thinking=chunk.delta_thinking))
        if chunk.delta_signature:
            self._signature = chunk.delta_signature
        if chunk.delta_text:
            items.append(StreamDelta(text=chunk.delta_text))
        for call in chunk.tool_calls:
            self._accumulate(call)
        if chunk.stop_reason:
            self._stop = chunk.stop_reason
        if chunk.usage is not None:
            self.usage.input_tokens = chunk.usage.input_tokens
            self.usage.output_tokens = chunk.usage.output_tokens
            self.usage.cache_read_tokens = chunk.usage.cache_read_tokens
            self.usage.cache_write_tokens = chunk.usage.cache_write_tokens
        return items

    def _accumulate(self, call: wire.ToolCall) -> None:
        call_id = call.id or self._active
        if not call_id:
            return
        slot = self._calls.setdefault(call_id, {"name": "", "arguments": ""})
        if call.name:
            slot["name"] = call.name
        self._active = call_id
        if call.arguments_json:
            previous = slot["arguments"]
            # Some models resend the whole buffer, others only the new part.
            slot["arguments"] = (
                call.arguments_json
                if call.arguments_json.startswith(previous)
                else previous + call.arguments_json
            )

    def finish(self) -> list[StreamItem]:
        items: list[StreamItem] = []
        if self._signature:
            items.append(StreamDelta(thinking_signature=self._signature))
        for call_id, call in self._calls.items():
            items.append(
                StreamDelta(
                    tool_use_id=call_id,
                    tool_name=call["name"],
                    tool_input_json=call["arguments"] or "{}",
                )
            )
        return items

    def stop_reason(self) -> StopReason:
        if self._calls:
            return StopReason.TOOL_USE
        if self._stop == wire.STOP_MAX_TOKENS:
            return StopReason.MAX_TOKENS
        return StopReason.END_TURN


# -- history ------------------------------------------------------------------


def cascade_id_for(session_id: str | None) -> str:
    """A UUID naming the Cascade thread. HX session ids are not UUIDs, so one
    is derived; without a session every turn is its own thread."""
    if not session_id:
        return str(uuid.uuid4())
    return str(uuid.uuid5(_CASCADE_NAMESPACE, session_id))


def _entry_id(cascade_id: str, *parts: object) -> str:
    return str(uuid.uuid5(_CASCADE_NAMESPACE, "\0".join((cascade_id, *map(str, parts)))))


def encode_history(
    messages: list[Message], cascade_id: str, model_id: str
) -> list[wire.ChatPrompt]:
    """Map the transcript onto Cascade's USER, SYSTEM (assistant) and TOOL channels.

    Ids are derived from position, never from content, so they stay stable when
    history is rebuilt. A thinking signature is replayed only on turns this same
    model produced: another route's signature means nothing here, and Cascade
    rejects one it did not issue.
    """
    prompts: list[wire.ChatPrompt] = []
    for index, message in enumerate(messages):
        if message.role == "assistant":
            prompt = _assistant_prompt(message, cascade_id, index, native=message.model == model_id)
            if prompt is not None:
                prompts.append(prompt)
            continue
        if message.role != "user":
            continue
        # Results answer the assistant turn above them, so they come first.
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                prompts.append(
                    wire.ChatPrompt(
                        message_id=_entry_id(cascade_id, index, "tool", block.tool_use_id),
                        source=wire.SOURCE_TOOL,
                        prompt=block.content,
                        tool_call_id=block.tool_use_id,
                        tool_result_is_error=block.is_error,
                    )
                )
        text = message.text()
        if text:
            prompts.append(
                wire.ChatPrompt(
                    message_id=_entry_id(cascade_id, index, "user"),
                    source=wire.SOURCE_USER,
                    prompt=text,
                )
            )
    return prompts


def _assistant_prompt(
    message: Message, cascade_id: str, index: int, *, native: bool
) -> wire.ChatPrompt | None:
    text: list[str] = []
    thinking: list[str] = []
    signature = ""
    calls: list[wire.ToolCall] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            text.append(block.text)
        elif isinstance(block, ThinkingBlock):
            thinking.append(block.text)
            if native and not signature and block.signature:
                signature = block.signature
        elif isinstance(block, ToolUseBlock):
            calls.append(wire.ToolCall(block.id, block.name, json.dumps(block.input)))
    if not text and not any(thinking) and not signature and not calls:
        return None
    return wire.ChatPrompt(
        message_id=f"bot-{_entry_id(cascade_id, index, 'assistant')}",
        source=wire.SOURCE_SYSTEM,
        prompt="".join(text),
        thinking="".join(thinking),
        signature=signature,
        tool_calls=tuple(calls),
    )


# -- tools --------------------------------------------------------------------


def encode_tools(tools: list[dict[str, Any]], *, gemini: bool) -> list[wire.ToolDefinition]:
    definitions: list[wire.ToolDefinition] = []
    for tool in tools:
        encoded = encode_tool(tool)
        schema = encoded["parameters"] or {"type": "object", "properties": {}}
        if gemini:
            schema = gemini_schema(schema)
        definitions.append(
            wire.ToolDefinition(
                name=encoded["name"],
                description=encoded["description"],
                json_schema=json.dumps(schema, separators=(",", ":")),
            )
        )
    return definitions


def _is_gemini(model_uid: str) -> bool:
    uid = model_uid.lower()
    return "gemini" in uid


_GEMINI_DROPPED = frozenset(
    {"$schema", "$id", "additionalProperties", "patternProperties", "unevaluatedProperties"}
)


def gemini_schema(schema: Any) -> Any:
    """Reduce a JSON Schema to what Gemini's tool validator accepts.

    Cascade forwards tool schemas to Gemini untouched, and Gemini rejects a type
    array such as ``["string", "null"]`` - not with a 400 naming the field, but
    as an opaque ``invalid_argument`` that fails the whole turn.
    """
    if isinstance(schema, list):
        return [gemini_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _GEMINI_DROPPED:
            continue
        if key == "type" and isinstance(value, list):
            kinds = [kind for kind in value if kind != "null"]
            out["type"] = kinds[0] if kinds else "string"
            if len(kinds) < len(value):
                out["nullable"] = True
            continue
        if key == "const":
            out["enum"] = [value]
            continue
        if key in ("properties", "$defs", "definitions") and isinstance(value, dict):
            out[key] = {name: gemini_schema(sub) for name, sub in value.items()}
            continue
        out[key] = gemini_schema(value)
    return out


# -- errors -------------------------------------------------------------------


def _http_error(operation: str, status: int, body: bytes) -> ProviderError:
    error = wire.unary_error(body)
    if status in (401, 403):
        return ProviderError(
            f"Devin rejected the credential (HTTP {status}). "
            "Run `hx auth login devin` to sign in again.",
            status=status,
            retryable=False,
        )
    detail = error.message if error is not None and error.message else _plain(body)
    return ProviderError(
        f"Devin {operation} failed ({status}){': ' + detail if detail else ''}",
        status=status,
        retryable=status in DevinProvider.RETRY_STATUSES,
    )


def _stream_error(error: wire.ConnectError) -> ProviderError:
    code = error.code.lower()
    if code == "unauthenticated":
        return ProviderError(
            f"Devin rejected the credential: {error.message}. "
            "Run `hx auth login devin` to sign in again.",
            status=401,
        )
    if code == "resource_exhausted":
        return ProviderError(f"Devin usage limit reached: {error.message}", status=429)
    return ProviderError(
        f"Devin stream error {error.code}: {error.message}",
        retryable=code in _ERRORS_TO_RETRY,
    )


def _plain(body: bytes) -> str:
    """A short, printable excerpt of an error body - never HTML or binary."""
    try:
        text = body.decode()
    except UnicodeDecodeError:
        return ""
    text = " ".join(text.split())
    if text.lower().startswith(("<!doctype", "<html")) or not text.isprintable():
        return ""
    return text[:400]


def _no_effort(model_id: str) -> str | None:
    return None


def _backoff(attempt: int) -> float:
    """Exponential with jitter, matching the other providers."""
    return min(2.0**attempt, 8.0) * (0.5 + random.random() / 2)
