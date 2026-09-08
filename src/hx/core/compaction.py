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

from hx.core.messages import Message

SUMMARY_SECTIONS = (
    "Goal",
    "Decisions made",
    "Files touched",
    "Current state",
    "Next steps",
    "Open questions",
)


@dataclass(slots=True)
class CompactionResult:
    summary: Message
    kept: list[Message]
    dropped: list[Message]
    tokens_before: int
    tokens_after: int


class Compactor:
    def __init__(self, keep_recent_turns: int = 6) -> None:
        raise NotImplementedError

    def should_compact(self, context_fraction: float, threshold: float) -> bool:
        raise NotImplementedError

    def split(self, messages: list[Message]) -> tuple[list[Message], list[Message]]:
        """Partition into (to summarise, to keep verbatim).

        The boundary snaps to a turn edge: never split an assistant tool-use
        message from its tool results, or the next request is malformed.
        """
        raise NotImplementedError

    async def compact(
        self,
        messages: list[Message],
        instructions: str | None = None,
    ) -> CompactionResult:
        """Summarise via a provider call and assemble the replacement transcript."""
        raise NotImplementedError

    def build_summary_prompt(
        self,
        messages: list[Message],
        instructions: str | None,
    ) -> str:
        """Prompt requesting the sections in :data:`SUMMARY_SECTIONS`."""
        raise NotImplementedError
