"""Canonical transcript model.

Every layer - provider, tools, session persistence, TUI - speaks these types.
Provider-specific wire formats are converted at the provider boundary only, so
the transcript on disk stays stable across model switches.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


class StopReason(StrEnum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass(slots=True)
class TextBlock:
    text: str
    type: Literal["text"] = "text"


@dataclass(slots=True)
class ThinkingBlock:
    """Reasoning content. Kept in the transcript but never re-sent as cacheable prefix
    unless the provider requires it."""

    text: str
    signature: str | None = None
    type: Literal["thinking"] = "thinking"


@dataclass(slots=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: Literal["tool_use"] = "tool_use"


@dataclass(slots=True)
class ImageBlock:
    """An image the model is shown: pasted by the user or returned by a tool.

    The bytes live in the block, base64-encoded, rather than behind a path. A
    transcript that points at files breaks the day one of them moves, and a
    resumed, forked or traced session has to be able to send every image it
    holds. They are normalised on the way in (see :mod:`hx.core.images`), so
    each is small enough for every route to accept as it stands.
    """

    media_type: str
    """One of :data:`hx.core.images.MEDIA_TYPES`."""
    data: str
    """Base64 of the encoded image, without a ``data:`` prefix."""
    width: int = 0
    height: int = 0
    label: str = ""
    """What the user and the model call it: ``Image #2``, ``screenshot.png``."""
    source: str = ""
    """The file it came from, when that is not already its label - a dragged-in
    ``mockup.png`` that the prompt calls ``Image #2``."""
    type: Literal["image"] = "image"

    def data_url(self) -> str:
        return f"data:{self.media_type};base64,{self.data}"

    def caption(self) -> str:
        """``Image #2: mockup.png`` - the name, and where it came from."""
        if self.source and self.source != self.label:
            return f"{self.label}: {self.source}" if self.label else self.source
        return self.label


@dataclass(slots=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False
    spilled_path: str | None = None
    """Set when the full output was capped and written to disk."""
    images: list[ImageBlock] = field(default_factory=list)
    """Images the tool returned alongside its text - a Read of a PNG, an MCP
    screenshot. Every route can carry them next to the result they belong to."""
    type: Literal["tool_result"] = "tool_result"


ContentBlock = TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock | ImageBlock


@dataclass(slots=True)
class Message:
    """One transcript entry.

    Attributes:
        ephemeral: Late-injected content that is regenerated every turn and must
            never be treated as part of a stable cache prefix. See
            ``hx.core.lateinject``.
        compacted: Marks messages superseded by a compaction summary. They stay
            in the session file for resume/undo but are excluded from context.
    """

    role: Role
    content: list[ContentBlock]
    timestamp: float = field(default_factory=time.time)
    model: str | None = None
    ephemeral: bool = False
    compacted: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def text(self) -> str:
        """Concatenated text blocks. Ignores thinking and tool blocks."""
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    def tool_uses(self) -> list[ToolUseBlock]:
        return [b for b in self.content if isinstance(b, ToolUseBlock)]

    def tool_results(self) -> list[ToolResultBlock]:
        return [b for b in self.content if isinstance(b, ToolResultBlock)]

    def images(self) -> list[ImageBlock]:
        """Images attached to this message itself, not those inside tool results."""
        return [b for b in self.content if isinstance(b, ImageBlock)]


_BLOCK_TYPES: dict[str, type[ContentBlock]] = {
    "text": TextBlock,
    "thinking": ThinkingBlock,
    "tool_use": ToolUseBlock,
    "tool_result": ToolResultBlock,
    "image": ImageBlock,
}


def block_to_dict(block: ContentBlock) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ThinkingBlock):
        return {"type": "thinking", "text": block.text, "signature": block.signature}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ImageBlock):
        return {
            "type": "image",
            "media_type": block.media_type,
            "data": block.data,
            "width": block.width,
            "height": block.height,
            "label": block.label,
            "source": block.source,
        }
    record: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": block.tool_use_id,
        "content": block.content,
        "is_error": block.is_error,
        "spilled_path": block.spilled_path,
    }
    # Only when there are some, so a text-only result is written exactly as it
    # was before images existed.
    if block.images:
        record["images"] = [block_to_dict(image) for image in block.images]
    return record


