"""Context assembly invariants.

The prefix-stability test is the one that protects cache hit rate. If it ever
starts failing, every request in the harness got more expensive.
"""

from __future__ import annotations

from pathlib import Path

from hx.core.context import (
    MAX_CACHE_BREAKPOINTS,
    SYSTEM_PROMPT,
    ContextBuilder,
    build_project_context,
    load_system_prompt,
    resolve_system_prompt,
)
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


def test_project_context_carries_agents_md(project: Path) -> None:
    (project / "AGENTS.md").write_text("run the tests with pytest")
    context = build_project_context(project)
    assert "# Project instructions (AGENTS.md)" in context
    assert "run the tests with pytest" in context


def test_project_context_without_agents_md_says_nothing_about_instructions(
    project: Path,
) -> None:
    assert "Project instructions" not in build_project_context(project)


def test_the_built_in_prompt_is_used_when_nothing_overrides_it(
    hx_home: Path, project: Path
) -> None:
    resolved = resolve_system_prompt(project)
    assert resolved.text == SYSTEM_PROMPT
    assert resolved.source == "built-in"
    assert resolved.appends == ()


def test_a_project_file_replaces_the_built_in_prompt(hx_home: Path, project: Path) -> None:
    (project / ".hx" / "system-prompt.md").write_text("You are TESTBOT.\n")

    resolved = resolve_system_prompt(project)

    assert resolved.text == "You are TESTBOT."
    assert resolved.source.endswith(".hx/system-prompt.md")


def test_a_project_file_wins_over_the_user_file(hx_home: Path, project: Path) -> None:
    (hx_home / "system-prompt.md").write_text("user prompt")
    (project / ".hx" / "system-prompt.md").write_text("project prompt")

    assert resolve_system_prompt(project).text == "project prompt"


def test_the_flag_wins_over_every_file(hx_home: Path, project: Path) -> None:
    from hx.config import PromptSettings

    (hx_home / "system-prompt.md").write_text("user prompt")
    (project / ".hx" / "system-prompt.md").write_text("project prompt")

    resolved = resolve_system_prompt(project, PromptSettings(system="flag prompt"))

    assert resolved.text == "flag prompt"
    assert resolved.source == "--system-prompt"


def test_appends_stack_user_then_project_then_flags(hx_home: Path, project: Path) -> None:
    from hx.config import PromptSettings

    (hx_home / "system-prompt-append.md").write_text("from the user")
    (project / ".hx" / "system-prompt-append.md").write_text("from the project")

    resolved = resolve_system_prompt(project, PromptSettings(append=("from the flag",)))

    assert resolved.text.startswith(SYSTEM_PROMPT.rstrip("\n"))
    assert resolved.text.endswith("from the user\n\nfrom the project\n\nfrom the flag")
    assert len(resolved.appends) == 3


def test_an_empty_override_file_is_treated_as_absent(hx_home: Path, project: Path) -> None:
    (project / ".hx" / "system-prompt.md").write_text("   \n")
    assert resolve_system_prompt(project).source == "built-in"


def test_load_system_prompt_returns_the_resolved_text(hx_home: Path, project: Path) -> None:
    (project / ".hx" / "system-prompt.md").write_text("You are TESTBOT.")
    assert load_system_prompt(project) == "You are TESTBOT."
