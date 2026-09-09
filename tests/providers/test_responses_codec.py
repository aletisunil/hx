"""The Responses wire format, exercised without a socket."""

from __future__ import annotations

import json
from typing import Any

from hx.core.context import AssembledContext, PromptSection
from hx.core.messages import (
    Message,
    StopReason,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from hx.providers.base import ProviderRequest, StreamDelta, StreamEnd
from hx.providers.responses_codec import (
    StreamState,
    build_body,
    decode_reasoning,
    encode_input,
    parse_usage,
    reasoning_signature,
)

TOOL = {
    "name": "Read",
    "description": "Read a file",
    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
}


def request(
    messages: list[Message] | None = None,
    tools: list[dict[str, Any]] | None = None,
    system: str = "You are HX.",
) -> ProviderRequest:
    context = AssembledContext(
        system=[PromptSection(name="system", text=system)],
        messages=messages or [Message(role="user", content=[TextBlock(text="hi")])],
        tools=tools if tools is not None else [TOOL],
    )
    return ProviderRequest(context=context, model="openai-codex/gpt-5.3-codex", max_tokens=8192)


def test_the_system_prompt_becomes_instructions_not_a_message() -> None:
    """Responses has no system role; sending one is a 400."""
    body = build_body(request())
    assert body["instructions"] == "You are HX."
    assert all(item.get("role") != "system" for item in body["input"])


def test_conversations_are_never_stored_server_side() -> None:
    """HX keeps its own transcript; leaving code in a retention bucket is not ours to opt into."""
    assert build_body(request())["store"] is False


def test_tools_are_flat_not_wrapped_in_a_function_envelope() -> None:
    """Chat-completions nests these under ``function``; Responses rejects that."""
    tool = build_body(request())["tools"][0]
    assert tool == {
        "type": "function",
        "name": "Read",
        "description": "Read a file",
        "parameters": TOOL["input_schema"],
    }


def test_an_already_wrapped_tool_is_unwrapped() -> None:
    wrapped = {"type": "function", "function": {"name": "Grep", "parameters": {"type": "object"}}}
    tool = build_body(request(tools=[wrapped]))["tools"][0]
    assert tool["name"] == "Grep"
    assert "function" not in tool


def test_the_session_id_is_the_prompt_cache_key() -> None:
    body = build_body(request(), session_id="sess-7")
    assert body["prompt_cache_key"] == "sess-7"
    assert body["include"] == ["reasoning.encrypted_content"]


def test_no_session_id_means_no_cache_key_rather_than_an_empty_one() -> None:
    assert "prompt_cache_key" not in build_body(request())


def test_reasoning_effort_is_only_sent_when_asked_for() -> None:
    assert "reasoning" not in build_body(request())
    body = build_body(request(), reasoning_effort="high")
    assert body["reasoning"] == {"effort": "high", "summary": "auto"}


def test_tool_calls_and_results_are_separate_input_items() -> None:
    """Responses flattens what chat-completions nests on the assistant message."""
    messages = [
        Message(role="user", content=[TextBlock(text="read it")]),
        Message(
            role="assistant",
            content=[
                TextBlock(text="on it"),
                ToolUseBlock(id="call_1", name="Read", input={"path": "a.py"}),
            ],
        ),
        Message(role="tool", content=[ToolResultBlock(tool_use_id="call_1", content="body")]),
    ]
    items = encode_input(messages)

    assert [item["type"] for item in items] == [
        "message",
        "message",
        "function_call",
        "function_call_output",
    ]
    assert items[2]["call_id"] == "call_1"
    assert json.loads(items[2]["arguments"]) == {"path": "a.py"}
    assert items[3] == {"type": "function_call_output", "call_id": "call_1", "output": "body"}


def test_user_and_assistant_text_use_the_right_content_type() -> None:
    items = encode_input(
        [
            Message(role="user", content=[TextBlock(text="q")]),
            Message(role="assistant", content=[TextBlock(text="a")]),
        ]
    )
    assert items[0]["content"][0]["type"] == "input_text"
    assert items[1]["content"][0]["type"] == "output_text"


def test_reasoning_is_replayed_ahead_of_the_text_it_produced() -> None:
    """Order matters: the API rejects a reasoning item that trails its message."""
    signature = reasoning_signature(
        [{"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque", "summary": ["drop me"]}]
    )
    items = encode_input(
        [
            Message(
                role="assistant",
                content=[ThinkingBlock(text="thought", signature=signature), TextBlock(text="a")],
            )
        ]
    )
    assert [item["type"] for item in items] == ["reasoning", "message"]
    assert items[0] == {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}


def test_reasoning_without_a_payload_is_dropped_not_sent_as_text() -> None:
    """Transcripts from OpenRouter carry thinking text with no opaque payload."""
    items = encode_input(
        [Message(role="assistant", content=[ThinkingBlock(text="plain"), TextBlock(text="a")])]
    )
    assert [item["type"] for item in items] == ["message"]


def test_a_corrupt_signature_is_ignored_rather_than_crashing_the_turn() -> None:
    assert decode_reasoning("{not json") == []
    assert decode_reasoning(json.dumps({"type": "reasoning"})) == []
    assert decode_reasoning(json.dumps([{"type": "message"}])) == []
    assert decode_reasoning(None) == []


def test_a_tool_call_is_emitted_once_with_id_name_and_arguments() -> None:
    """The loop only builds a tool call from a delta carrying all three."""
    state = StreamState()
    events = [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "function_call", "call_id": "call_1", "name": "Read"},
        },
        {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"pa'},
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "delta": 'th":"a.py"}',
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {"type": "function_call", "call_id": "call_1", "name": "Read"},
        },
    ]
    emitted = [item for event in events for item in state.consume(event)]

    assert len(emitted) == 1, "a half-built tool call must never reach the loop"
    delta = emitted[0]
    assert isinstance(delta, StreamDelta)
    assert (delta.tool_use_id, delta.tool_name) == ("call_1", "Read")
    assert json.loads(delta.tool_input_json or "") == {"path": "a.py"}
    assert state.stop_reason is StopReason.TOOL_USE


