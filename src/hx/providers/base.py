"""Provider protocol.

A provider converts an :class:`~hx.core.context.AssembledContext` into wire
format, streams the response, and converts it back into core message types.
All provider-specific quirks - cache markers, reasoning fields, tool-call
encodings - stay behind this boundary.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from hx.core.context import AssembledContext
from hx.core.messages import StopReason
from hx.core.usage import TurnUsage


@dataclass(slots=True)
class StreamDelta:
    """A chunk off the wire."""

    text: str | None = None
    thinking: str | None = None
    tool_use_id: str | None = None
    tool_name: str | None = None
    tool_input_json: str | None = None
    """Partial JSON for a tool call; accumulated by the consumer."""


@dataclass(slots=True)
class StreamEnd:
    stop_reason: StopReason
    usage: TurnUsage = field(default_factory=TurnUsage)


StreamItem = StreamDelta | StreamEnd


@dataclass(slots=True)
class ProviderRequest:
    context: AssembledContext
    model: str
    max_tokens: int
    temperature: float | None = None
    stop_sequences: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Provider(Protocol):
    name: str

    def astream(self, request: ProviderRequest) -> AsyncIterator[StreamItem]:
        """Stream a completion. Must raise :class:`ProviderError` on failure.

        Implementations are async generator functions, so this is declared as a
        plain ``def`` returning an ``AsyncIterator`` rather than ``async def``.
        """
        ...

    async def aclose(self) -> None: ...


class ProviderError(Exception):
    """Transport or API failure.

    Attributes:
        retryable: Whether the loop should retry with backoff (429, 5xx,
            connection resets) rather than surfacing to the user.
    """

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable
