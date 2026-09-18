"""Devin transport against a scripted Cascade server: the whole turn, over HTTP."""

from __future__ import annotations

import base64
import json
import struct
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from hx.auth.resolve import ResolvedAuth
from hx.core.context import AssembledContext, PromptSection
from hx.core.messages import (
    Message,
    StopReason,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from hx.providers import devin_wire as wire
from hx.providers.base import ProviderError, ProviderRequest, StreamDelta, StreamEnd
from hx.providers.devin import DevinProvider, cascade_id_for, encode_history, gemini_schema
from hx.providers.models import ModelInfo, ModelPricing
from hx.providers.protowire import Fields, WireError, Writer, parse


def jwt(claims: dict[str, Any]) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def chunk(**fields: Any) -> bytes:
    """One ``GetChatMessageResponse`` frame."""
    message = Writer()
    message.put_str(3, fields.get("text", ""))
    message.put_uint(5, fields.get("stop", 0))
    for call_id, name, args in fields.get("calls", ()):
        call = Writer()
        call.put_str(1, call_id)
        call.put_str(2, name)
        call.put_str(3, args)
        message.put_message(6, call)
    if "usage" in fields:
        usage = Writer()
        for number, value in zip((2, 3, 4, 5), fields["usage"], strict=True):
            usage.put_uint(number, value)
        message.put_message(7, usage)
    message.put_str(9, fields.get("thinking", ""))
    message.put_str(10, fields.get("signature", ""))
    return wire.frame(message.finish())


def trailer(error: dict[str, str] | None = None) -> bytes:
    body = json.dumps({"error": error} if error else {}).encode()
    return struct.pack(">BI", wire.CONNECT_END_STREAM, len(body)) + body


def unary(build: Callable[[Writer], None]) -> bytes:
    message = Writer()
    build(message)
    return message.finish()


class Server:
    """A Cascade backend that answers with whatever the test scripted."""

    def __init__(self) -> None:
        self.chat_frames: list[bytes] = [chunk(text="hi"), trailer()]
        self.chat_status = 200
        self.user_jwt = jwt({"exp": 4_000_000_000})
        self.requests: dict[str, list[Fields]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == wire.CHAT_PATH:
            body = request.content
            (flags, length) = struct.unpack(">BI", body[:5])
            assert flags == wire.CONNECT_COMPRESSED
            import gzip

            self.requests.setdefault(path, []).append(parse(gzip.decompress(body[5 : 5 + length])))
            if self.chat_status != 200:
                return httpx.Response(
                    self.chat_status, json={"code": "unauthenticated", "message": "nope"}
                )
            # A stream, not `content=`: the provider reads it raw, frame by frame.
            return httpx.Response(200, stream=httpx.ByteStream(b"".join(self.chat_frames)))

        self.requests.setdefault(path, []).append(parse(request.content))
        if path == wire.AUTH_PATH:
            return httpx.Response(200, content=unary(lambda m: m.put_str(1, self.user_jwt)))
        if path == wire.ASSIGN_MODEL_PATH:

            def assignment(message: Writer) -> None:
                inner = Writer()
                inner.put_str(1, "assign-jwt")
                inner.put_str(2, "claude-sonnet-5")
                message.put_message(1, inner)

            return httpx.Response(200, content=unary(assignment))
        return httpx.Response(404)

    def calls(self, path: str) -> list[Fields]:
        return self.requests.get(path, [])


def info(model_id: str, **kwargs: Any) -> ModelInfo:
    return ModelInfo(
        id=model_id,
        name=model_id,
        context_window=200_000,
        max_output_tokens=64_000,
        pricing=ModelPricing(),
        provider_id="devin",
        is_subscription=True,
        **kwargs,
    )


def provider(
    server: Server, catalogue: dict[str, ModelInfo] | None = None, **kwargs: Any
) -> DevinProvider:
    async def token() -> ResolvedAuth:
        return ResolvedAuth(token="session-abc", source="test")

    known = catalogue or {}
    devin = DevinProvider(
        token,
        model_info=lambda model_id: known.get(model_id) or info(model_id),
        max_retries=0,
        **kwargs,
    )
    devin._client = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    return devin


def request(model: str = "devin/swe-1-6", messages: list[Message] | None = None) -> ProviderRequest:
    context = AssembledContext(
        system=[PromptSection(name="base", text="You are HX.")],
        messages=messages or [Message(role="user", content=[TextBlock("hello")])],
        tools=[
            {
                "name": "Read",
                "description": "Read a file",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        ],
    )
    return ProviderRequest(context=context, model=model, max_tokens=4096)


async def collect(devin: DevinProvider, req: ProviderRequest) -> list[Any]:
    return [item async for item in devin.astream(req)]


async def test_a_turn_streams_thinking_text_tools_and_usage() -> None:
    server = Server()
    server.chat_frames = [
        chunk(thinking="Let me look", signature="sig-1"),
        chunk(text="Reading "),
        chunk(text="now.", calls=[("call-1", "Read", '{"pa')]),
        # A later fragment may omit the id; it belongs to the call in progress.
        chunk(calls=[("", "", 'th": "a.py"}')]),
        chunk(stop=wire.STOP_MAX_TOKENS, usage=(100, 20, 5, 900)),
        trailer(),
    ]
    items = await collect(provider(server), request())

    deltas = [item for item in items if isinstance(item, StreamDelta)]
    assert [d.thinking for d in deltas if d.thinking] == ["Let me look"]
    assert "".join(d.text for d in deltas if d.text) == "Reading now."
    [signature] = [d.thinking_signature for d in deltas if d.thinking_signature]
    assert signature == "sig-1"
    [call] = [d for d in deltas if d.tool_use_id]
    assert (call.tool_use_id, call.tool_name) == ("call-1", "Read")
    assert json.loads(call.tool_input_json or "") == {"path": "a.py"}

    end = items[-1]
    assert isinstance(end, StreamEnd)
    # A tool call outranks the max-tokens stop: the loop must run the tool.
    assert end.stop_reason is StopReason.TOOL_USE
    assert (end.usage.input_tokens, end.usage.output_tokens) == (100, 20)
    assert (end.usage.cache_write_tokens, end.usage.cache_read_tokens) == (5, 900)


async def test_the_chat_request_carries_what_cascade_requires() -> None:
    server = Server()
    await collect(provider(server, session_id="20260917-101010-abcd1234"), request())

    [sent] = server.calls(wire.CHAT_PATH)
    metadata = sent.message(1)
    assert metadata is not None
    # The scheme prefix is required on the wire even though the store omits it.
    assert metadata.text(3) == "devin-session-token$session-abc"
    assert metadata.text(21) == server.user_jwt
    assert metadata.text(1) == "devin-cli"
    assert metadata.text(28) == "chisel"
    assert sent.text(2) == "You are HX."
    assert sent.text(21) == "swe-1-6"
    assert sent.uint(7) == wire.REQUEST_TYPE_CASCADE
    assert sent.text(16) == cascade_id_for("20260917-101010-abcd1234")
    [tool] = sent.messages(10)
    assert tool.text(1) == "Read"
    assert json.loads(tool.text(3))["properties"]["path"] == {"type": "string"}
    config = sent.message(8)
    assert config is not None and config.uint(2) == 4096
    assert "<|user|>" in config.texts(9)


async def test_the_user_jwt_is_minted_once_per_session() -> None:
    server = Server()
    devin = provider(server)
    await collect(devin, request())
    await collect(devin, request())
    assert len(server.calls(wire.AUTH_PATH)) == 1
    assert len(server.calls(wire.CHAT_PATH)) == 2


async def test_an_expired_user_jwt_is_replaced() -> None:
    server = Server()
    server.user_jwt = jwt({"exp": 1})
    devin = provider(server)
    await collect(devin, request())
    await collect(devin, request())
    assert len(server.calls(wire.AUTH_PATH)) == 2


async def test_a_rejected_user_jwt_is_reminted_once_then_reported() -> None:
    server = Server()
    server.chat_status = 401
    with pytest.raises(ProviderError, match="hx auth login devin"):
        await collect(provider(server), request())
    # First JWT, then one fresh one - not a loop.
    assert len(server.calls(wire.AUTH_PATH)) == 2


async def test_a_stream_error_in_the_trailer_is_raised_with_its_message() -> None:
    server = Server()
    server.chat_frames = [trailer({"code": "resource_exhausted", "message": "quota spent"})]
    with pytest.raises(ProviderError, match="quota spent"):
        await collect(provider(server), request())


async def test_an_unauthenticated_trailer_says_how_to_sign_in() -> None:
    server = Server()
    server.chat_frames = [trailer({"code": "unauthenticated", "message": "expired"})]
    with pytest.raises(ProviderError, match="hx auth login devin"):
        await collect(provider(server), request())


async def test_a_body_that_ends_mid_frame_is_an_error_not_a_short_answer() -> None:
    server = Server()
    server.chat_frames = [chunk(text="partial")[:-2]]
    with pytest.raises(ProviderError, match="mid-frame"):
        await collect(provider(server), request())


async def test_the_effort_picks_the_backend_model() -> None:
    server = Server()
    catalogue = {
        "devin/claude-opus-5": info(
            "devin/claude-opus-5",
            reasoning_levels=("high", "max"),
            default_reasoning_level="high",
            effort_routes=(("high", "opus-high"), ("max", "opus-max")),
        )
    }
    devin = provider(server, catalogue, effort=lambda model_id: "max")
    await collect(devin, request("devin/claude-opus-5"))
    assert server.calls(wire.CHAT_PATH)[0].text(21) == "opus-max"


async def test_a_router_model_is_assigned_before_the_chat() -> None:
    server = Server()
    catalogue = {"devin/adaptive": info("devin/adaptive", model_router=True)}
    await collect(provider(server, catalogue), request("devin/adaptive"))

    [assign] = server.calls(wire.ASSIGN_MODEL_PATH)
    assert assign.text(2) == "adaptive"
    scored = assign.message(5)
    assert scored is not None and scored.text(3) == "hello"
    # The router scores the request alone; the chat call mints the id.
    assert scored.text(1) == ""

    [chat] = server.calls(wire.CHAT_PATH)
    assert chat.text(21) == "claude-sonnet-5"
    assert chat.text(26) == "assign-jwt"
    assert chat.text(16) == assign.text(3), "assignment and chat must share the cascade"


def test_history_maps_onto_cascade_channels() -> None:
    messages = [
        Message(role="user", content=[TextBlock("read a.py")]),
        Message(
            role="assistant",
            model="devin/swe-1-6",
            content=[
                ThinkingBlock("thinking", signature="sig"),
                TextBlock("ok"),
                ToolUseBlock(id="c1", name="Read", input={"path": "a.py"}),
            ],
        ),
        Message(
            role="user", content=[ToolResultBlock(tool_use_id="c1", content="x = 1", is_error=True)]
        ),
    ]
    prompts = encode_history(messages, "cascade", "devin/swe-1-6")

    assert [p.source for p in prompts] == [wire.SOURCE_USER, wire.SOURCE_SYSTEM, wire.SOURCE_TOOL]
    assistant = prompts[1]
    assert (assistant.prompt, assistant.thinking, assistant.signature) == ("ok", "thinking", "sig")
    assert assistant.message_id.startswith("bot-")
    assert assistant.tool_calls[0].name == "Read"
    assert json.loads(assistant.tool_calls[0].arguments_json) == {"path": "a.py"}
    tool = prompts[2]
    assert (tool.tool_call_id, tool.prompt, tool.tool_result_is_error) == ("c1", "x = 1", True)

    # Stable across rebuilds, so the server can thread the same history.
    assert [p.message_id for p in encode_history(messages, "cascade", "devin/swe-1-6")] == [
        p.message_id for p in prompts
    ]


def test_a_signature_from_another_model_is_not_replayed() -> None:
    """Cascade rejects a signature it did not issue - and a Codex one is not even its format."""
    messages = [
        Message(
            role="assistant",
            model="openai-codex/gpt-5.5",
            content=[
                ThinkingBlock("thought", signature='[{"type": "reasoning"}]'),
                TextBlock("hi"),
            ],
        )
    ]
    [prompt] = encode_history(messages, "cascade", "devin/swe-1-6")
    assert prompt.signature == ""
    assert prompt.thinking == "thought"


def test_an_empty_assistant_turn_is_skipped() -> None:
    assert encode_history([Message(role="assistant", content=[])], "c", "devin/x") == []


def test_the_cascade_id_is_a_stable_uuid_per_session() -> None:
    import uuid

    first = cascade_id_for("20260917-101010-abcd1234")
    assert uuid.UUID(first)
    assert first == cascade_id_for("20260917-101010-abcd1234")
    assert first != cascade_id_for("20260917-101010-ffff0000")


def test_gemini_schemas_lose_what_gemini_rejects() -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "limit": {"type": ["integer", "null"]},
            "mode": {"const": "fast"},
            "additionalProperties": {"type": "string"},
        },
    }
    assert gemini_schema(schema) == {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "nullable": True},
            "mode": {"enum": ["fast"]},
            # A property that happens to share a keyword's name is still a property.
            "additionalProperties": {"type": "string"},
        },
    }


def test_frames_split_across_chunks_are_reassembled() -> None:
    reader = wire.FrameReader()
    data = chunk(text="one") + chunk(text="two")
    frames = [frame for byte in data for frame in reader.feed(bytes([byte]))]
    assert [wire.parse_chat_chunk(f.payload).delta_text for f in frames] == ["one", "two"]
    assert reader.leftover == 0


def test_an_oversized_frame_is_refused_before_it_is_buffered() -> None:
    reader = wire.FrameReader()
    with pytest.raises(WireError, match="cap"):
        list(reader.feed(struct.pack(">BI", 0, wire.MAX_FRAME_BYTES + 1)))


def test_a_gzipped_unary_body_still_decodes() -> None:
    import gzip

    body = unary(lambda m: m.put_str(1, "token"))
    assert wire.parse_user_jwt(gzip.compress(body)).jwt == "token"
    with pytest.raises(WireError):
        wire.decode_unary(b"\xff\xff\xff")


ENTERPRISE_HOST = "https://seven-eleven.tenant.example"


class EnterpriseServer(Server):
    """The shared host signs the user in and points at the tenant's own server."""

    def __init__(self) -> None:
        super().__init__()
        self.hosts: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.hosts.append((f"{request.url.scheme}://{request.url.host}", request.url.path))
        if request.url.path == wire.AUTH_PATH:

            def answer(message: Writer) -> None:
                message.put_str(1, self.user_jwt)
                message.put_str(2, ENTERPRISE_HOST + "/")

            return httpx.Response(200, content=unary(answer))
        if request.url.path == wire.MODEL_CONFIGS_PATH:
            config = Writer()
            config.put_str(1, "SWE-1.6")
            config.put_str(22, "swe-1-6")
            return httpx.Response(200, content=unary(lambda m: m.put_message(1, config)))
        return super().handler(request)


async def test_an_enterprise_tenants_turns_go_to_its_own_api_server() -> None:
    server = EnterpriseServer()
    await collect(provider(server), request())
    assert server.hosts == [
        (wire.DEFAULT_BASE_URL, wire.AUTH_PATH),
        (ENTERPRISE_HOST, wire.CHAT_PATH),
    ]


async def test_an_enterprise_tenants_models_come_from_its_own_api_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hx.providers import devin_catalogue

    server = EnterpriseServer()

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(server.handler))

    monkeypatch.setattr(devin_catalogue, "async_client", client)
    models = await devin_catalogue.fetch_models(ResolvedAuth(token="t", source="test"))

    assert [m.id for m in models] == ["devin/swe-1-6"]
    assert server.hosts[-1] == (ENTERPRISE_HOST, wire.MODEL_CONFIGS_PATH)
