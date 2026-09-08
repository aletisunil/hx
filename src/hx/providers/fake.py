"""Scripted provider for tests.

Replays a canned sequence of deltas and tool calls so the entire agent loop -
tool dispatch, output capping, compaction triggers, late injection,
cancellation - is deterministic and runs offline.

It also records every :class:`~hx.providers.base.ProviderRequest` it receives,
which is what the prefix-stability tests assert against.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from hx.core.messages import StopReason
from hx.core.usage import TurnUsage
from hx.providers.base import ProviderError, ProviderRequest, StreamDelta, StreamEnd, StreamItem


class FakeProvider:
    name = "fake"

    def __init__(self, script: list[list[StreamItem]]) -> None:
        """Args:
        script: One list of stream items per expected turn, consumed in order.
        """
        self._script = list(script)
        self._turn = 0
        self._requests: list[ProviderRequest] = []
        self.closed = False

    async def astream(self, request: ProviderRequest) -> AsyncIterator[StreamItem]:
        self._requests.append(request)
        if self._turn >= len(self._script):
            raise ProviderError(
                f"FakeProvider script exhausted after {self._turn} turns; "
                "the loop ran more turns than the test expected"
            )
        items = self._script[self._turn]
        self._turn += 1
        for item in items:
            yield item

    async def aclose(self) -> None:
        self.closed = True

    @property
    def requests(self) -> list[ProviderRequest]:
        """Every request seen, in order."""
        return self._requests

    def prefix_fingerprints(self) -> list[str]:
        """Prefix hash per recorded request. Identical values across turns means
        the KV cache stayed warm."""
        return [r.context.prefix_fingerprint() for r in self._requests]


def text_turn(text: str, *, usage: TurnUsage | None = None) -> list[StreamItem]:
    """Script helper: a plain text response ending the turn."""
    return [
        StreamDelta(text=text),
        StreamEnd(stop_reason=StopReason.END_TURN, usage=usage or TurnUsage()),
    ]


def tool_turn(
    name: str,
    tool_input: dict[str, object],
    tool_use_id: str = "t1",
    *,
    text: str | None = None,
    usage: TurnUsage | None = None,
) -> list[StreamItem]:
    """Script helper: a single tool call."""
    items: list[StreamItem] = []
    if text:
        items.append(StreamDelta(text=text))
    items.append(
        StreamDelta(
            tool_use_id=tool_use_id,
            tool_name=name,
            tool_input_json=json.dumps(tool_input, sort_keys=True),
        )
    )
    items.append(StreamEnd(stop_reason=StopReason.TOOL_USE, usage=usage or TurnUsage()))
    return items
