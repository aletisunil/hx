"""OpenAI Responses wire format.

Kept apart from the transport so every encoding decision is testable without a
socket, the way ``build_payload``/``parse_usage`` already are for OpenRouter.

The Responses API differs from chat-completions in three ways that matter here:

1. The system prompt is a top-level ``instructions`` string, not a message.
2. ``input`` is a flat list of *items*, not roles with content arrays. A tool
   call and its result are separate items, not fields on an assistant message.
3. Reasoning is opaque. The model returns ``reasoning`` items carrying
   ``encrypted_content``; those items must be echoed back verbatim on the next
   turn or the server drops the prompt cache and re-reasons from scratch. We
   stash them in :attr:`~hx.core.messages.ThinkingBlock.signature`, which
   exists for exactly this.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from hx.core.messages import (
    Message,
    StopReason,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from hx.core.usage import TurnUsage
from hx.providers.base import ProviderError, ProviderRequest, StreamDelta, StreamEnd, StreamItem

DEFAULT_INSTRUCTIONS = "You are a helpful assistant."

#: Asking for encrypted reasoning is what makes multi-turn cache hits possible
#: with ``store: false``.
INCLUDE = ["reasoning.encrypted_content"]

_INCOMPLETE_REASONS = {
    "max_output_tokens": StopReason.MAX_TOKENS,
    "max_tokens": StopReason.MAX_TOKENS,
    "content_filter": StopReason.STOP_SEQUENCE,
}


def build_body(
    request: ProviderRequest,
    *,
    session_id: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Serialise the assembled context into a Responses request body.

    ``store`` is false: HX keeps its own transcript, and leaving conversations
    server-side would put the user's code in a retention bucket they did not
    ask for. ``prompt_cache_key`` carries the session id instead, which is what
    the implicit cache keys off.
    """
    context = request.context

    body: dict[str, Any] = {
        "model": request.model,
        "store": False,
        "stream": True,
        "instructions": context.system_text() or DEFAULT_INSTRUCTIONS,
        "input": encode_input(context.messages),
        "include": list(INCLUDE),
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }
    if context.tools:
        body["tools"] = [encode_tool(tool) for tool in context.tools]
    if session_id:
        body["prompt_cache_key"] = session_id
    if request.temperature is not None:
        body["temperature"] = request.temperature
    if reasoning_effort:
        body["reasoning"] = {"effort": reasoning_effort, "summary": "auto"}
    body.update(request.extra)
    return body


