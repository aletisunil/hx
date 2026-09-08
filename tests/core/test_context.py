"""Context assembly invariants.

The prefix-stability test is the one that protects cache hit rate. If it ever
starts failing, every request in the harness got more expensive.
"""

from __future__ import annotations

from pathlib import Path

from hx.core.context import MAX_CACHE_BREAKPOINTS, ContextBuilder, build_project_context
from hx.core.messages import TextBlock, assistant_message, user_message


def _builder(cwd: Path) -> ContextBuilder:
    return ContextBuilder(system_prompt="sys", cwd=cwd, keep_recent_turns=2)


def test_prefix_fingerprint_stable_across_turns(project: Path) -> None:
    """Adding conversation turns must not change the static prefix hash."""
    builder = _builder(project)
    tools = [{"name": "Read", "description": "d", "input_schema": {}}]
    first = builder.build(messages=[], tools=tools)

    messages = [user_message("hi"), assistant_message([TextBlock("there")])]
    second = builder.build(messages=messages, tools=tools)

    assert first.prefix_fingerprint() == second.prefix_fingerprint()


def test_tool_schema_order_is_deterministic(project: Path) -> None:
    """Same tools in a different input order must serialise identically."""
    tools_a = [{"name": "Bash"}, {"name": "mcp__b__x"}, {"name": "mcp__a__y"}]
    tools_b = [{"name": "mcp__a__y"}, {"name": "Bash"}, {"name": "mcp__b__x"}]

    assert _builder(project).build([], tools_a).tools == _builder(project).build([], tools_b).tools


def test_builtins_sort_ahead_of_mcp_tools(project: Path) -> None:
    ordered = _builder(project).build([], [{"name": "mcp__a__y"}, {"name": "Zebra"}]).tools
    assert [t["name"] for t in ordered] == ["Zebra", "mcp__a__y"]


def test_breakpoints_respect_provider_limit(project: Path) -> None:
    builder = _builder(project)
    messages = [user_message(f"m{i}") for i in range(40)]
    ctx = builder.build(messages=messages, tools=[], cache_mode="explicit")
    assert len(ctx.breakpoints) <= MAX_CACHE_BREAKPOINTS


def test_implicit_cache_mode_emits_no_breakpoints(project: Path) -> None:
    ctx = _builder(project).build(messages=[], tools=[], cache_mode="implicit")
    assert ctx.breakpoints == ()


def test_rolling_breakpoint_holds_still_between_short_turns(project: Path) -> None:
    """Advancing the rolling breakpoint every turn would rewrite the cache
    constantly and cost more than the hit it buys."""
    builder = _builder(project)
    messages = [user_message("x" * 200) for _ in range(10)]

    first = builder.build(messages, []).breakpoints
    messages.append(user_message("short follow-up"))
    second = builder.build(messages, []).breakpoints

    assert first == second


def test_compacted_messages_are_excluded(project: Path) -> None:
    kept = user_message("keep")
    dropped = user_message("drop")
    dropped.compacted = True
    ctx = _builder(project).build([dropped, kept], [])
    assert ctx.messages == [kept]


def test_project_context_reports_cwd(project: Path) -> None:
    assert str(project) in build_project_context(project)
