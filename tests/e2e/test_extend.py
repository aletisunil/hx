"""Extending HX: project instructions, skills, subagents, hooks, MCP servers, settings."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from tests.e2e.conftest import HX
from tests.e2e.stub import GPT5, SONNET, Stub, call, say

ECHO_SERVER = Path(__file__).parent / "fixtures" / "echo_server.py"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_agents_md_reaches_the_model(hx: HX, stub: Stub) -> None:
    """The project's AGENTS.md is part of what every request carries."""
    write(hx.project / "AGENTS.md", "Run tests with `make check`, never pytest directly.\n")
    stub.script(say("Noted."))
    hx.run("-p", "how do I test?", check=True)
    assert "Run tests with `make check`" in json.dumps(stub.requests[0].messages)


def test_user_agents_md_reaches_the_model_before_the_projects(hx: HX, stub: Stub) -> None:
    """~/.hx/AGENTS.md rides in every request, ahead of the project's AGENTS.md, byte-stable
    across turns."""
    write(hx.hx_home / "AGENTS.md", "USER-RULE: write commit subjects in the imperative.\n")
    write(hx.project / "AGENTS.md", "PROJECT-RULE: run tests with `make check`.\n")
    write(hx.project / "notes.txt", "hello\n")
    stub.script(call("Read", file_path=str(hx.project / "notes.txt")), say("Read it."))
    hx.run("-p", "read notes.txt", check=True)

    first, second = stub.requests[0].system, stub.requests[1].system
    assert first == second
    user = first.index("USER-RULE: write commit subjects")
    project = first.index("PROJECT-RULE: run tests")
    assert user < project
    assert first.index("# User instructions") < user < first.index("# Project instructions")


def test_user_agents_md_is_created_empty_and_never_clobbered(hx: HX, stub: Stub) -> None:
    """The first session leaves an empty ~/.hx/AGENTS.md to fill in, adding nothing to the
    prompt; later sessions load what the user wrote and never rewrite it, even through a
    symlink into a dotfiles repo."""
    user_md = hx.hx_home / "AGENTS.md"
    assert not user_md.exists()
    stub.script(say("Fine."))
    hx.run("-p", "hi", check=True)
    assert user_md.is_file() and user_md.read_text() == ""
    assert "# User instructions" not in stub.requests[0].system

    user_md.write_text("USER-RULE: sign off every answer.\n")
    stub.script(say("Fine."))
    hx.run("-p", "hi", check=True)
    assert user_md.read_text() == "USER-RULE: sign off every answer.\n"
    assert "USER-RULE: sign off every answer." in stub.requests[1].system

    dotfiles = write(hx.home / "dotfiles" / "AGENTS.md", "DOTFILES-RULE: tabs, not spaces.\n")
    user_md.unlink()
    user_md.symlink_to(dotfiles)
    stub.script(say("Fine."))
    hx.run("-p", "hi", check=True)
    assert user_md.is_symlink()
    assert dotfiles.read_text() == "DOTFILES-RULE: tabs, not spaces.\n"
    assert "DOTFILES-RULE: tabs, not spaces." in stub.requests[2].system


def test_blank_unreadable_and_shared_agents_md_are_harmless(hx: HX, stub: Stub) -> None:
    """A blank AGENTS.md adds no empty heading, one that is not UTF-8 or not readable does not
    stop the session, and running from inside $HX_HOME does not load the same file twice."""
    write(hx.project / "AGENTS.md", "  \n\n")
    user_md = hx.hx_home / "AGENTS.md"
    # Saved from a Latin-1 editor: "café" is not valid UTF-8.
    user_md.write_bytes(b"NEVER-SEEN caf\xe9\n")
    stub.script(say("Fine."))
    hx.run("-p", "hi", check=True)
    system = stub.requests[-1].system
    assert "# User instructions" not in system
    assert "# Project instructions" not in system
    assert "NEVER-SEEN" not in system

    # Root reads through any mode bits, so there is nothing to test there.
    if os.geteuid() != 0:
        user_md.write_text("NEVER-SEEN\n")
        user_md.chmod(0o000)
        try:
            stub.script(say("Fine."))
            hx.run("-p", "hi", check=True)
            assert "NEVER-SEEN" not in stub.requests[-1].system
        finally:
            user_md.chmod(0o644)

    user_md.write_text("SHARED-RULE: once only.\n")
    stub.script(say("Fine."))
    hx.run("-p", "hi", cwd=hx.hx_home, check=True)
    system = stub.requests[-1].system
    assert system.count("SHARED-RULE: once only.") == 1
    assert "# User instructions" in system
    assert "# Project instructions" not in system


