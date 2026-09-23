"""OpenRouter payload construction and usage parsing."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from hx.core.context import ContextBuilder
from hx.core.messages import (
    ImageBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    assistant_message,
    tool_result_message,
    user_message,
)
from hx.providers.base import ProviderRequest
from hx.providers.openrouter import (
    MAX_CACHE_CONTROL_MARKERS,
    MissingAPIKey,
    OpenRouterProvider,
    api_key_source,
    load_api_key,
    mask_api_key,
    parse_usage,
    save_api_key,
)


def _request(cwd: Path, *, cache_mode: str = "explicit") -> ProviderRequest:
    builder = ContextBuilder("sys prompt", cwd, keep_recent_turns=2)
    messages = [
        user_message("hi"),
        assistant_message([TextBlock("ok"), ToolUseBlock("c1", "Read", {"file_path": "a.py"})]),
        tool_result_message([ToolResultBlock("c1", "contents")]),
    ]
    tools = [{"name": "Read", "description": "read a file", "input_schema": {"type": "object"}}]
    context = builder.build(messages, tools, cache_mode=cache_mode)
    return ProviderRequest(context=context, model="anthropic/claude-sonnet-4.5", max_tokens=1024)


def test_usage_accounting_is_requested(project: Path) -> None:
    """Without usage.include there are no cache or cost numbers to display."""
    payload = OpenRouterProvider(api_key="k").build_payload(_request(project))
    assert payload["usage"] == {"include": True}


def test_tool_results_become_tool_role_messages(project: Path) -> None:
    payload = OpenRouterProvider(api_key="k").build_payload(_request(project))
    assert [m["role"] for m in payload["messages"]] == ["system", "user", "assistant", "tool"]
    assert payload["messages"][2]["tool_calls"][0]["function"]["name"] == "Read"
    assert payload["messages"][3]["tool_call_id"] == "c1"


def test_tools_use_the_function_envelope(project: Path) -> None:
    payload = OpenRouterProvider(api_key="k").build_payload(_request(project))
    assert payload["tools"][0]["type"] == "function"
    assert payload["tools"][0]["function"]["name"] == "Read"


def test_cache_control_capped_at_four_markers() -> None:
    """Exceeding the limit is a hard API error, not a degradation."""
    provider = OpenRouterProvider(api_key="k")
    messages: list[dict] = [
        {"role": "user", "content": [{"type": "text", "text": "x"}]} for _ in range(10)
    ]
    provider._apply_cache_control(messages, tuple(range(10)))
    assert sum("cache_control" in str(m) for m in messages) == MAX_CACHE_CONTROL_MARKERS


def test_implicit_cache_mode_sends_no_markers(project: Path) -> None:
    payload = OpenRouterProvider(api_key="k").build_payload(
        _request(project, cache_mode="implicit")
    )
    assert "cache_control" not in json.dumps(payload)


def test_payload_is_byte_stable_for_identical_input(project: Path) -> None:
    """A payload that serialises differently between turns is a cache miss."""
    provider = OpenRouterProvider(api_key="k")
    request = _request(project)
    assert json.dumps(provider.build_payload(request)) == json.dumps(
        provider.build_payload(request)
    )


def test_cached_prompt_tokens_are_split_out() -> None:
    """OpenRouter's prompt_tokens already includes cached_tokens."""
    usage = parse_usage(
        {
            "prompt_tokens": 10_000,
            "completion_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 9_000},
            "cost": 0.02,
        }
    )
    assert usage.input_tokens == 1_000
    assert usage.cache_read_tokens == 9_000
    assert usage.cost_usd == 0.02
    assert usage.cache_hit_rate == pytest.approx(0.9)


def test_api_key_round_trips_with_restrictive_permissions(hx_home: Path) -> None:
    save_api_key("sk-test")
    assert load_api_key() == "sk-test"
    assert (hx_home / "auth.json").stat().st_mode & 0o777 == 0o600


def test_missing_api_key_is_actionable(hx_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HX_OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(MissingAPIKey, match=re.escape("openrouter.ai/keys")):
        load_api_key()


def test_key_masking_never_shows_the_middle() -> None:
    assert mask_api_key("sk-or-v1-0123456789abcdef") == "sk-or-…cdef"
    assert "0123456789" not in mask_api_key("sk-or-v1-0123456789abcdef")
    assert mask_api_key("short") == "…"


def test_api_key_source_prefers_the_environment(
    hx_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HX_OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert api_key_source() is None

    save_api_key("sk-or-from-file")
    assert api_key_source() == str(hx_home / "auth.json")

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-from-env")
    assert api_key_source() == "environment (OPENROUTER_API_KEY)"


def test_a_swapped_key_reaches_the_request_header(project: Path) -> None:
    """Saving a key to disk is useless if the live client keeps the old header."""
    provider = OpenRouterProvider(api_key="old-key")
    assert provider._client.headers["Authorization"] == "Bearer old-key"

    provider.set_api_key("new-key")
    assert provider.api_key == "new-key"
    assert provider._client.headers["Authorization"] == "Bearer new-key"


def test_text_alongside_tool_results_still_reaches_the_model(project: Path) -> None:
    """Anything but the tool entries used to be dropped on the floor here.

    A message carrying tool results can also carry text - a late-injected
    reminder rides the newest user message, and that is frequently this one.
    Encoding only the tool entries silently deleted it.
    """
    from hx.core.messages import Message

    builder = ContextBuilder("sys prompt", project, keep_recent_turns=2)
    messages = [
        user_message("hi"),
        assistant_message([TextBlock("ok"), ToolUseBlock("c1", "Read", {"file_path": "a.py"})]),
        Message(
            role="user",
            content=[ToolResultBlock("c1", "contents"), TextBlock("stop and read b.py instead")],
        ),
    ]
    context = builder.build(messages, [], cache_mode="explicit")
    request = ProviderRequest(context=context, model="anthropic/claude-sonnet-4.5", max_tokens=1024)

    payload = OpenRouterProvider(api_key="k").build_payload(request)
    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "user"]
    assert "stop and read b.py instead" in json.dumps(payload["messages"][-1])


# --- stream framing --------------------------------------------------------


def _tool_call_chunks() -> list[dict]:
    """A complete tool call, split across chunks the way the wire sends it."""
    return [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "Read", "arguments": '{"file_path"'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ':"/tmp/x"}'}}]}}
            ]
        },
    ]


