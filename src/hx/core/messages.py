"""Canonical transcript model.

Every layer - provider, tools, session persistence, TUI - speaks these types.
Provider-specific wire formats are converted at the provider boundary only, so
the transcript on disk stays stable across model switches.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
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
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False
    spilled_path: str | None = None
    """Set when the full output was capped and written to disk."""
    type: Literal["tool_result"] = "tool_result"


ContentBlock = TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock


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


_BLOCK_TYPES: dict[str, type[ContentBlock]] = {
    "text": TextBlock,
    "thinking": ThinkingBlock,
    "tool_use": ToolUseBlock,
    "tool_result": ToolResultBlock,
}


def block_to_dict(block: ContentBlock) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ThinkingBlock):
        return {"type": "thinking", "text": block.text, "signature": block.signature}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return {
        "type": "tool_result",
        "tool_use_id": block.tool_use_id,
        "content": block.content,
        "is_error": block.is_error,
        "spilled_path": block.spilled_path,
    }


def block_from_dict(data: dict[str, Any]) -> ContentBlock:
    kind = data.get("type")
    cls = _BLOCK_TYPES.get(str(kind))
    if cls is None:
        raise ValueError(f"unknown content block type: {kind!r}")
    payload = {k: v for k, v in data.items() if k != "type"}
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


def user_message(text: str) -> Message:
    return Message(role="user", content=[TextBlock(text=text)])


def assistant_message(blocks: list[ContentBlock], model: str | None = None) -> Message:
    return Message(role="assistant", content=list(blocks), model=model)


def tool_result_message(results: list[ToolResultBlock]) -> Message:
    """Tool results are carried on a ``user``-role message, matching the provider wire format."""
    return Message(role="user", content=list(results))