def test_skill_is_indexed_then_loaded(hx: HX, stub: Stub) -> None:
    """Only a skill's name and description are sent up front; calling Skill loads its body."""
    write(
        hx.project / ".hx" / "skills" / "deploy" / "SKILL.md",
        "---\nname: deploy\ndescription: Tag, build and ship a release\n---\n\n"
        "1. Run the tests.\n2. Tag the commit with SECRET-STEP-MARKER.\n",
    )
    stub.script(call("Skill", name="deploy"), say("Following the deploy skill."))
    hx.run("-p", "ship it", check=True)

    first = json.dumps(stub.requests[0].messages)
    assert "Tag, build and ship a release" in first
    assert "SECRET-STEP-MARKER" not in first
    assert "Skill" in stub.requests[0].tools
    assert "SECRET-STEP-MARKER" in json.dumps(stub.requests[1].messages)


def test_skills_and_agents_commands(hx: HX) -> None:
    """/skills lists installed skills; /agents lists the built-in and project subagents."""
    write(
        hx.project / ".hx" / "skills" / "deploy" / "SKILL.md",
        "---\nname: deploy\ndescription: Tag, build and ship a release\n---\nbody\n",
    )
    write(
        hx.project / ".hx" / "agents" / "reviewer.md",
        "---\nname: reviewer\ndescription: Reviews a diff against conventions\ntools: Read, Grep\n"
        "---\nYou review code.\n",
    )
    term = hx.tui(columns=80, rows=40)
    term.submit("/skills")
    screen = term.wait_for("Tag, build and ship a release")
    assert "deploy" in screen
    term.submit("/agents")
    screen = term.wait_for("Reviews a diff against conventions")
    term.snapshot("/skills and /agents")
    for name in ("explore", "plan", "general", "reviewer"):
        assert name in screen
    assert "tools: Read, Grep" in screen
    # A description too long for the row wraps under itself, not the names.
    lines = screen.splitlines()
    row = next(i for i, line in enumerate(lines) if line.strip().startswith("explore"))
    column = lines[row].index("Read-only search")
    assert lines[row + 1][:column].strip() == "" and lines[row + 1][column] != " ", lines[row + 1]


def test_subagent_runs_in_its_own_context(hx: HX, stub: Stub) -> None:
    """Task runs a subagent with its own prompt, tools and model; only its report returns."""
    write(
        hx.project / ".hx" / "agents" / "reviewer.md",
        f"---\nname: reviewer\ndescription: Reviews code\ntools: Read, Grep\nmodel: {GPT5}\n"
        "---\nYou review code. Be specific.\n",
    )
    (hx.project / "app.py").write_text("print('hi')\n")
    stub.script(
        call("Task", subagent_type="reviewer", prompt="Review app.py", description="review app"),
        # The subagent's own turns: it reads, then reports.
        call("Read", file_path=str(hx.project / "app.py")),
        say("REPORT: app.py prints a greeting; nothing to fix."),
        say("The reviewer found nothing to fix."),
    )
    result = hx.run("-p", "review the app", "--mode", "bypass", check=True)
    assert result.stdout.strip() == "The reviewer found nothing to fix."

    parent, child, child_after_read, parent_after = stub.requests
    assert parent.model == SONNET
    assert child.model == GPT5
    assert child.system.startswith("You review code.")
    assert set(child.tools) == {"Read", "Grep"}, "only the agent's allowlist, and never Task"
    assert child.last_user_text().endswith("Review app.py")
    assert "print('hi')" in child_after_read.tool_results()[0]
    report = parent_after.tool_results()[0]
    assert "REPORT: app.py prints a greeting" in report
    assert "print('hi')" not in json.dumps(parent_after.messages), "the child's reads stay there"


def test_pre_tool_use_hook_blocks(hx: HX, stub: Stub) -> None:
    """A PreToolUse hook that exits 2 refuses the call; its stderr is the reason the model gets."""
    guard = write(
        hx.home / ".hx-guard.sh",
        "#!/bin/sh\ncat > \"$HOOK_LOG\"\necho 'rm is not allowed here' >&2\nexit 2\n",
    )
    guard.chmod(0o755)
    log = hx.root / "hook-input.json"
    write(
        hx.hx_home / "settings.json",
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": str(guard)}]}
                    ]
                }
            }
        ),
    )
    victim = write(hx.project / "keep.txt", "precious\n")
    stub.script(call("Bash", command=f"rm {victim}"), say("The hook stopped me."))
    hx.run("-p", "delete it", "--mode", "bypass", env={"HOOK_LOG": str(log)}, check=True)

    assert victim.exists()
    assert "rm is not allowed here" in stub.requests[1].tool_results()[0]
    event = json.loads(log.read_text())
    assert event["hook_event_name"] == "PreToolUse"
    assert event["tool_name"] == "Bash"
    assert event["tool_input"]["command"] == f"rm {victim}"


def test_post_tool_use_hook_adds_context(hx: HX, stub: Stub) -> None:
    """A PostToolUse hook's additionalContext is appended to the tool result the model reads."""
    checker = write(
        hx.home / ".hx-check.sh",
        '#!/bin/sh\ncat >/dev/null\necho \'{"additionalContext": "lint: line 1 is too long"}\'\n',
    )
    checker.chmod(0o755)
    write(
        hx.hx_home / "settings.json",
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Edit|Write",
                            "hooks": [{"type": "command", "command": str(checker)}],
                        }
                    ]
                }
            }
        ),
    )
    stub.script(
        call("Write", file_path=str(hx.project / "x.py"), content="x = 1\n"), say("Will fix.")
    )
    hx.run("-p", "write x", "--mode", "acceptEdits", check=True)
    assert "lint: line 1 is too long" in stub.requests[1].tool_results()[0]