def encode_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Responses puts the tool fields at the top level.

    Chat-completions nests them under ``function``; sending that shape here is
    a 400, so a tool that already arrived wrapped is unwrapped.
    """
    if "function" in tool and isinstance(tool["function"], dict):
        tool = {**tool["function"], "type": "function"}
    return {
        "type": "function",
        "name": tool.get("name", ""),
        "description": tool.get("description", ""),
        "parameters": tool.get("input_schema") or tool.get("parameters") or {},
    }


def encode_input(messages: Iterable[Message]) -> list[dict[str, Any]]:
    """Flatten the transcript into Responses input items."""
    items: list[dict[str, Any]] = []
    for message in messages:
        items.extend(_encode_message(message))
    return items


def _encode_message(message: Message) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    text_parts: list[str] = []

    for block in message.content:
        if isinstance(block, TextBlock):
            text_parts.append(block.text)
        elif isinstance(block, ThinkingBlock):
            items.extend(decode_reasoning(block.signature))
        elif isinstance(block, ToolUseBlock):
            items.append(
                {
                    "type": "function_call",
                    "call_id": block.id,
                    "name": block.name,
                    "arguments": json.dumps(block.input, sort_keys=True),
                }
            )
        elif isinstance(block, ToolResultBlock):
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": block.tool_use_id,
                    "output": block.content,
                }
            )

    text = "".join(text_parts)
    if text:
        role = "assistant" if message.role == "assistant" else "user"
        content_type = "output_text" if role == "assistant" else "input_text"
        # Reasoning must precede the text it produced, so insert ahead of any
        # tool items but after the reasoning ones.
        insert_at = sum(1 for item in items if item.get("type") == "reasoning")
        items.insert(
            insert_at,
            {"type": "message", "role": role, "content": [{"type": content_type, "text": text}]},
        )
    return items


def decode_reasoning(signature: str | None) -> list[dict[str, Any]]:
    """Rebuild the opaque reasoning items stashed on a thinking block.

    Blocks without a stored payload - anything that came from another provider,
    or from a session recorded before this existed - yield nothing rather than
    being sent as plain text, which the API rejects.
    """
    if not signature:
        return []
    try:
        payload = json.loads(signature)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [
        # ``summary`` is required on the way back in, so a session recorded
        # before it was stored still replays rather than 400ing the turn.
        {**item, "summary": item.get("summary") or []}
        for item in payload
        if isinstance(item, dict) and item.get("type") == "reasoning"
    ]


def reasoning_signature(items: list[dict[str, Any]]) -> str:
    """Serialise a turn's reasoning items for storage on a :class:`ThinkingBlock`.

    Only the fields the API needs back are kept. ``summary`` is one of them:
    the server rejects a reasoning item that arrives without it, even though
    the same text is already in the block's ``text``.
    """
    keep = ("type", "id", "encrypted_content", "summary")
    return json.dumps(
        [{k: item[k] for k in keep if k in item} for item in items],
        sort_keys=True,
    )


class StreamState:
    """Accumulates one streamed response.

    Tool-call arguments arrive as fragments and are flushed only when the item
    is done, so the consumer never sees half a JSON object.
    """

    def __init__(self) -> None:
        self.stop_reason = StopReason.END_TURN
        self.usage = TurnUsage()
        self._calls: dict[str, dict[str, str]] = {}
        self._reasoning: list[dict[str, Any]] = []

    def consume(self, event: dict[str, Any]) -> Iterable[StreamItem]:
        kind = event.get("type")
        if not isinstance(kind, str):
            return

        if kind == "response.output_text.delta":
            if delta := event.get("delta"):
                yield StreamDelta(text=str(delta))

        elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            if delta := event.get("delta"):
                yield StreamDelta(thinking=str(delta))

        elif kind == "response.output_item.added":
            self._item_added(event)

        elif kind == "response.function_call_arguments.delta":
            self._accumulate_arguments(event)

        elif kind == "response.output_item.done":
            yield from self._item_done(event)

        elif kind in ("response.completed", "response.done"):
            yield from self._finish(event)

        elif kind == "response.incomplete":
            response = event.get("response") or {}
            reason = (response.get("incomplete_details") or {}).get("reason")
            yield from self._finish(event)
            self.stop_reason = _INCOMPLETE_REASONS.get(str(reason), StopReason.MAX_TOKENS)

        elif kind in ("response.failed", "error"):
            raise ProviderError(_error_message(event))

    def _item_added(self, event: dict[str, Any]) -> None:
        """Open a slot for a tool call. Nothing is emitted yet - the loop only
        builds a tool call from a delta carrying id, name and arguments at
        once, and the arguments are still streaming."""
        item = event.get("item") or {}
        if item.get("type") != "function_call":
            return
        call_id = str(item.get("call_id") or item.get("id") or "")
        if not call_id:
            return
        self._calls[self._key(event, item)] = {
            "id": call_id,
            "name": str(item.get("name") or ""),
            "arguments": str(item.get("arguments") or ""),
        }

    def _accumulate_arguments(self, event: dict[str, Any]) -> None:
        """Buffer an argument fragment; nothing is emitted until the item is done."""
        index = event.get("output_index")
        key = str(index) if index is not None else str(event.get("item_id") or "")
        call = self._calls.get(key)
        if call is not None:
            call["arguments"] += str(event.get("delta") or "")

    def _item_done(self, event: dict[str, Any]) -> Iterable[StreamItem]:
        item = event.get("item") or {}
        kind = item.get("type")

        if kind == "reasoning":
            self._reasoning.append(item)
            return

        if kind != "function_call":
            return

        call = self._calls.pop(self._key(event, item), None) or {}
        call_id = str(item.get("call_id") or call.get("id") or "")
        if not call_id:
            return
        self.stop_reason = StopReason.TOOL_USE
        yield StreamDelta(
            tool_use_id=call_id,
            tool_name=str(item.get("name") or call.get("name") or ""),
            tool_input_json=str(item.get("arguments") or call.get("arguments") or "") or "{}",
        )

    @staticmethod
    def _key(event: dict[str, Any], item: dict[str, Any]) -> str:
        """Slot key for a streaming item.

        ``output_index`` is present on every event of a well-formed stream;
        the item id is the fallback for servers that omit it.
        """
        index = event.get("output_index")
        return str(index) if index is not None else str(item.get("id") or "")

    def _finish(self, event: dict[str, Any]) -> Iterable[StreamItem]:
        response = event.get("response") or {}
        self.usage = parse_usage(response.get("usage") or {})

        output = response.get("output") or []
        if any(item.get("type") == "function_call" for item in output):
            self.stop_reason = StopReason.TOOL_USE

        # A terminal event may carry the whole output, including reasoning
        # items we never saw stream individually.
        for item in output:
            if item.get("type") == "reasoning" and item not in self._reasoning:
                self._reasoning.append(item)

        if self._reasoning:
            # One combined payload: the loop keeps the last signature it sees,
            # and the next turn has to replay every reasoning item, not one.
            yield StreamDelta(thinking_signature=reasoning_signature(self._reasoning))


def parse_usage(raw: dict[str, Any]) -> TurnUsage:
    """Map a Responses usage object.

    ``input_tokens`` includes the cached portion, so the cached tokens are
    subtracted out to keep the cost maths honest - the same correction the
    OpenRouter provider makes.
    """
    input_tokens = _int(raw.get("input_tokens"))
    cached = _int((raw.get("input_tokens_details") or {}).get("cached_tokens"))
    return TurnUsage(
        input_tokens=max(input_tokens - cached, 0),
        output_tokens=_int(raw.get("output_tokens")),
        cache_read_tokens=cached,
        reasoning_tokens=_int((raw.get("output_tokens_details") or {}).get("reasoning_tokens")),
    )


def _error_message(event: dict[str, Any]) -> str:
    error = event.get("error") or (event.get("response") or {}).get("error") or {}
    message = error.get("message") or error.get("code") if isinstance(error, dict) else str(error)
    return f"Codex: {message or 'the response failed'}"


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def stream_end(state: StreamState, latency_ms: float) -> StreamEnd:
    usage = state.usage
    usage.latency_ms = latency_ms
    return StreamEnd(stop_reason=state.stop_reason, usage=usage)
