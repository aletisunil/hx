"""Tools in the interactive session: approvals, diffs, shell output, modes."""

from __future__ import annotations

import json

from tests.e2e.conftest import HX
from tests.e2e.stub import Stub, call, say


def test_write_asks_and_allow_once(hx: HX, stub: Stub) -> None:
    """A write stops at an approval that shows the content; `y` allows it once and it runs."""
    target = hx.project / "notes.txt"
    stub.script(
        call("Write", "I'll write the notes.", file_path=str(target), content="one\ntwo\n"),
        say("Wrote the notes."),
    )
    term = hx.tui()
    term.submit("write notes")
    term.wait_for("Permission needed")
    term.settle()
    term.snapshot("approval for a write")
    screen = term.text()
    assert "allow once" in screen
    assert "+    1 one" in screen
    assert not target.exists(), "nothing happens before the answer"

    term.type("y")
    term.wait_for("Wrote the notes.")
    term.settle()
    term.snapshot("after allowing once")
    assert target.read_text() == "one\ntwo\n"
    assert "✓ allowed once · notes.txt" in term.text()


def test_deny_tells_the_model(hx: HX, stub: Stub) -> None:
    """`n` refuses the call; the model hears it was denied and nothing is written."""
    target = hx.project / "notes.txt"
    stub.script(
        call("Write", file_path=str(target), content="x\n"), say("Understood, not writing.")
    )
    term = hx.tui()
    term.submit("write notes")
    term.wait_for("Permission needed")
    term.type("n")
    term.wait_for("Understood, not writing.")
    term.settle()
    term.snapshot("after denying")

    assert not target.exists()
    assert "✗ denied · notes.txt" in term.text()
    assert stub.requests[1].tool_results()[0] == "Write was not permitted: the user declined"


def test_always_allow_is_remembered(hx: HX, stub: Stub) -> None:
    """`a` saves a rule under ~/.hx for this project, so the next session does not ask again."""
    target = hx.project / "notes.txt"
    stub.script(call("Write", file_path=str(target), content="first\n"), say("Done once."))
    term = hx.tui()
    term.submit("write it")
    assert "saves a rule for this project in ~/.hx" in term.wait_for("Permission needed")
    term.type("a")
    term.wait_for("Done once.")
    term.press("ctrl+d")
    term.wait_exit()

    (settings,) = hx.hx_home.glob("projects/*/settings.local.json")
    rules = json.loads(settings.read_text())["permissions"]["allow"]
    assert any(rule.startswith("Write(") for rule in rules), rules
    assert not (hx.project / ".hx" / "settings.local.json").exists(), "never in the checkout"

    stub.script(
        call("Read", file_path=str(target)),
        call("Write", file_path=str(target), content="second\n"),
        say("Done again."),
    )
    term = hx.tui()
    term.submit("write it again")
    term.wait_for("Done again.")
    assert "Permission needed" not in term.scrollback()
    assert target.read_text() == "second\n"


def test_edit_shows_a_diff(hx: HX, stub: Stub) -> None:
    """An edit is approved against a diff of the change, and the transcript keeps the diff."""
    source = hx.project / "app.py"
    source.write_text("def greet():\n    return 'hello'\n")
    stub.script(
        call("Read", file_path=str(source)),
        call("Edit", file_path=str(source), old_string="'hello'", new_string="'hello, world'"),
        say("Updated."),
    )
    term = hx.tui()
    term.submit("make it greet the world")
    term.wait_for("Permission needed")
    term.settle()
    term.snapshot("approval for an edit")
    screen = term.text()
    assert "-    2     return 'hello'" in screen
    assert "+    2     return 'hello, world'" in screen

    term.press("enter")  # the highlighted choice: allow once
    term.wait_for("Updated.")
    assert source.read_text() == "def greet():\n    return 'hello, world'\n"


