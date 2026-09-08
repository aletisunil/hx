"""End-to-end: loop + real tools + permission engine + sandbox.

The unit tests check each layer alone. These check that a turn actually edits a
file, that a denial reaches the model as something it can act on, and that plan
mode does not merely refuse writes but never offers them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hx.config import PermissionMode, load_settings
from hx.core.context import ContextBuilder
from hx.core.events import EventBus
from hx.core.lateinject import InjectionRegistry
from hx.core.loop import AgentLoop
from hx.core.session import new_session
from hx.permissions.engine import (
    Decision,
    GrantScope,
    PermissionAnswer,
    PermissionEngine,
    PermissionRequest,
    parse_rule,
)
from hx.providers.base import StreamItem
from hx.providers.fake import FakeProvider, text_turn, tool_turn
from hx.providers.models import ModelRegistry
from hx.tools.bash import BackgroundJobs, PersistentShell
from hx.tools.read import FileTracker
from hx.tools.registry import build_default_registry

MODEL = "anthropic/claude-sonnet-4.5"


def build(
    tmp_path: Path,
    script: list[list[StreamItem]],
    *,
    mode: PermissionMode = PermissionMode.DEFAULT,
    rules: list[Any] | None = None,
    asker: Any = None,
    shell: PersistentShell | None = None,
) -> AgentLoop:
    settings = load_settings(tmp_path, {"permissions": {"mode": mode.value}})
    jobs = BackgroundJobs(tmp_path / ".hx" / "jobs")
    return AgentLoop(
        provider=FakeProvider(script),
        session=new_session(tmp_path, MODEL),
        tools=build_default_registry(shell, jobs if shell else None, FileTracker()),
        permissions=PermissionEngine(mode, rules or [], tmp_path, asker=asker),
        context=ContextBuilder("sys", tmp_path),
        compactor=None,
        injections=InjectionRegistry(),
        bus=EventBus(),
        settings=settings,
        model_info=ModelRegistry().get_or_default(MODEL),
    )


async def test_the_agent_reads_and_edits_a_real_file(hx_home: Path, tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("def greet():\n    return 'hi'\n")

    script = [
        tool_turn("Read", {"file_path": "app.py"}, "r1"),
        tool_turn(
            "Edit",
            {"file_path": "app.py", "old_string": "'hi'", "new_string": "'hello'"},
            "e1",
        ),
        text_turn("Updated the greeting."),
    ]
    rules = [parse_rule("Edit(**)", "test", Decision.ALLOW)]
    loop = build(tmp_path, script, rules=rules)

    result = await loop.run("make it say hello")

    assert result.error is None
    assert target.read_text() == "def greet():\n    return 'hello'\n"


async def test_an_edit_without_a_prior_read_is_refused_recoverably(
    hx_home: Path, tmp_path: Path
) -> None:
    """The model must see why it failed, not a dead turn."""
    target = tmp_path / "app.py"
    target.write_text("body\n")

    script = [
        tool_turn("Edit", {"file_path": "app.py", "old_string": "body", "new_string": "x"}, "e1"),
        text_turn("I should read it first."),
    ]
    loop = build(tmp_path, script, rules=[parse_rule("Edit(**)", "t", Decision.ALLOW)])
    await loop.run("edit it")

    tool_result = loop.session.messages[2].tool_results()[0]
    assert tool_result.is_error
    assert "has not been read" in tool_result.content
    assert target.read_text() == "body\n"


async def test_a_denied_tool_tells_the_model_why(hx_home: Path, tmp_path: Path) -> None:
    async def refuse(request: PermissionRequest) -> PermissionAnswer:
        return PermissionAnswer(allowed=False)

    script = [
        tool_turn("Write", {"file_path": "new.py", "content": "x"}, "w1"),
        text_turn("Understood, leaving it alone."),
    ]
    loop = build(tmp_path, script, asker=refuse)
    await loop.run("write a file")

    tool_result = loop.session.messages[2].tool_results()[0]
    assert tool_result.is_error
    assert "declined" in tool_result.content
    assert not (tmp_path / "new.py").exists()


async def test_plan_mode_never_offers_the_mutating_tools(hx_home: Path, tmp_path: Path) -> None:
    """Hiding them beats refusing them: the model cannot waste a turn on a call
    that was never going to be allowed."""
    loop = build(tmp_path, [text_turn("here is the plan")], mode=PermissionMode.PLAN)
    await loop.run("plan the work")

    offered = {tool["name"] for tool in loop.provider.requests[0].context.tools}
    assert "Read" in offered
    assert offered.isdisjoint({"Write", "Edit", "Bash"})


async def test_bash_runs_under_the_engine_and_reaches_the_shell(
    hx_home: Path, tmp_path: Path
) -> None:
    shell = PersistentShell(tmp_path)
    await shell.start()
    try:
        script = [
            tool_turn("Bash", {"command": "echo integration-ok"}, "b1"),
            text_turn("done"),
        ]
        loop = build(tmp_path, script, shell=shell)
        await loop.run("run it")

        tool_result = loop.session.messages[2].tool_results()[0]
        assert "integration-ok" in tool_result.content
        assert not tool_result.is_error
    finally:
        await shell.close()


async def test_a_chained_command_is_not_covered_by_a_narrow_allow_rule(
    hx_home: Path, tmp_path: Path
) -> None:
    """The end-to-end version of the parser test: an allow rule for `echo` must
    not carry a second, unrelated command along with it."""
    asked: list[str] = []

    async def record(request: PermissionRequest) -> PermissionAnswer:
        asked.append(str(request.specifier))
        return PermissionAnswer(True, GrantScope.ONCE)

    shell = PersistentShell(tmp_path)
    await shell.start()
    try:
        script = [
            tool_turn("Bash", {"command": "echo safe && touch sneaky.txt"}, "b1"),
            text_turn("done"),
        ]
        loop = build(
            tmp_path,
            script,
            rules=[parse_rule("Bash(echo:*)", "t", Decision.ALLOW)],
            asker=record,
            shell=shell,
        )
        await loop.run("run it")
    finally:
        await shell.close()

    assert asked == ["echo safe && touch sneaky.txt"]


async def test_tool_schemas_stay_stable_across_turns(hx_home: Path, tmp_path: Path) -> None:
    """Every builtin lands in the cached prefix; a wobbling schema costs a
    cache miss on every request."""
    (tmp_path / "a.py").write_text("x\n")
    script = [tool_turn("Read", {"file_path": "a.py"}, "r1"), text_turn("ok")]
    loop = build(tmp_path, script)
    await loop.run("read it")

    fingerprints = loop.provider.prefix_fingerprints()
    assert len(fingerprints) == 2
    assert len(set(fingerprints)) == 1


@pytest.mark.sandbox
async def test_the_sandbox_stops_a_write_the_rules_would_have_allowed(
    hx_home: Path, tmp_path: Path
) -> None:
    """Defence in depth: the rule engine says yes, the OS still says no."""
    from hx.permissions.sandbox import Sandbox, SandboxBackend, default_policy

    if Sandbox(default_policy(tmp_path)).backend is SandboxBackend.NONE:
        pytest.skip("no sandbox backend")

    outside = Path.home() / ".hx-integration-probe"
    outside.unlink(missing_ok=True)

    shell = PersistentShell(tmp_path, sandbox=Sandbox(default_policy(tmp_path)))
    await shell.start()
    try:
        script = [
            tool_turn("Bash", {"command": f"echo pwned > {outside}"}, "b1"),
            text_turn("blocked"),
        ]
        loop = build(
            tmp_path,
            script,
            rules=[parse_rule("Bash(echo:*)", "t", Decision.ALLOW)],
            shell=shell,
        )
        await loop.run("write outside")
        assert not outside.exists()
    finally:
        await shell.close()
        outside.unlink(missing_ok=True)


async def test_compaction_replaces_history_and_the_turn_continues(
    hx_home: Path, tmp_path: Path
) -> None:
    """The whole point: a long session keeps working instead of hitting the wall."""
    from hx.core.compaction import SUMMARY_MARKER, Compactor
    from hx.core.messages import TextBlock, assistant_message, user_message

    loop = build(tmp_path, [text_turn("carrying on")])
    loop.compactor = Compactor(
        provider=FakeProvider([text_turn("## Goal\nfinish the refactor")]),
        model=MODEL,
        keep_recent_turns=2,
        context=loop.context,
    )

    for index in range(10):
        loop.session.append(user_message(f"q{index}"))
        loop.session.append(assistant_message([TextBlock(f"a{index}")]))

    assert await loop.compact(reason="test")

    active = loop.session.active_messages()
    assert SUMMARY_MARKER in active[0].text()
    assert "finish the refactor" in active[0].text()
    assert len(active) == 3

    await loop.run("what next?")
    assert loop.session.messages[-1].text() == "carrying on"


async def test_compaction_fires_automatically_at_the_threshold(
    hx_home: Path, tmp_path: Path
) -> None:
    from hx.core.compaction import Compactor
    from hx.core.messages import TextBlock, assistant_message, user_message

    loop = build(tmp_path, [text_turn("ok")])
    loop.compactor = Compactor(
        provider=FakeProvider([text_turn("digest")]),
        model=MODEL,
        keep_recent_turns=2,
        context=loop.context,
    )
    for index in range(8):
        loop.session.append(user_message(f"q{index}"))
        loop.session.append(assistant_message([TextBlock(f"a{index}")]))

    # Pretend the window is nearly full.
    loop.session.usage.context_tokens = 100_000
    loop.session.usage.context_window = 110_000

    await loop.run("continue")

    assert any(m.compacted for m in loop.session.messages)


async def test_todos_reach_the_model_through_late_injection(hx_home: Path, tmp_path: Path) -> None:
    """Todo state must ride the tail of the request, never the system prefix."""
    from hx.tools.todo import TodoList, todo_injector

    todos = TodoList()
    script = [
        tool_turn(
            "TodoWrite",
            {"todos": [{"content": "run the tests", "status": "in_progress"}]},
            "t1",
        ),
        text_turn("done"),
    ]

    settings = load_settings(tmp_path)
    loop = build(tmp_path, script)
    loop.tools = build_default_registry(None, None, FileTracker(), todos, loop.bus)
    loop.injections.register("todos", todo_injector(todos))
    assert settings is not None

    await loop.run("track your work")

    first, second = loop.provider.requests
    assert "run the tests" not in first.context.system_text()
    assert "run the tests" in second.context.messages[-1].text()
    assert "run the tests" not in second.context.system_text()


async def test_the_cached_prefix_survives_todo_updates(hx_home: Path, tmp_path: Path) -> None:
    """Todos change every turn; if they sat in the prefix every turn would miss."""
    from hx.tools.todo import TodoList, todo_injector

    todos = TodoList()
    script = [
        tool_turn("TodoWrite", {"todos": [{"content": "one", "status": "in_progress"}]}, "t1"),
        tool_turn("TodoWrite", {"todos": [{"content": "two", "status": "completed"}]}, "t2"),
        text_turn("done"),
    ]
    loop = build(tmp_path, script)
    loop.tools = build_default_registry(None, None, FileTracker(), todos, loop.bus)
    loop.injections.register("todos", todo_injector(todos))

    await loop.run("go")

    assert len(set(loop.provider.prefix_fingerprints())) == 1


async def test_a_file_changed_on_disk_is_flagged_to_the_model(
    hx_home: Path, tmp_path: Path
) -> None:
    from hx.cli import _stale_files_injector
    from hx.core.lateinject import Injection

    tracker = FileTracker()
    target = tmp_path / "a.py"
    target.write_text("v1")

    script = [tool_turn("Read", {"file_path": "a.py"}, "r1"), text_turn("read it")]
    loop = build(tmp_path, script)
    loop.tools = build_default_registry(None, None, tracker)
    loop.injections.register("stale", _stale_files_injector(tracker, Injection))

    await loop.run("read the file")
    target.write_text("v2 from elsewhere")

    loop.provider = FakeProvider([text_turn("noted")])
    await loop.run("now edit it")

    assert "changed on disk" in loop.provider.requests[0].context.messages[-1].text()


async def _approve_everything(request: PermissionRequest) -> PermissionAnswer:
    return PermissionAnswer(True, GrantScope.SESSION)


async def test_a_skill_body_enters_context_only_when_called(hx_home: Path, tmp_path: Path) -> None:
    """Progressive disclosure end to end: the index is always there, the body
    only after the model asks for it."""
    from hx.skills.loader import build_index, discover
    from hx.skills.runtime import SkillTool

    skills_dir = tmp_path / ".hx" / "skills" / "deploy"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: Ship a release\n---\n\nSTEP ONE: tag the commit\n"
    )
    skills = {skill.name: skill for skill in discover(tmp_path)}

    script = [tool_turn("Skill", {"name": "deploy"}, "s1"), text_turn("Tagging now.")]
    loop = build(tmp_path, script)
    loop.tools.register(SkillTool(skills))
    loop.skills_index = build_index(list(skills.values()))

    await loop.run("deploy the release")

    first, second = loop.provider.requests
    assert "Ship a release" in first.context.system_text()
    assert "STEP ONE" not in first.context.system_text()
    assert "STEP ONE" not in second.context.system_text()
    assert any(
        "STEP ONE" in block.content
        for message in second.context.messages
        for block in message.content
        if hasattr(block, "content")
    )


async def test_a_subagent_keeps_its_tool_output_out_of_the_parent(
    hx_home: Path, tmp_path: Path
) -> None:
    """The parent pays for the report, not for the search that produced it."""
    from hx.agents.definitions import discover as discover_agents
    from hx.agents.subagent import SubagentRunner
    from hx.tools.task import TaskTool

    for index in range(3):
        (tmp_path / f"file{index}.py").write_text(f"# needle {index}\n" * 200)

    runner = SubagentRunner(
        definitions={a.name: a for a in discover_agents(tmp_path)},
        provider=FakeProvider(
            [
                tool_turn("Grep", {"pattern": "needle", "output_mode": "content"}, "g1"),
                text_turn("The needle is in file0.py:1."),
            ]
        ),
        tools=build_default_registry(None, None, FileTracker()),
        permissions=None,
        bus=EventBus(),
        settings=load_settings(tmp_path),
        parent_usage=None,
    )

    loop = build(
        tmp_path,
        [
            tool_turn("Task", {"subagent_type": "explore", "prompt": "find the needle"}, "t1"),
            text_turn("Thanks."),
        ],
        asker=_approve_everything,
    )
    loop.tools.register(TaskTool(runner))

    await loop.run("where is the needle?")

    parent_result = loop.session.messages[2].tool_results()[0]
    assert parent_result.content == "The needle is in file0.py:1."
    assert "needle 0\n# needle 0" not in parent_result.content


async def test_mcp_tools_join_the_registry_without_disturbing_the_prefix(
    hx_home: Path, tmp_path: Path
) -> None:
    import sys

    from hx.mcp.manager import MCPManager, MCPServerConfig

    server = Path(__file__).parent / "mcp" / "fixtures" / "echo_server.py"
    manager = MCPManager(
        [
            MCPServerConfig(
                name="demo",
                transport="stdio",
                command=sys.executable,
                args=(str(server),),
                timeout=15.0,
            )
        ]
    )

    script = [
        tool_turn("mcp__demo__echo", {"message": "from mcp"}, "m1"),
        text_turn("done"),
    ]
    loop = build(tmp_path, script, asker=_approve_everything)
    try:
        await manager.connect_all()
        await manager.register_tools(loop.tools)

        await loop.run("call the mcp tool")

        assert loop.session.messages[2].tool_results()[0].content == "from mcp"
        offered = [tool["name"] for tool in loop.provider.requests[0].context.tools]
        assert offered == sorted(offered, key=lambda n: (n.startswith("mcp__"), n))
        assert len(set(loop.provider.prefix_fingerprints())) == 1
    finally:
        await manager.close_all()
