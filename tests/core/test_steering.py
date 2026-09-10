"""Steering: getting a message into a turn that is already running.

Queueing answers "run this next". Steering answers "no, do this instead" - the
question a user asks while watching the agent head the wrong way, when waiting
for a hundred tool calls to finish is not an answer.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from hx.config import load_settings
from hx.core.context import ContextBuilder
from hx.core.events import EventBus
from hx.core.lateinject import InjectionRegistry
from hx.core.loop import AgentLoop
from hx.core.messages import StopReason, ToolUseBlock
from hx.core.session import new_session
from hx.core.usage import TurnUsage
from hx.providers.base import StreamDelta, StreamEnd, StreamItem
from hx.providers.fake import FakeProvider, Pause, text_turn
from hx.tools.base import Tool, ToolContext, ToolResult
from hx.tools.registry import ToolRegistry

MODEL = "anthropic/claude-sonnet-4.5"


def build_loop(
    script: list[list[StreamItem]],
    tmp_path: Path,
    tools: ToolRegistry | None = None,
) -> AgentLoop:
    session = new_session(tmp_path, MODEL)
    session.set_title("steered session")  # naming has its own tests
    return AgentLoop(
        provider=FakeProvider(script),
        session=session,
        tools=tools or ToolRegistry(),
        permissions=None,
        context=ContextBuilder("sys", tmp_path),
        compactor=None,
        injections=InjectionRegistry(),
        bus=EventBus(),
        settings=load_settings(tmp_path),
        model_info=None,
    )


def user_texts(loop: AgentLoop) -> list[str]:
    return [m.text() for m in loop.session.messages if m.role == "user"]


async def test_a_steer_interrupts_the_stream_and_lands_in_the_next_call(
    hx_home: Path, tmp_path: Path
) -> None:
    pause = Pause()
    script = [
        [
            StreamDelta(text="I will rewrite the parser"),
            pause,
            StreamDelta(text=" and everything under it"),
            StreamEnd(stop_reason=StopReason.END_TURN, usage=TurnUsage()),
        ],
        text_turn("stopping there"),
    ]
    loop = build_loop(script, tmp_path)

    turn = asyncio.ensure_future(loop.run("clean up the parser"))
    await loop.provider.wait_for_requests(1)

    assert loop.steer("stop - only fix the tokeniser") is True
    await loop.provider.wait_for_requests(2)
    pause.release()
    await turn

    second = loop.provider.requests[1].context.messages
    assert "stop - only fix the tokeniser" in second[-1].text()
    assert second[-1].role == "user"
    # The text that had already streamed is kept: the user watched it arrive.
    assert any("I will rewrite the parser" in m.text() for m in second if m.role == "assistant")
    assert " and everything under it" not in " ".join(m.text() for m in second)


async def test_a_steered_turn_never_leaves_an_unanswered_tool_call(
    hx_home: Path, tmp_path: Path
) -> None:
    """A ``tool_use`` with no result is a wire-format error on the next call."""
    pause = Pause()
    script = [
        [
            StreamDelta(text="reading it now"),
            StreamDelta(tool_use_id="t1", tool_name="Echo", tool_input_json='{"text": "hi"}'),
            pause,
            StreamEnd(stop_reason=StopReason.TOOL_USE, usage=TurnUsage()),
        ],
        text_turn("understood"),
    ]
    loop = build_loop(script, tmp_path)

    turn = asyncio.ensure_future(loop.run("look at the parser"))
    await loop.provider.wait_for_requests(1)
    loop.steer("actually look at the lexer")
    await loop.provider.wait_for_requests(2)
    pause.release()
    await turn

    sent = loop.provider.requests[1].context.messages
    blocks = [b for m in sent for b in m.content if isinstance(b, ToolUseBlock)]
    assert blocks == [], "an unexecuted tool call was left in the history"
    assert "actually look at the lexer" in sent[-1].text()


class SlowEcho(Tool):
    """A tool that parks until released, so a steer can arrive mid-execution."""

    name = "SlowEcho"
    description = "echo, slowly"
    mutating = False

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.finished = False

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.started.set()
        await self.gate.wait()
        self.finished = True
        return ToolResult(content=params["text"], summary="echoed")


async def test_a_steer_during_tool_execution_waits_for_the_boundary(
    hx_home: Path, tmp_path: Path
) -> None:
    """Cancelling a running tool is how a working tree ends up half-written, so
    the tool finishes and the steer lands right after its result."""
    tool = SlowEcho()
    registry = ToolRegistry()
    registry.register(tool)
    script = [
        [
            StreamDelta(tool_use_id="t1", tool_name="SlowEcho", tool_input_json='{"text": "hi"}'),
            StreamEnd(stop_reason=StopReason.TOOL_USE, usage=TurnUsage()),
        ],
        text_turn("done"),
    ]
    loop = build_loop(script, tmp_path, tools=registry)

    turn = asyncio.ensure_future(loop.run("echo something"))
    await asyncio.wait_for(tool.started.wait(), timeout=2)
    loop.steer("stop echoing")
    tool.gate.set()
    await turn

    assert tool.finished, "the running tool was cancelled out from under itself"
    sent = loop.provider.requests[1].context.messages
    assert "stop echoing" in sent[-1].text()
    assert sent[-1].role == "user"
    # The tool result rides its own message, ahead of the steer.
    assert "hi" in sent[-2].text() or any("hi" in str(b) for b in sent[-2].content)


async def test_a_steer_delivered_between_turns_is_not_lost(hx_home: Path, tmp_path: Path) -> None:
    """Steered while nothing is running, it is simply the next thing said."""
    loop = build_loop([text_turn("first"), text_turn("second")], tmp_path)

    await loop.run("do the thing")
    assert loop.steer("and now this") is False
    assert loop.take_pending_steer() == ["and now this"]
    assert loop.take_pending_steer() == []


async def test_a_cancelled_turn_hands_pending_steers_back(hx_home: Path, tmp_path: Path) -> None:
    """The words the user typed are theirs; an interrupt must not eat them."""
    pause = Pause()
    script = [[StreamDelta(text="working"), pause, StreamEnd(StopReason.END_TURN, TurnUsage())]]
    loop = build_loop(script, tmp_path)

    turn = asyncio.ensure_future(loop.run("start"))
    await loop.provider.wait_for_requests(1)
    loop.steer("wait, stop")
    loop.cancel()
    turn.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await turn
    pause.release()

    assert loop.take_pending_steer() == ["wait, stop"]


async def test_steering_a_subagent_is_refused(hx_home: Path, tmp_path: Path) -> None:
    """A subagent's conversation is not the user's to talk into mid-flight."""
    loop = build_loop([text_turn("done")], tmp_path)
    loop.origin = "reviewer subagent"

    assert loop.steer("stop") is False
    assert loop.take_pending_steer() == []