def test_a_stream_that_never_sends_finish_reason_still_yields_its_tool_calls() -> None:
    """Tool arguments are buffered until the call is known to be complete, and
    that was keyed on ``finish_reason`` alone.

    A stream cut short, or a route that simply omits the field, left a fully
    assembled call in the buffer and reported ``end_turn``: the model appeared
    to stop for no reason, and the user had already paid for the tokens.
    """
    from hx.core.messages import StopReason
    from hx.providers.openrouter import _StreamState

    state = _StreamState()
    emitted = [item for chunk in _tool_call_chunks() for item in state.consume(chunk)]
    assert emitted == []

    flushed = state.flush()
    assert [(item.tool_name, item.tool_input_json) for item in flushed] == [
        ("Read", '{"file_path":"/tmp/x"}')
    ]
    assert state.stop_reason is StopReason.TOOL_USE


def test_flushing_twice_does_not_duplicate_a_tool_call() -> None:
    """The ordinary path flushes on ``finish_reason`` and again at the end of
    the stream; the model must not be handed the same call twice."""
    from hx.providers.openrouter import _StreamState

    state = _StreamState()
    for chunk in _tool_call_chunks():
        state.consume(chunk)
    on_finish = state.consume({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})

    assert len(on_finish) == 1
    assert state.flush() == []


# -- images -------------------------------------------------------------------

PICTURE = ImageBlock("image/png", "iVBORw0KGgo=", width=4, height=3, label="Image #1")


def _payload_for(cwd: Path, messages: list[Message], *, cache_mode: str) -> dict:
    context = ContextBuilder("sys", cwd).build(messages, [], cache_mode=cache_mode)
    request = ProviderRequest(context=context, model="openai/gpt-5", max_tokens=64)
    return OpenRouterProvider(api_key="k").build_payload(request)


@pytest.mark.parametrize("cache_mode", ["explicit", "implicit"])
def test_a_user_image_is_an_image_url_part_after_the_text(project: Path, cache_mode: str) -> None:
    payload = _payload_for(
        project, [user_message("what is [Image #1]?", [PICTURE])], cache_mode=cache_mode
    )
    content = payload["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "what is [Image #1]?"}
    assert content[1] == {"type": "text", "text": "[Image #1]"}
    assert content[2] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
    }
    if cache_mode == "explicit":
        # The breakpoint may land on an image part; Anthropic accepts that.
        assert "cache_control" not in content[0]


def test_an_image_alone_is_a_whole_message(project: Path) -> None:
    payload = _payload_for(project, [user_message("", [PICTURE])], cache_mode="implicit")
    content = payload["messages"][1]["content"]
    assert [part["type"] for part in content] == ["text", "image_url"]
    assert content[0]["text"] == "[Image #1]"


def test_a_tool_image_rides_a_user_turn_after_the_results(project: Path) -> None:
    """A ``tool`` message is text-only in chat-completions."""
    messages = [
        user_message("look"),
        assistant_message([ToolUseBlock("c1", "Read", {"file_path": "a.png"})]),
        tool_result_message([ToolResultBlock("c1", "Image a.png, attached.", images=[PICTURE])]),
    ]
    payload = _payload_for(project, messages, cache_mode="implicit")
    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "user"]
    assert payload["messages"][3]["content"] == "Image a.png, attached."
    trailing = payload["messages"][4]["content"]
    assert trailing[0] == {"type": "text", "text": "Images returned by tool call c1:"}
    assert trailing[-1]["type"] == "image_url"


def test_a_text_only_payload_is_unchanged_by_image_support(project: Path) -> None:
    """Byte-stable prefixes are what the cache keys on."""
    payload = OpenRouterProvider(api_key="k").build_payload(_request(project, cache_mode="none"))
    assert payload["messages"][1] == {"role": "user", "content": "hi"}
    assert payload["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": "contents"}


def test_an_image_is_named_with_the_file_it_came_from(project: Path) -> None:
    dragged = replace(PICTURE, source="mockup.png")
    payload = _payload_for(project, [user_message("", [dragged])], cache_mode="implicit")
    assert payload["messages"][1]["content"][0] == {
        "type": "text",
        "text": "[Image #1: mockup.png]",
    }