def test_text_and_reasoning_stream_separately() -> None:
    state = StreamState()
    text = list(state.consume({"type": "response.output_text.delta", "delta": "hel"}))
    thinking = list(
        state.consume({"type": "response.reasoning_summary_text.delta", "delta": "hmm"})
    )
    assert isinstance(text[0], StreamDelta) and text[0].text == "hel"
    assert isinstance(thinking[0], StreamDelta) and thinking[0].thinking == "hmm"


def test_the_turn_ends_with_one_combined_reasoning_payload() -> None:
    """One signature per turn: the loop keeps the last it sees, so several would lose the rest."""
    state = StreamState()
    for index in (0, 1):
        list(
            state.consume(
                {
                    "type": "response.output_item.done",
                    "output_index": index,
                    "item": {
                        "type": "reasoning",
                        "id": f"rs_{index}",
                        "encrypted_content": f"e{index}",
                    },
                }
            )
        )
    emitted = list(state.consume({"type": "response.completed", "response": {"usage": {}}}))

    assert len(emitted) == 1
    delta = emitted[0]
    assert isinstance(delta, StreamDelta)
    assert [item["id"] for item in json.loads(delta.thinking_signature or "")] == ["rs_0", "rs_1"]


def test_usage_separates_cached_tokens_from_billed_ones() -> None:
    usage = parse_usage(
        {
            "input_tokens": 10_000,
            "input_tokens_details": {"cached_tokens": 9_000},
            "output_tokens": 250,
            "output_tokens_details": {"reasoning_tokens": 100},
        }
    )
    assert usage.input_tokens == 1_000, "cached tokens are already inside input_tokens"
    assert usage.cache_read_tokens == 9_000
    assert usage.prompt_tokens == 10_000
    assert usage.reasoning_tokens == 100


def test_a_truncated_response_reports_max_tokens() -> None:
    state = StreamState()
    list(
        state.consume(
            {
                "type": "response.incomplete",
                "response": {"incomplete_details": {"reason": "max_output_tokens"}, "usage": {}},
            }
        )
    )
    assert state.stop_reason is StopReason.MAX_TOKENS


def test_a_failed_response_raises_rather_than_ending_the_turn_quietly() -> None:
    import pytest

    from hx.providers.base import ProviderError

    state = StreamState()
    with pytest.raises(ProviderError, match="rate limited"):
        list(state.consume({"type": "response.failed", "error": {"message": "rate limited"}}))


def test_stream_end_carries_the_latency() -> None:
    from hx.providers.responses_codec import stream_end

    state = StreamState()
    end = stream_end(state, 1234.0)
    assert isinstance(end, StreamEnd)
    assert end.usage.latency_ms == 1234.0