def block_from_dict(data: dict[str, Any]) -> ContentBlock:
    kind = data.get("type")
    cls = _BLOCK_TYPES.get(str(kind))
    if cls is None:
        raise ValueError(f"unknown content block type: {kind!r}")
    payload = {k: v for k, v in data.items() if k != "type"}
    if cls is ToolResultBlock:
        payload["images"] = [
            image
            for raw in payload.get("images") or []
            if isinstance(image := block_from_dict(raw), ImageBlock)
        ]
    return cls(**payload)


def to_dict(message: Message) -> dict[str, Any]:
    """Serialise for the session JSONL."""
    return {
        "role": message.role,
        "content": [block_to_dict(b) for b in message.content],
        "timestamp": message.timestamp,
        "model": message.model,
        "ephemeral": message.ephemeral,
        "compacted": message.compacted,
        "metadata": message.metadata,
    }


def from_dict(data: dict[str, Any]) -> Message:
    """Inverse of :func:`to_dict`. Must round-trip exactly."""
    return Message(
        role=data["role"],
        content=[block_from_dict(b) for b in data.get("content", [])],
        timestamp=data.get("timestamp", 0.0),
        model=data.get("model"),
        ephemeral=data.get("ephemeral", False),
        compacted=data.get("compacted", False),
        metadata=data.get("metadata", {}),
    )


@dataclass(frozen=True, slots=True)
class UserTurn:
    """Something the user sent that is not in the transcript yet: queued behind
    a running turn, or steered into one."""

    text: str
    images: tuple[ImageBlock, ...] = ()


def user_message(text: str, images: Sequence[ImageBlock] = ()) -> Message:
    """A user turn. Images follow the text, in the order they were attached."""
    content: list[ContentBlock] = [TextBlock(text=text)] if text or not images else []
    content.extend(images)
    return Message(role="user", content=content)


def assistant_message(blocks: list[ContentBlock], model: str | None = None) -> Message:
    return Message(role="assistant", content=list(blocks), model=model)


def tool_result_message(results: list[ToolResultBlock]) -> Message:
    """Tool results are carried on a ``user``-role message, matching the provider wire format."""
    return Message(role="user", content=list(results))


INTERRUPTED = "Interrupted by the user before this call finished."
_OUTPUT_SO_FAR = "\n\nOutput before the interrupt:\n"


def interrupted_result(tool_use_id: str, output: str = "") -> ToolResultBlock:
    """The answer for a call the user cut off, before it started or while it ran.

    What it printed before then goes with it: how far a command got is what
    the model needs to decide whether to run it again.
    """
    content = f"{INTERRUPTED}{_OUTPUT_SO_FAR}{output}" if output.strip() else INTERRUPTED
    return ToolResultBlock(tool_use_id=tool_use_id, content=content, is_error=True)


def interrupted_output(result: ToolResultBlock) -> str | None:
    """What an interrupted call printed, or ``None`` when ``result`` is not one."""
    if not (result.is_error and result.content.startswith(INTERRUPTED)):
        return None
    return result.content.removeprefix(INTERRUPTED).removeprefix(_OUTPUT_SO_FAR)


def answer_unanswered_calls(messages: Sequence[Message]) -> list[Message]:
    """``messages`` with every tool call answered by the message after it.

    The loop answers every call it starts, interrupted or not, so a request is
    never built while a call waits. A transcript can still hold a call with no
    result: written before the loop did that, or by a process killed mid-tool.
    Every route rejects such a call - on every request after it, since it never
    leaves the history - so the missing results are filled in as interrupted.
    Only the request is repaired; the transcript on disk keeps what happened.
    """
    repaired: list[Message] = []
    waiting: list[ToolUseBlock] = []
    for message in [*messages, None]:
        if waiting:
            results = {r.tool_use_id: r for r in message.tool_results()} if message else {}
            if any(call.id not in results for call in waiting):
                answers = [results.get(c.id) or interrupted_result(c.id) for c in waiting]
                ids = {call.id for call in waiting}
                if message is not None and results:
                    # Into the results already there: the calls stay answered
                    # by one message, in the order they were made.
                    rest = [
                        b
                        for b in message.content
                        if not (isinstance(b, ToolResultBlock) and b.tool_use_id in ids)
                    ]
                    message = replace(message, content=[*answers, *rest])
                else:
                    repaired.append(tool_result_message(answers))
        if message is None:
            break
        repaired.append(message)
        waiting = message.tool_uses() if message.role == "assistant" else []
    return repaired
