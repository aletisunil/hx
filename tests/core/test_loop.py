"""Agent loop behaviour, driven by the scripted fake provider."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from hx.config import load_settings
from hx.core.context import ContextBuilder
from hx.core.events import EventBus, TextDelta, ToolCallFinished, TurnFinished
from hx.core.lateinject import Injection, InjectionRegistry
from hx.core.loop import AgentLoop
from hx.core.messages import StopReason
from hx.core.session import new_session
from hx.core.title import TITLE_MAX_TOKENS
from hx.core.usage import TurnUsage
from hx.hooks.engine import HookEngine
from hx.hooks.spec import HookCommand, HookEvent
from hx.providers.base import StreamDelta, StreamEnd, StreamItem
from hx.providers.fake import FakeProvider, text_turn, tool_turn
from hx.providers.models import ModelRegistry
from hx.tools.base import Tool, ToolContext, ToolResult
from hx.tools.registry import ToolRegistry


class EchoTool(Tool):
    name = "Echo"
    description = "echo"
    mutating = False

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(content=params["text"], summary="echoed")


class Harness:
    """A loop wired to a scripted provider, plus the events it published."""

    def __init__(self, provider: FakeProvider, loop: AgentLoop, bus: EventBus) -> None:
        self.provider = provider
        self.loop = loop
        self.bus = bus
        self.events: list[Any] = []
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Harness:
        async def consume() -> None:
            async for event in self.bus.subscribe():
                self.events.append(event)

        self._task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await asyncio.sleep(0.05)
        self.bus.close()
        if self._task is not None:
            await self._task


def build_loop(
    script: list[list[StreamItem]],
    tmp_path: Path,
    tools: ToolRegistry | None = None,
    title: str | None = "scripted session",
    hooks: HookEngine | None = None,
) -> Harness:
    provider = FakeProvider(script)
    bus = EventBus()
    session = new_session(tmp_path, "anthropic/claude-sonnet-4.5")
    # An already-named session does not ask the model for a name, so the script
    # a test writes is exactly the turns it gets. The naming call itself is
    # exercised in the tests that pass ``title=None``.
    if title:
        session.set_title(title)
    loop = AgentLoop(
        provider=provider,
        session=session,
        tools=tools or ToolRegistry(),
        permissions=None,
        context=ContextBuilder("sys", tmp_path, keep_recent_turns=2),
        compactor=None,
        injections=InjectionRegistry(),
        bus=bus,
        settings=load_settings(tmp_path),
        model_info=ModelRegistry().get_or_default("anthropic/claude-sonnet-4.5"),
        hooks=hooks,
    )
    return Harness(provider, loop, bus)


@pytest.fixture()
def registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return registry


async def test_loop_stops_on_text_only_turn(hx_home: Path, tmp_path: Path) -> None:
    async with build_loop([text_turn("done")], tmp_path) as h:
        result = await h.loop.run("hello")

    assert result.stop_reason is StopReason.END_TURN
    assert len(h.provider.requests) == 1
    assert h.loop.session.messages[-1].text() == "done"


async def test_tool_results_are_appended_before_the_next_turn(
    hx_home: Path, tmp_path: Path, registry: ToolRegistry
) -> None:
    script = [tool_turn("Echo", {"text": "pong"}), text_turn("ok")]
    async with build_loop(script, tmp_path, registry) as h:
        await h.loop.run("ping")

    roles = [m.role for m in h.loop.session.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert h.loop.session.messages[2].tool_results()[0].content == "pong"


async def test_prefix_stays_warm_across_a_multi_turn_run(
    hx_home: Path, tmp_path: Path, registry: ToolRegistry
) -> None:
    """The whole point of the caching design: every turn reuses one prefix."""
    script = [tool_turn("Echo", {"text": "x"}), text_turn("ok")]
    async with build_loop(script, tmp_path, registry) as h:
        await h.loop.run("go")

    fingerprints = h.provider.prefix_fingerprints()
    assert len(fingerprints) == 2
    assert len(set(fingerprints)) == 1


async def test_late_injections_do_not_disturb_the_prefix(
    hx_home: Path, tmp_path: Path, registry: ToolRegistry
) -> None:
    """A reminder that changes every turn must not show up above a breakpoint."""
    script = [tool_turn("Echo", {"text": "x"}), text_turn("ok")]
    async with build_loop(script, tmp_path, registry) as h:
        counter = iter(range(100))
        h.loop.injections.register(
            "counter", lambda: Injection(source="counter", text=f"tick {next(counter)}")
        )
        await h.loop.run("go")

    assert len(set(h.provider.prefix_fingerprints())) == 1
    last = h.provider.requests[-1].context.messages[-1]
    assert "tick" in last.text()


async def test_unknown_tool_returns_an_error_result_not_an_exception(
    hx_home: Path, tmp_path: Path
) -> None:
    """A refusal must reach the model so it can adapt, not kill the turn."""
    script = [tool_turn("Nope", {}), text_turn("recovered")]
    async with build_loop(script, tmp_path) as h:
        result = await h.loop.run("go")

    assert result.stop_reason is StopReason.END_TURN
    tool_result = h.loop.session.messages[2].tool_results()[0]
    assert tool_result.is_error
    assert "Unknown tool" in tool_result.content


async def test_failing_tool_becomes_an_error_result(hx_home: Path, tmp_path: Path) -> None:
    class Boom(Tool):
        name = "Boom"
        description = "explodes"

        def schema(self) -> dict[str, Any]:
            return {"type": "object", "properties": {}}

        async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
            raise RuntimeError("kaboom")

    registry = ToolRegistry()
    registry.register(Boom())
    script = [tool_turn("Boom", {}), text_turn("handled")]
    async with build_loop(script, tmp_path, registry) as h:
        await h.loop.run("go")

    assert "kaboom" in h.loop.session.messages[2].tool_results()[0].content
    assert any(isinstance(e, ToolCallFinished) and e.is_error for e in h.events)


async def test_usage_and_events_are_published(
    hx_home: Path, tmp_path: Path, registry: ToolRegistry
) -> None:
    usage = TurnUsage(input_tokens=100, output_tokens=20, cache_read_tokens=900, cost_usd=0.01)
    async with build_loop([text_turn("hi", usage=usage)], tmp_path, registry) as h:
        await h.loop.run("go")

    assert h.loop.session.usage.total_cache_read == 900
    assert h.loop.session.usage.total_cost_usd == 0.01
    assert any(isinstance(e, TextDelta) for e in h.events)
    assert sum(isinstance(e, TurnFinished) for e in h.events) == 1


async def test_mutating_tools_run_serially_in_emission_order(hx_home: Path, tmp_path: Path) -> None:
    order: list[str] = []

    class Recorder(Tool):
        description = "records"
        mutating = True

        def __init__(self, name: str) -> None:
            self.name = name

        def schema(self) -> dict[str, Any]:
            return {"type": "object", "properties": {}}

        async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
            order.append(f"start:{self.name}")
            await asyncio.sleep(0.01)
            order.append(f"end:{self.name}")
            return ToolResult(content="ok")

    registry = ToolRegistry()
    registry.register(Recorder("A"))
    registry.register(Recorder("B"))

    script = [
        [
            StreamDelta(tool_use_id="1", tool_name="A", tool_input_json="{}"),
            StreamDelta(tool_use_id="2", tool_name="B", tool_input_json="{}"),
            StreamEnd(stop_reason=StopReason.TOOL_USE),
        ],
        text_turn("done"),
    ]
    async with build_loop(script, tmp_path, registry) as h:
        await h.loop.run("go")

    assert order == ["start:A", "end:A", "start:B", "end:B"]


async def test_permission_events_announce_only_what_actually_prompts(
    hx_home: Path, tmp_path: Path
) -> None:
    """Publishing on every check made headless runs report a permission prompt
    for calls that were auto-allowed and never asked about."""
    from hx.config import PermissionMode
    from hx.core.events import PermissionRequested
    from hx.permissions.engine import PermissionAnswer, PermissionEngine, PermissionRequest

    class Writer(Tool):
        name = "Write"
        description = "writes"
        mutating = True

        def schema(self) -> dict[str, Any]:
            return {"type": "object", "properties": {}}

        async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
            return ToolResult(content="ok")

    registry = ToolRegistry()
    registry.register(EchoTool())
    registry.register(Writer())

    async def asker(request: PermissionRequest) -> PermissionAnswer:
        return PermissionAnswer(allowed=True)

    script = [
        [
            StreamDelta(tool_use_id="1", tool_name="Echo", tool_input_json='{"text": "hi"}'),
            StreamEnd(stop_reason=StopReason.TOOL_USE),
        ],
        [
            StreamDelta(tool_use_id="2", tool_name="Write", tool_input_json="{}"),
            StreamEnd(stop_reason=StopReason.TOOL_USE),
        ],
        text_turn("done"),
    ]
    async with build_loop(script, tmp_path, registry) as h:
        h.loop.permissions = PermissionEngine(PermissionMode.DEFAULT, [], tmp_path, asker=asker)
        await h.loop.run("go")

    announced = [e.tool_name for e in h.events if isinstance(e, PermissionRequested)]
    assert announced == ["Write"], "the read-only Echo call never stopped for approval"


async def test_a_finished_turn_names_the_session(hx_home: Path, tmp_path: Path) -> None:
    """Without this, /resume lists timestamps instead of work."""
    script = [text_turn("done"), text_turn("Parser bug fix")]
    async with build_loop(script, tmp_path, title=None) as h:
        await h.loop.run("fix the parser")

    assert h.loop.session.meta.title == "Parser bug fix"
    assert h.provider.requests[-1].max_tokens == TITLE_MAX_TOKENS


async def test_a_session_is_named_once(hx_home: Path, tmp_path: Path) -> None:
    script = [text_turn("done"), text_turn("Parser bug fix"), text_turn("done again")]
    async with build_loop(script, tmp_path, title=None) as h:
        await h.loop.run("fix the parser")
        await h.loop.run("thanks")

    assert h.loop.session.meta.title == "Parser bug fix"
    assert len(h.provider.requests) == 3  # two turns, one naming call


async def test_naming_falls_back_to_the_first_message(hx_home: Path, tmp_path: Path) -> None:
    """The script runs out before the naming call, so the provider errors."""
    async with build_loop([text_turn("done")], tmp_path, title=None) as h:
        result = await h.loop.run("fix the parser")

    assert result.stop_reason is StopReason.END_TURN
    assert result.error is None
    assert h.loop.session.meta.title == "fix the parser"


async def test_the_naming_call_is_counted_in_the_ledger(hx_home: Path, tmp_path: Path) -> None:
    script = [
        text_turn("done", usage=TurnUsage(input_tokens=10, output_tokens=5)),
        text_turn("Parser bug fix", usage=TurnUsage(input_tokens=20, output_tokens=4)),
    ]
    async with build_loop(script, tmp_path, title=None) as h:
        await h.loop.run("fix the parser")

    assert h.loop.session.usage.total_input == 30
    assert h.loop.session.usage.total_output == 9


async def test_a_subagent_session_is_not_named(hx_home: Path, tmp_path: Path) -> None:
    """Subagent transcripts never appear in /resume, so naming them is spend
    with nothing behind it."""
    async with build_loop([text_turn("done")], tmp_path, title=None) as h:
        h.loop.origin = "reviewer subagent"
        await h.loop.run("fix the parser")

    assert h.loop.session.meta.title is None
    assert len(h.provider.requests) == 1


async def test_a_title_model_override_is_used(hx_home: Path, tmp_path: Path) -> None:
    async with build_loop([text_turn("done"), text_turn("Named")], tmp_path, title=None) as h:
        h.loop.settings = load_settings(tmp_path, {"models": {"title_model": "cheap/model"}})
        await h.loop.run("fix the parser")

    assert h.provider.requests[-1].model == "cheap/model"
    assert h.provider.requests[0].model == "anthropic/claude-sonnet-4.5"


async def test_closing_the_session_renames_it_for_what_it_became(
    hx_home: Path, tmp_path: Path
) -> None:
    """A name from the first exchange describes an opening question, not a session.

    ``/resume`` is picked from these names, so the one worth keeping is the one
    written when the work is done.
    """
    script = [
        text_turn("done"),
        text_turn("Parser bug fix"),
        text_turn("done again"),
        text_turn("Parser rewrite and tests"),
    ]
    async with build_loop(script, tmp_path, title=None) as h:
        await h.loop.run("fix the parser")
        assert h.loop.session.meta.title == "Parser bug fix"
        await h.loop.run("now rewrite it")

        await h.loop.retitle_session()

    assert h.loop.session.meta.title == "Parser rewrite and tests"


async def test_closing_an_unchanged_session_does_not_spend_a_call(
    hx_home: Path, tmp_path: Path
) -> None:
    """Nothing was said after the name was written, so there is nothing to rename."""
    script = [text_turn("done"), text_turn("Parser bug fix")]
    async with build_loop(script, tmp_path, title=None) as h:
        await h.loop.run("fix the parser")
        before = len(h.provider.requests)

        await h.loop.retitle_session()

    assert h.loop.session.meta.title == "Parser bug fix"
    assert len(h.provider.requests) == before


async def test_a_failed_rename_keeps_the_name_it_had(hx_home: Path, tmp_path: Path) -> None:
    """The script runs out, so the naming call raises. Losing the old name over
    that would make closing a session worse than not closing it."""
    script = [text_turn("done"), text_turn("Parser bug fix"), text_turn("done again")]
    async with build_loop(script, tmp_path, title=None) as h:
        await h.loop.run("fix the parser")
        await h.loop.run("now rewrite it")

        await h.loop.retitle_session()

    assert h.loop.session.meta.title == "Parser bug fix"


async def test_a_subagent_session_is_not_renamed_on_close(hx_home: Path, tmp_path: Path) -> None:
    async with build_loop([text_turn("done")], tmp_path, title=None) as h:
        h.loop.origin = "reviewer subagent"
        await h.loop.run("fix the parser")

        await h.loop.retitle_session()

    assert h.loop.session.meta.title is None
    assert len(h.provider.requests) == 1


# --- hooks ------------------------------------------------------------------


def _hooks(tmp_path: Path, event: HookEvent, *commands: HookCommand) -> HookEngine:
    return HookEngine(hooks={event: list(commands)}, cwd=tmp_path, session_id="s1")


async def test_pre_tool_use_hook_blocks_the_call(
    hx_home: Path, tmp_path: Path, registry: ToolRegistry
) -> None:
    """The tool never runs and the model is told why."""
    hooks = _hooks(
        tmp_path,
        HookEvent.PRE_TOOL_USE,
        HookCommand("echo 'Echo is off limits' >&2; exit 2", timeout=10),
    )
    script = [tool_turn("Echo", {"text": "hi"}), text_turn("understood")]
    async with build_loop(script, tmp_path, registry, hooks=hooks) as h:
        await h.loop.run("go")

    results = [m for m in h.loop.session.messages if m.tool_results()]
    block = results[0].tool_results()[0]
    assert block.is_error
    assert "Echo is off limits" in block.content
    assert "hi" not in block.content


async def test_pre_tool_use_hook_can_rewrite_the_input(
    hx_home: Path, tmp_path: Path, registry: ToolRegistry
) -> None:
    payload = '{"updatedInput": {"text": "rewritten"}}'
    hooks = _hooks(tmp_path, HookEvent.PRE_TOOL_USE, HookCommand(f"echo '{payload}'", timeout=10))
    script = [tool_turn("Echo", {"text": "original"}), text_turn("ok")]
    async with build_loop(script, tmp_path, registry, hooks=hooks) as h:
        await h.loop.run("go")

    results = [m for m in h.loop.session.messages if m.tool_results()]
    assert results[0].tool_results()[0].content == "rewritten"


async def test_post_tool_use_hook_appends_context(
    hx_home: Path, tmp_path: Path, registry: ToolRegistry
) -> None:
    payload = '{"additionalContext": "note: suite is red"}'
    hooks = _hooks(tmp_path, HookEvent.POST_TOOL_USE, HookCommand(f"echo '{payload}'", timeout=10))
    script = [tool_turn("Echo", {"text": "hi"}), text_turn("ok")]
    async with build_loop(script, tmp_path, registry, hooks=hooks) as h:
        await h.loop.run("go")

    results = [m for m in h.loop.session.messages if m.tool_results()]
    content = results[0].tool_results()[0].content
    assert content.startswith("hi")
    assert "note: suite is red" in content


async def test_user_prompt_submit_hook_can_refuse_the_turn(hx_home: Path, tmp_path: Path) -> None:
    hooks = _hooks(
        tmp_path,
        HookEvent.USER_PROMPT_SUBMIT,
        HookCommand("echo 'not now' >&2; exit 2", timeout=10),
    )
    async with build_loop([text_turn("unreachable")], tmp_path, hooks=hooks) as h:
        result = await h.loop.run("go")

    assert result.stop_reason is StopReason.ERROR
    assert result.error == "not now"
    assert h.provider.requests == []
    assert h.loop.session.messages == []


async def test_a_broken_hook_does_not_block_the_call(
    hx_home: Path, tmp_path: Path, registry: ToolRegistry
) -> None:
    """A typo in a hook is a notice, not a wedged session."""
    hooks = _hooks(tmp_path, HookEvent.PRE_TOOL_USE, HookCommand("exit 9", timeout=10))
    script = [tool_turn("Echo", {"text": "hi"}), text_turn("ok")]
    async with build_loop(script, tmp_path, registry, hooks=hooks) as h:
        await h.loop.run("go")

    results = [m for m in h.loop.session.messages if m.tool_results()]
    assert results[0].tool_results()[0].content == "hi"
    assert any("hook" in getattr(e, "message", "") for e in h.events)


async def test_stop_hook_fires_when_the_turn_ends(hx_home: Path, tmp_path: Path) -> None:
    marker = tmp_path / "stopped"
    hooks = _hooks(tmp_path, HookEvent.STOP, HookCommand(f"touch {marker}", timeout=10))
    async with build_loop([text_turn("done")], tmp_path, hooks=hooks) as h:
        await h.loop.run("go")

    assert marker.exists()


async def test_stop_hook_fires_when_the_turn_falls_over(hx_home: Path, tmp_path: Path) -> None:
    """The exits nobody planned are the ones a cleanup hook is needed on."""
    marker = tmp_path / "stopped"
    hooks = _hooks(tmp_path, HookEvent.STOP, HookCommand(f"touch {marker}", timeout=10))
    # An empty script makes FakeProvider raise ProviderError on the first turn.
    async with build_loop([], tmp_path, hooks=hooks) as h:
        result = await h.loop.run("go")

    assert result.stop_reason is StopReason.ERROR
    assert marker.exists()


async def test_subagents_inherit_the_parent_hooks(hx_home: Path, tmp_path: Path) -> None:
    """A guard that Task could bypass would not be a guard."""
    from hx.agents.definitions import AgentDefinition
    from hx.agents.subagent import SubagentRunner

    hooks = _hooks(tmp_path, HookEvent.PRE_TOOL_USE, HookCommand("exit 2", timeout=10))
    runner = SubagentRunner(
        definitions={"probe": AgentDefinition(name="probe", description="d", system_prompt="s")},
        provider=FakeProvider([]),
        tools=ToolRegistry(),
        permissions=None,
        bus=EventBus(),
        settings=load_settings(tmp_path),
        hooks=hooks,
    )
    child = runner._build_loop(runner.definitions["probe"], "sub-1")
    assert child.hooks is hooks