def test_project_hooks_are_ignored(hx: HX, stub: Stub) -> None:
    """Hooks in the checked-in .hx/settings.json never run: cloning must not execute code."""
    marker = hx.root / "hook-ran"
    write(
        hx.project / ".hx" / "settings.json",
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [{"hooks": [{"type": "command", "command": f"touch {marker}"}]}]
                }
            }
        ),
    )
    stub.script(call("Read", file_path=str(write(hx.project / "a.txt", "a\n"))), say("Read it."))
    hx.run("-p", "read a", check=True)
    assert not marker.exists()

    term = hx.tui()
    term.submit("/hooks")
    screen = term.wait_for("ignored")
    term.snapshot("/hooks lists the refused project hook")
    assert f"touch {marker}" in term.scrollback() or "touch" in screen


def test_settings_allow_rule_skips_the_prompt(hx: HX, stub: Stub) -> None:
    """A project allow rule in .hx/settings.json lets a matching command run headless."""
    write(
        hx.project / ".hx" / "settings.json",
        json.dumps({"permissions": {"allow": ["Bash(touch:*)"]}}),
    )
    stub.script(call("Bash", command="touch allowed.txt"), say("Done."))
    hx.run("-p", "touch it", check=True)
    assert (hx.project / "allowed.txt").exists()


def test_settings_deny_rule_wins_over_bypass(hx: HX, stub: Stub) -> None:
    """A deny rule holds even in bypass mode."""
    write(
        hx.project / ".hx" / "settings.json",
        json.dumps({"permissions": {"deny": ["Bash(touch:*)"]}}),
    )
    stub.script(call("Bash", command="touch denied.txt"), say("Refused."))
    hx.run("-p", "touch it", "--mode", "bypass", check=True)
    assert not (hx.project / "denied.txt").exists()
    assert "not permitted" in stub.requests[1].tool_results()[0]


def test_mcp_tools_are_offered_and_called(hx: HX, stub: Stub) -> None:
    """A configured MCP server's tools arrive namespaced, and a call reaches the real server."""
    hx.run("mcp", "add", "echo", sys.executable, str(ECHO_SERVER), check=True)
    stub.script(call("mcp__echo__echo", message="ping from the model"), say("It echoed."))
    hx.run("-p", "use echo", "--mode", "bypass", check=True)

    assert "mcp__echo__echo" in stub.requests[0].tools
    assert "ping from the model" in stub.requests[1].tool_results()[0]


def test_mcp_status_in_the_tui(hx: HX) -> None:
    """/mcp shows each server and whether it connected; a broken one does not stop the session."""
    hx.run("mcp", "add", "echo", sys.executable, str(ECHO_SERVER), check=True)
    hx.run("mcp", "add", "broken", sys.executable, str(ECHO_SERVER), "--crash", check=True)
    term = hx.tui()
    term.submit("/mcp")
    screen = term.wait_for("broken")
    term.settle()
    term.snapshot("/mcp with one healthy and one broken server")
    assert "echo" in screen
    assert "2 tools" in term.text()


def test_resume_from_the_command_line(hx: HX, stub: Stub) -> None:
    """`hx resume` reopens the last session here, conversation and all."""
    stub.title = "Owl facts"
    stub.script(say("Owls can rotate their heads 270 degrees."))
    hx.run("-p", "tell me about owls", check=True)

    stub.script(say("You asked about owls."))
    term = hx.tui("resume")
    term.wait_for("Owls can rotate their heads 270 degrees.")
    term.snapshot("resumed from the command line")
    term.submit("what did I ask?")
    term.wait_for("You asked about owls.")
    history = json.dumps(stub.requests[-1].messages)
    assert "tell me about owls" in history


def test_trace_command_line(hx: HX, stub: Stub) -> None:
    """`hx trace out.html` writes the last session here to the path given."""
    stub.script(say("Answer for the trace."))
    hx.run("-p", "trace this", check=True)
    out = hx.root / "out.html"
    result = hx.run("trace", str(out), check=True)
    assert out.exists()
    assert "Answer for the trace." in out.read_text()
    assert str(out) in result.stdout + result.stderr


def test_pasted_image_path_is_attached(hx: HX, stub: Stub) -> None:
    """Dragging an image file into the terminal attaches it as [Image #1] and sends it."""
    from tests.e2e.test_print import png

    image = png(hx.project / "mock.png")
    stub.script(say("It is a red rectangle."))
    term = hx.tui()
    term.paste(str(image))
    term.wait_for("[Image #1]")
    term.snapshot("image attached")
    term.type(" what is this?")
    term.press("enter")
    term.wait_for("It is a red rectangle.")
    parts = stub.requests[0].last("user")["content"]
    assert any(p.get("type") == "image_url" for p in parts)