def test_bash_output_in_the_transcript(hx: HX, stub: Stub) -> None:
    """A shell command that changes things asks first; its output is shown under the command,
    a long one collapsed until ctrl+o expands it."""
    stub.script(call("Bash", command="touch made.txt && seq 1 40"), say("Counted."))
    term = hx.tui()
    term.submit("count to forty")
    term.wait_for("Permission needed")
    assert "$ touch made.txt && seq 1 40" in term.text()
    term.type("y")
    term.wait_for("Counted.")
    term.snapshot("collapsed shell output")
    assert (hx.project / "made.txt").exists()
    assert "40" in stub.requests[1].tool_results()[0]
    collapsed = term.text()
    # Collapsed to its tail: a command's answer is at the end of it.
    assert "… (32 more lines, ctrl+o to expand)" in collapsed
    assert "\n   33\n" in collapsed and "\n   40\n" in collapsed
    assert "\n   1\n" not in collapsed

    term.press("ctrl+o")
    term.wait_for(lambda screen: "more lines" not in screen)
    term.snapshot("expanded shell output")
    expanded = term.scrollback()
    assert "\n   1\n" in expanded and "\n   40\n" in expanded


def test_read_only_commands_do_not_ask(hx: HX, stub: Stub) -> None:
    """Reads, searches and read-only shell commands run without an approval."""
    (hx.project / "a.py").write_text("TODO = 1\n")
    stub.script(
        call("Grep", pattern="TODO", path=str(hx.project)),
        call("Bash", command="ls"),
        say("Looked around."),
    )
    term = hx.tui()
    term.submit("look around")
    term.wait_for("Looked around.")
    assert "Permission needed" not in term.scrollback()
    assert "a.py" in stub.requests[1].tool_results()[0]
    assert "a.py" in stub.requests[2].tool_results()[0]


def test_shift_tab_cycles_the_mode(hx: HX, stub: Stub) -> None:
    """shift+tab moves through the permission modes and the status bar follows."""
    term = hx.tui()
    seen = []
    for _ in range(4):
        term.press("shift+tab")
        term.settle()
        status = term.lines()[-2]
        seen.append(status)
    term.snapshot("after cycling modes")
    joined = "\n".join(seen)
    for mode in ("acceptEdits", "plan", "bypass"):
        assert mode in joined, joined


def test_accept_edits_writes_without_asking(hx: HX, stub: Stub) -> None:
    """In acceptEdits, file edits go through without an approval."""
    target = hx.project / "out.txt"
    stub.script(call("Write", file_path=str(target), content="ok\n"), say("Written."))
    term = hx.tui("--mode", "acceptEdits")
    assert "acceptEdits" in term.text()
    term.submit("write")
    term.wait_for("Written.")
    assert target.read_text() == "ok\n"
    assert "Permission needed" not in term.scrollback()


def test_bang_runs_a_shell_command_directly(hx: HX, stub: Stub) -> None:
    """`!command` runs in the shell without a model turn - through the same approvals and
    sandbox as the model's own commands."""
    term = hx.tui()
    term.submit("!echo straight-to-the-shell")
    term.wait_for("$ echo straight-to-the-shell")
    term.wait_for(lambda s: s.count("straight-to-the-shell") >= 3)
    term.snapshot("after !echo")

    term.submit("!touch made-by-bang.txt")
    term.wait_for("Permission needed")
    term.type("y")
    term.wait_for("allowed once")
    term.settle()
    assert (hx.project / "made-by-bang.txt").exists()
    assert stub.requests == []


def test_todo_list_is_shown(hx: HX, stub: Stub) -> None:
    """A plan the model writes with TodoWrite is drawn, and ctrl+t shows it again."""
    todos = [
        {"content": "Read the code", "status": "completed", "activeForm": "Reading the code"},
        {"content": "Fix the bug", "status": "in_progress", "activeForm": "Fixing the bug"},
        {"content": "Run the tests", "status": "pending", "activeForm": "Running the tests"},
    ]
    stub.script(call("TodoWrite", todos=todos), say("Plan made."))
    term = hx.tui()
    term.submit("plan it")
    term.wait_for("Plan made.")
    term.settle()
    term.snapshot("plan in the transcript")
    for item in ("Read the code", "Fix the bug", "Run the tests"):
        assert item in term.scrollback()
    assert "Run the tests" in stub.requests[1].system + json.dumps(stub.requests[1].messages)
