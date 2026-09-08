"""Subagent isolation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hx.agents.definitions import AgentDefinition, discover, parse_agent_file
from hx.agents.subagent import SubagentRunner
from hx.config import load_settings
from hx.core.events import EventBus, SubagentFinished, SubagentStarted
from hx.core.usage import TurnUsage, UsageLedger
from hx.providers.fake import FakeProvider, text_turn, tool_turn
from hx.tools.base import Tool, ToolContext, ToolResult
from hx.tools.read import FileTracker
from hx.tools.registry import build_default_registry
from hx.tools.task import TaskTool


class Marker(Tool):
    name = "Marker"
    description = "records that it ran"
    mutating = False

    def __init__(self) -> None:
        self.calls = 0

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.calls += 1
        return ToolResult(content="a long intermediate result the parent should never see")


def build_runner(
    tmp_path: Path,
    script: list[Any],
    *,
    definitions: dict[str, AgentDefinition] | None = None,
    tools: Any = None,
    parent_usage: UsageLedger | None = None,
) -> SubagentRunner:
    return SubagentRunner(
        definitions=definitions or {a.name: a for a in discover(tmp_path)},
        provider=FakeProvider(script),
        tools=tools or build_default_registry(None, None, FileTracker()),
        permissions=None,
        bus=EventBus(),
        settings=load_settings(tmp_path),
        parent_session_id=None,
        parent_usage=parent_usage,
    )


async def test_subagents_cannot_spawn_subagents(hx_home: Path, tmp_path: Path) -> None:
    """Recursion is prevented by omitting Task from the allowlist, not by a counter."""
    tools = build_default_registry(None, None, FileTracker())
    runner = build_runner(tmp_path, [text_turn("done")], tools=tools)
    tools.register(TaskTool(runner))

    assert "Task" in tools.names()

    await runner.run("general", "do something", "test")
    offered = {tool["name"] for tool in runner.provider.requests[0].context.tools}
    assert "Task" not in offered


async def test_only_the_final_report_reaches_the_parent(hx_home: Path, tmp_path: Path) -> None:
    """Intermediate tool output staying out of the parent context is the reason
    subagents exist at all."""
    marker = Marker()
    tools = build_default_registry(None, None, FileTracker())
    tools.register(marker)

    script = [tool_turn("Marker", {}, "m1"), text_turn("Found it in app.py:42")]
    runner = build_runner(tmp_path, script, tools=tools)

    result = await runner.run("general", "find the thing", "searching")

    assert marker.calls == 1
    assert result.report == "Found it in app.py:42"
    assert "intermediate result" not in result.report


async def test_subagent_cost_rolls_into_the_session_ledger(hx_home: Path, tmp_path: Path) -> None:
    """A subagent spending real money without moving the parent's cost display
    makes that display a lie."""
    parent = UsageLedger()
    usage = TurnUsage(input_tokens=500, output_tokens=50, cost_usd=0.02)
    runner = build_runner(tmp_path, [text_turn("done", usage=usage)], parent_usage=parent)

    result = await runner.run("general", "task", "working")

    assert result.cost_usd == pytest.approx(0.02)
    assert parent.total_cost_usd == pytest.approx(0.02)
    assert parent.total_input == 500


async def test_read_only_agents_get_a_read_only_toolset(hx_home: Path, tmp_path: Path) -> None:
    runner = build_runner(tmp_path, [text_turn("report")])
    await runner.run("explore", "find the config loader", "exploring")

    offered = {tool["name"] for tool in runner.provider.requests[0].context.tools}
    assert offered == {"Read", "Glob", "Grep"}


async def test_agent_system_prompt_is_used_not_the_parents(hx_home: Path, tmp_path: Path) -> None:
    runner = build_runner(tmp_path, [text_turn("report")])
    await runner.run("plan", "design it", "planning")

    system = runner.provider.requests[0].context.system_text()
    assert "software architect" in system


async def test_unknown_agent_type_is_reported_not_raised(hx_home: Path, tmp_path: Path) -> None:
    result = await build_runner(tmp_path, [text_turn("x")]).run("nonexistent", "p", "d")
    assert result.is_error
    assert "Unknown agent type" in result.report


async def test_events_bracket_the_run(hx_home: Path, tmp_path: Path) -> None:
    import asyncio

    runner = build_runner(tmp_path, [text_turn("done")])
    seen: list[Any] = []

    async def consume() -> None:
        async for event in runner.bus.subscribe():
            seen.append(event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await runner.run("general", "task", "working")
    await asyncio.sleep(0.05)
    runner.bus.close()
    await task

    assert any(isinstance(e, SubagentStarted) for e in seen)
    assert any(isinstance(e, SubagentFinished) for e in seen)


async def test_subagent_transcript_nests_under_the_parent(hx_home: Path, tmp_path: Path) -> None:
    """Subagent sessions must not clutter the /resume listing."""
    from hx.core.session import list_sessions, new_session

    parent = new_session(tmp_path, "m")
    runner = SubagentRunner(
        definitions={a.name: a for a in discover(tmp_path)},
        provider=FakeProvider([text_turn("done")]),
        tools=build_default_registry(None, None, FileTracker()),
        permissions=None,
        bus=EventBus(),
        settings=load_settings(tmp_path),
        parent_session_id=parent.meta.session_id,
    )
    await runner.run("general", "task", "working")

    listed = [meta.session_id for meta in list_sessions(tmp_path)]
    assert listed == [parent.meta.session_id]


def test_agent_definitions_load_from_disk(project: Path) -> None:
    agents_dir = project / ".hx" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Reviews a diff\ntools: Read, Grep\n"
        "model: openai/gpt-5\n---\n\nYou review code.\n"
    )

    found = {agent.name: agent for agent in discover(project)}
    assert found["reviewer"].tools == ("Read", "Grep")
    assert found["reviewer"].model == "openai/gpt-5"
    assert "You review code." in found["reviewer"].system_prompt
    assert "explore" in found, "builtins must survive alongside user definitions"


def test_a_project_agent_overrides_a_builtin(project: Path) -> None:
    agents_dir = project / ".hx" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "explore.md").write_text(
        "---\nname: explore\ndescription: my own explorer\n---\n\nCustom.\n"
    )
    found = {agent.name: agent for agent in discover(project)}
    assert found["explore"].description == "my own explorer"


def test_malformed_agent_file_is_reported(tmp_path: Path) -> None:
    from hx.frontmatter import FrontmatterError

    path = tmp_path / "bad.md"
    path.write_text("---\nname: bad\n---\nno description\n")
    with pytest.raises(FrontmatterError, match="description"):
        parse_agent_file(path)


async def test_subagent_prose_does_not_stream_into_the_parent_transcript(
    hx_home: Path, tmp_path: Path
) -> None:
    """A subagent's text is an intermediate result that reaches the user as the
    Task tool's output. Streaming it live would read as if the main assistant
    had said it."""
    import asyncio

    from hx.core.events import TextDelta, ToolCallStarted

    marker = Marker()
    tools = build_default_registry(None, None, FileTracker())
    tools.register(marker)
    runner = build_runner(
        tmp_path, [tool_turn("Marker", {}, "m1"), text_turn("the report")], tools=tools
    )

    seen: list[Any] = []

    async def consume() -> None:
        async for event in runner.bus.subscribe():
            seen.append(event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await runner.run("general", "task", "working")
    await asyncio.sleep(0.05)
    runner.bus.close()
    await task

    assert not [e for e in seen if isinstance(e, TextDelta)]

    started = [e for e in seen if isinstance(e, ToolCallStarted)]
    assert started and all("subagent > " in e.name for e in started)


async def test_parallel_task_calls_run_concurrently(hx_home: Path, tmp_path: Path) -> None:
    """Two Task calls in one turn must overlap.

    Task is mutating, and mutating tools are serialised by default so their side
    effects land in emission order. Subagents are independent by construction,
    so Task opts out - without that, "concurrent subagents" was just a comment.
    """
    import asyncio
    import time

    from hx.config import load_settings as _load_settings
    from hx.core.context import ContextBuilder
    from hx.core.lateinject import InjectionRegistry
    from hx.core.loop import AgentLoop
    from hx.core.messages import StopReason
    from hx.core.session import new_session
    from hx.providers.base import StreamDelta, StreamEnd
    from hx.providers.models import ModelRegistry

    class SlowRunner:
        def __init__(self) -> None:
            self.definitions = {"general": AgentDefinition("general", "d", "p")}
            self.spans: list[tuple[float, float]] = []

        async def run(self, agent_type: str, prompt: str, description: str) -> Any:
            start = time.monotonic()
            await asyncio.sleep(0.3)
            self.spans.append((start, time.monotonic()))
            from hx.agents.subagent import SubagentResult

            return SubagentResult("id", agent_type, "report", False, 1, 0.0)

    runner = SlowRunner()
    tools = build_default_registry(None, None, FileTracker())
    tools.register(TaskTool(runner))

    script = [
        [
            StreamDelta(
                tool_use_id="a",
                tool_name="Task",
                tool_input_json='{"subagent_type":"general","prompt":"one"}',
            ),
            StreamDelta(
                tool_use_id="b",
                tool_name="Task",
                tool_input_json='{"subagent_type":"general","prompt":"two"}',
            ),
            StreamEnd(stop_reason=StopReason.TOOL_USE),
        ],
        text_turn("done"),
    ]
    loop = AgentLoop(
        provider=FakeProvider(script),
        session=new_session(tmp_path, "m"),
        tools=tools,
        permissions=None,
        context=ContextBuilder("sys", tmp_path),
        compactor=None,
        injections=InjectionRegistry(),
        bus=EventBus(),
        settings=_load_settings(tmp_path),
        model_info=ModelRegistry().get_or_default("m"),
    )

    started = time.monotonic()
    await loop.run("do both")
    elapsed = time.monotonic() - started

    assert len(runner.spans) == 2
    assert elapsed < 0.55, f"the two subagents ran serially ({elapsed:.2f}s)"
    (_a_start, a_end), (b_start, _b_end) = runner.spans
    assert b_start < a_end, "the second subagent did not start until the first finished"
