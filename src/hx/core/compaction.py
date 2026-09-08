"""Conversation compaction.

Fires when the assembled context crosses ``context.compact_at`` of the model's
window, or on an explicit ``/compact [instructions]``. Older turns are replaced
by one structured summary; the last N turns and the full todo list survive
verbatim. The pre-compaction messages stay in the session JSONL (flagged
``compacted``) so resume and undo still work.

Compaction reseeds the cache prefix by definition - that cost is accepted and
surfaced in the status bar rather than hidden.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from hx.core.messages import (
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

if TYPE_CHECKING:
    from hx.core.context import ContextBuilder
    from hx.providers.base import Provider

SUMMARY_SECTIONS = (
    "Goal",
    "Decisions made",
    "Files touched",
    "Current state",
    "Next steps",
    "Open questions",
)

SUMMARY_MARKER = "<hx-compacted-summary>"
SUMMARY_END = "</hx-compacted-summary>"

SUMMARY_PROMPT = """\
Summarise the conversation below so that work can continue without it.

Write these sections, in order, using the exact headings:

{sections}

Be specific and concrete. Name files by path, record decisions with their
reasons, and state exactly where the work stopped. Preserve anything the next
turn would otherwise have to re-derive: chosen approaches, rejected approaches
and why, error messages still unresolved, and commands that must be re-run.

Do not summarise the summary instructions. Do not add commentary.
{extra}
--- conversation ---

{transcript}
"""


@dataclass(slots=True)
class CompactionResult:
    summary: Message
    kept: list[Message]
    dropped: list[Message]
    tokens_before: int
    tokens_after: int


class Compactor:
    #: Cap on the summary itself. A summary that can grow without bound just
    #: moves the context problem one turn later.
    SUMMARY_MAX_TOKENS: ClassVar[int] = 2048
    MIN_MESSAGES_TO_COMPACT: ClassVar[int] = 4

    def __init__(
        self,
        provider: Provider | None = None,
        model: str = "",
        keep_recent_turns: int = 6,
        context: ContextBuilder | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.keep_recent_turns = keep_recent_turns
        self.context = context

    def should_compact(self, context_fraction: float, threshold: float) -> bool:
        return context_fraction >= threshold > 0

    def split(self, messages: list[Message]) -> tuple[list[Message], list[Message]]:
        """Partition into (to summarise, to keep verbatim).

        The boundary snaps to a turn edge: never split an assistant tool-use
        message from its tool results, or the next request is malformed.
        """
        if len(messages) <= self.keep_recent_turns:
            return [], list(messages)

        boundary = len(messages) - self.keep_recent_turns
        while boundary > 0 and not _is_turn_edge(messages, boundary):
            boundary -= 1

        return list(messages[:boundary]), list(messages[boundary:])

    async def compact(
        self,
        messages: list[Message],
        instructions: str | None = None,
    ) -> CompactionResult:
        """Summarise via a provider call and assemble the replacement transcript."""
        dropped, kept = self.split(messages)
        if len(dropped) < self.MIN_MESSAGES_TO_COMPACT:
            return CompactionResult(
                summary=_summary_message("(nothing to compact)"),
                kept=list(messages),
                dropped=[],
                tokens_before=self._estimate(messages),
                tokens_after=self._estimate(messages),
            )

        text = await self._summarise(dropped, instructions)
        summary = _summary_message(text)
        return CompactionResult(
            summary=summary,
            kept=kept,
            dropped=dropped,
            tokens_before=self._estimate(messages),
            tokens_after=self._estimate([summary, *kept]),
        )

    async def _summarise(self, messages: list[Message], instructions: str | None) -> str:
        if self.provider is None:
            # Without a provider there is nothing to call; fall back to a
            # mechanical digest rather than silently dropping the history.
            return _mechanical_summary(messages)

        from hx.core.context import AssembledContext, PromptSection
        from hx.providers.base import ProviderRequest, StreamDelta

        prompt = self.build_summary_prompt(messages, instructions)
        context = AssembledContext(
            system=[PromptSection("system", "You write precise handover summaries.")],
            messages=[Message(role="user", content=[TextBlock(text=prompt)])],
            tools=[],
        )
        request = ProviderRequest(
            context=context,
            model=self.model,
            max_tokens=self.SUMMARY_MAX_TOKENS,
        )

        parts: list[str] = []
        async for item in self.provider.astream(request):
            if isinstance(item, StreamDelta) and item.text:
                parts.append(item.text)

        return "".join(parts).strip() or _mechanical_summary(messages)

    def build_summary_prompt(
        self,
        messages: list[Message],
        instructions: str | None,
    ) -> str:
        """Prompt requesting the sections in :data:`SUMMARY_SECTIONS`."""
        extra = f"\nAlso: {instructions.strip()}\n" if instructions else ""
        return SUMMARY_PROMPT.format(
            sections="\n".join(f"## {section}" for section in SUMMARY_SECTIONS),
            extra=extra,
            transcript=render_transcript(messages),
        )

    def _estimate(self, messages: list[Message]) -> int:
        if self.context is not None:
            return sum(self.context.estimate_tokens(_message_text(m)) for m in messages)
        return sum(len(_message_text(m)) // 4 for m in messages)


def _is_turn_edge(messages: list[Message], index: int) -> bool:
    """True when a split before ``index`` leaves every tool call with its results."""
    if index <= 0 or index >= len(messages):
        return True
    previous = messages[index - 1]
    current = messages[index]
    if previous.tool_uses():
        return False
    return not any(isinstance(block, ToolResultBlock) for block in current.content)


def render_transcript(messages: list[Message]) -> str:
    """Flatten messages to text for the summary prompt.

    Rendering to text rather than replaying the structured messages keeps the
    summary call free of tool-call pairing constraints, which a partial history
    would otherwise violate.
    """
    lines: list[str] = []
    for message in messages:
        for block in message.content:
            if isinstance(block, TextBlock):
                lines.append(f"[{message.role}] {block.text}")
            elif isinstance(block, ToolUseBlock):
                lines.append(f"[{message.role}] calls {block.name}({block.input})")
            elif isinstance(block, ToolResultBlock):
                marker = "error" if block.is_error else "result"
                lines.append(f"[tool {marker}] {_clip(block.content)}")
            elif isinstance(block, ThinkingBlock):
                continue
    return "\n".join(lines)


def _mechanical_summary(messages: list[Message]) -> str:
    """Last-resort digest when no model is available to write one."""
    users = [m.text() for m in messages if m.role == "user" and m.text()]
    tools = sorted({b.name for m in messages for b in m.tool_uses()})
    lines = ["## Goal", users[0][:500] if users else "(unknown)", "", "## Current state"]
    lines.append(f"{len(messages)} earlier messages were compacted without a model summary.")
    if tools:
        lines.append(f"Tools used: {', '.join(tools)}.")
    if len(users) > 1:
        lines += ["", "## Open questions", *[f"- {text[:200]}" for text in users[1:6]]]
    return "\n".join(lines)


def _summary_message(text: str) -> Message:
    body = f"{SUMMARY_MARKER}\nThis replaces the earlier conversation.\n\n{text}\n{SUMMARY_END}"
    return Message(
        role="user",
        content=[TextBlock(text=body)],
        metadata={"compaction_summary": True},
    )


def _message_text(message: Message) -> str:
    parts: list[str] = []
    for block in message.content:
        text = getattr(block, "text", None) or getattr(block, "content", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _clip(text: str, limit: int = 2000) -> str:
    return text if len(text) <= limit else text[:limit] + " … [clipped]"
