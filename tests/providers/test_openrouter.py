"""OpenRouter payload construction and usage parsing."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from hx.core.context import ContextBuilder
from hx.core.messages import (
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
