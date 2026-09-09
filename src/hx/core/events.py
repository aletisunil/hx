"""Async event bus.

The agent core is a producer; the TUI and ``hx -p`` print mode are consumers.
Keeping this one-way means the core never imports Textual and can be driven
head-lessly in tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, ClassVar


@dataclass(slots=True)
class Event:
    """Base class for everything on the bus."""


@dataclass(slots=True)
class TurnStarted(Event):
    turn_index: int
    model: str


@dataclass(slots=True)
class TextDelta(Event):
    """Streaming assistant text."""

    text: str


@dataclass(slots=True)
class ThinkingDelta(Event):
    text: str


@dataclass(slots=True)
class ToolCallStarted(Event):
    tool_use_id: str
    name: str
    input: dict[str, Any]


@dataclass(slots=True)
class ToolCallProgress(Event):
    """Incremental output (e.g. Bash stdout) shown live in the TUI, uncapped."""

    tool_use_id: str
    chunk: str


@dataclass(slots=True)
class ToolCallFinished(Event):
    tool_use_id: str
    is_error: bool
    duration_ms: float
    summary: str
    detail: str = ""
    """Why it failed, in the tool's own words.

    ``summary`` is a label - often the constant ``"error"`` - so without this
    the reason a call failed reached the model and nothing else. Empty on
    success, where the output the tool streamed is the whole story."""
    metadata: dict[str, Any] = field(default_factory=dict)
    """Structured detail the renderer draws with - an edit's diff, a command's
    exit code. Presentation only: the model never sees it."""


@dataclass(slots=True)
class PermissionRequested(Event):
    """Core is blocked until a consumer answers via the paired future."""

    request_id: str
    tool_name: str
    description: str
    detail: str


@dataclass(slots=True)
class UsageUpdated(Event):
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    context_tokens: int
    context_window: int
    cost_usd: float


@dataclass(slots=True)
class TodosUpdated(Event):
    todos: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class SubagentStarted(Event):
    subagent_id: str
    agent_type: str
    description: str


@dataclass(slots=True)
class SubagentFinished(Event):
    subagent_id: str
    is_error: bool


@dataclass(slots=True)
class CompactionStarted(Event):
    reason: str


@dataclass(slots=True)
class CompactionFinished(Event):
    tokens_before: int
    tokens_after: int


@dataclass(slots=True)
class TurnFinished(Event):
    turn_index: int
    stop_reason: str


@dataclass(slots=True)
class ErrorRaised(Event):
    message: str
    recoverable: bool = True


class EventBus:
    """Fan-out pub/sub over ``asyncio.Queue``.

    Slow consumers must not stall the agent loop: each subscriber gets its own
    bounded queue and drops coalescible deltas rather than applying backpressure.
    """

    #: Event types safe to drop when a subscriber falls behind. Losing a text
    #: delta degrades the render; losing a ToolCallFinished corrupts it.
    COALESCIBLE: ClassVar[tuple[type[Event], ...]] = (
        TextDelta,
        ThinkingDelta,
        ToolCallProgress,
        UsageUpdated,
    )

    def __init__(self, max_queue: int = 1024) -> None:
        self._max_queue = max_queue
        self._queues: list[asyncio.Queue[Event | None]] = []
        self._closed = False

    def publish(self, event: Event) -> None:
        """Non-blocking. Safe to call from inside the agent loop."""
        if self._closed:
            return
        for queue in self._queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                if isinstance(event, self.COALESCIBLE):
                    continue
                # Structural events must survive: evict the oldest item instead.
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with suppress(asyncio.QueueFull):
                    queue.put_nowait(event)

    async def subscribe(self) -> AsyncIterator[Event]:
        """Yield events until :meth:`close` is called."""
        queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=self._max_queue)
        self._queues.append(queue)
        try:
            while True:
                event = await queue.get()
                if event is None:
                    return
                yield event
        finally:
            if queue in self._queues:
                self._queues.remove(queue)

    def close(self) -> None:
        self._closed = True
        for queue in self._queues:
            with suppress(asyncio.QueueFull):
                queue.put_nowait(None)
