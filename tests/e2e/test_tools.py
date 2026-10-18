"""Tools in the interactive session: approvals, diffs, shell output, modes."""

from __future__ import annotations

import json
import re
import time

from tests.e2e.conftest import HX, SANDBOX_STATUS, git
from tests.e2e.stub import Stub, ToolCall, call, calls, say
from tests.e2e.terminal import Terminal


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


def test_a_denied_command_says_nothing_about_time(hx: HX, stub: Stub) -> None:
    """A denied call never ran, so it has no "Took": the time the user spent deciding
    is not the command's. The allowed call beside it still reports its own run."""
    stub.script(
        call("Bash", "First this.", command="touch denied.txt"),
        call("Bash", "Then this.", command="touch allowed.txt && sleep 1"),
        say("Done."),
    )
    term = hx.tui()
    term.submit("make two files")
    term.wait_for("Permission needed")
    time.sleep(3)  # the user thinks about it
    term.type("n")
    term.wait_for(lambda screen: "Permission needed" in screen and "allowed.txt" in screen)
    time.sleep(3)
    term.type("y")
    term.wait_for("Done.")
    term.settle()
    term.snapshot("one call denied, one allowed")

    assert not (hx.project / "denied.txt").exists()
    assert (hx.project / "allowed.txt").exists()
    scrollback = term.scrollback()
    denied = scrollback[scrollback.index("✗ $ touch denied.txt") : scrollback.index("Then this.")]
    assert "Took" not in denied, denied
    took = [float(seconds) for seconds in re.findall(r"Took ([\d.]+)s", scrollback)]
    assert len(took) == 1 and 1 <= took[0] < 2, took


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


def test_always_allow_covers_a_command_that_cannot_be_decomposed(hx: HX, stub: Stub) -> None:
    """A heredoc whose body has an apostrophe cannot be split into segments, so `a` saves it
    verbatim - and the next session honours that rule instead of asking again."""
    command = "cat <<'EOF'\ndon't ship on fridays\nEOF"
    stub.script(call("Bash", command=command), say("Printed once."))
    term = hx.tui()
    term.submit("print the notes")
    term.wait_for("Permission needed")
    term.type("a")
    term.wait_for("Printed once.")
    term.press("ctrl+d")
    term.wait_exit()

    (settings,) = hx.hx_home.glob("projects/*/settings.local.json")
    assert json.loads(settings.read_text())["permissions"]["allow"] == [f"Bash({command})"]

    stub.script(call("Bash", command=command), say("Printed again."))
    term = hx.tui()
    term.submit("print them again")
    term.wait_for("Printed again.")
    term.snapshot("the saved rule covers the heredoc")
    assert "Permission needed" not in term.scrollback()
    assert stub.requests[-1].tool_results()[-1] == "don't ship on fridays"


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


def test_escape_during_a_tool_answers_every_call(hx: HX, stub: Stub) -> None:
    """esc while a shell command runs stops it; the next question still gets an answer.

    The model asked for two tools at once. The read finished before the
    interrupt and keeps its real result; the command that was cut off is
    answered as interrupted with what it printed, which stays on screen - and
    on a resumed one. The next command runs in the same shell and gets its own
    output, not what was left of the one stopped. A call left
    with no answer at all is a 400 from every upstream - Codex says "No tool
    output found for function call" - and every later turn would fail the same way.
    """
    (hx.project / "notes.txt").write_text("remember the milk\n")
    stub.script(
        calls(
            ToolCall("Read", {"file_path": str(hx.project / "notes.txt")}, id="call_read"),
            ToolCall("Bash", {"command": "printf 'warm%s\\n' up && sleep 30"}, id="call_sleep"),
            text="Reading, then waiting.",
        ),
        call("Bash", "Trying again.", command="echo after"),
        say("Here is the other answer."),
    )
    term = hx.tui()
    term.submit("read and wait")
    term.wait_for("warmup")
    term.press("esc")
    screen = term.wait_for("Interrupted after")
    term.settle()
    term.snapshot("interrupted while the command runs")
    assert "⠋" not in term.text() and "Took" not in screen, "the call is shown stopped"
    assert "warmup" in term.text(), "what it printed before the interrupt stays"

    term.submit("another question")
    term.wait_for("Here is the other answer.")
    term.settle()
    term.snapshot("the next question is answered")

    request = stub.requests[1]
    results = {
        m["tool_call_id"]: str(m["content"]) for m in request.messages if m["role"] == "tool"
    }
    assert "remember the milk" in results["call_read"]
    assert "interrupted" in results["call_sleep"].lower()
    assert "warmup" in results["call_sleep"], "the model hears how far it got"
    assert request.last_user_text().endswith("another question")
    after = stub.requests[2].tool_results()[-1]
    assert "after" in after and "warmup" not in after

    term.close()
    (meta,) = hx.sessions()
    again = hx.tui("resume", meta["session_id"])
    again.wait_for("Here is the other answer.")
    again.settle()
    again.snapshot("resumed: the cut-off call still shows what it printed")
    assert "warmup" in again.text() and "Interrupted" in again.text()


def test_resuming_a_session_killed_mid_tool(hx: HX, stub: Stub) -> None:
    """A session whose process died while a tool ran resumes and carries on.

    Nothing was there to answer the call, so the transcript on disk ends in a
    call without a result - as does every session interrupted before HX
    answered interrupted calls. Resumed, the call reads as interrupted and the
    next question is answered rather than rejected.
    """
    stub.script(
        call("Bash", "Waiting a while.", command="printf 'warm%s\\n' up && sleep 30"),
        say("Picked up where we left off."),
    )
    term = hx.tui()
    term.submit("wait a while")
    term.wait_for("warmup")
    term.kill()
    (meta,) = hx.sessions()

    again = hx.tui("resume", meta["session_id"])
    again.wait_for("Waiting a while.")
    again.settle()
    again.snapshot("resumed: the cut-off call reads as interrupted")
    assert "Interrupted" in again.text()
    assert "⠋" not in again.text()

    again.submit("are you still there?")
    again.wait_for("Picked up where we left off.")
    again.snapshot("the next question is answered")

    (result,) = stub.requests[1].tool_results()
    assert "interrupted" in result.lower()
    assert stub.requests[1].last_user_text().endswith("are you still there?")


def test_counts_of_one_are_singular(hx: HX, stub: Stub) -> None:
    """A tool that found or read one thing says so in the singular, and two stay plural."""
    (hx.project / "one.txt").write_text("TODO: ship\n")
    (hx.project / "two.txt").write_text("first\nsecond\n")
    stub.script(
        calls(
            ToolCall("Read", {"file_path": str(hx.project / "one.txt")}, id="read_one"),
            ToolCall("Read", {"file_path": str(hx.project / "two.txt")}, id="read_two"),
            ToolCall("Grep", {"pattern": "TODO", "path": str(hx.project)}, id="grep"),
            ToolCall("Glob", {"pattern": "one.*", "path": str(hx.project)}, id="glob"),
        ),
        call("Write", file_path=str(hx.project / "note.txt"), content="done\n"),
        say("Counted."),
    )
    term = hx.tui("--mode", "acceptEdits")
    term.submit("count things")
    term.wait_for("Counted.")
    term.settle()
    term.snapshot("singular and plural counts")
    screen = term.scrollback()
    assert "read 1 line" in screen and "read 1 lines" not in screen
    assert "read 2 lines" in screen
    assert "1 result" in screen and "1 results" not in screen
    assert "1 match" in screen and "1 matches" not in screen
    assert stub.requests[2].tool_results()[-1].endswith("(1 line)")


def test_status_bar_keeps_the_project_name_of_a_deep_path(hx: HX, stub: Stub) -> None:
    """A path too long for the status bar loses its start, not its end: the project's own
    name and its branch stay on screen. Before any request, the cache field says so in words."""
    deep = hx.root / "clients" / "acme-corporation" / "platform" / "services" / "billing-recon"
    deep.mkdir(parents=True)
    git(deep, "init", "-q", "-b", "feature/invoices")
    term = hx.tui(cwd=deep)
    term.snapshot("status bar for a deep project")
    location, stats = term.text().rstrip("\n").splitlines()[-2:]
    assert location.strip().startswith("…/"), location
    assert "/billing-recon (feature/invoices)" in location
    assert location.rstrip().endswith(f"default · {SANDBOX_STATUS}")
    assert "no cache" in stats and "cache -" not in stats


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


def test_glob_on_a_bare_directory_pattern_is_a_usable_error(hx: HX, stub: Stub) -> None:
    """A pattern that names only a directory (".", "./") or an absolute path is answered
    with what to use instead, the same on every Python, rather than a traceback or
    pathlib's own wording; the model recovers with a real pattern."""
    (hx.project / "main.py").write_text("print(1)\n")
    stub.script(
        call("Glob", pattern="."),
        call("Glob", pattern="./"),
        call("Glob", pattern=f"{hx.project}/*.py"),
        call("Glob", pattern="*"),
        say("One file: main.py."),
    )
    result = hx.run("-p", "what files are here?", check=True)

    assert result.stdout == "One file: main.py.\n"
    dot, dot_slash, absolute, real = (stub.requests[i].tool_results()[-1] for i in (1, 2, 3, 4))
    for answer in (dot, dot_slash):
        assert "it names a directory, not files. Use '*' or '**/*'." in answer
    assert (
        f"bad pattern '{hx.project}/*.py': it is an absolute path. Pass the directory as "
        f"'path' and a relative pattern, such as path='{hx.project}', pattern='*.py'."
    ) in absolute
    for answer in (dot, dot_slash, absolute):
        assert "Error" not in answer and "Unacceptable" not in answer
    assert "main.py" in real


WORKING = re.compile(r"([⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]) Working… \((\d+)s")


def _working(screen: str) -> tuple[str, int] | None:
    """The spinner frame and the seconds the working status shows, if it is shown."""
    found = WORKING.search(screen)
    return (found[1], int(found[2])) if found else None


def _watch_working(term: Terminal, seconds: float) -> list[tuple[str, int]]:
    """What the working status showed, sampled for ``seconds``."""
    seen: list[tuple[str, int]] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if (state := _working(term.text())) is not None:
            seen.append(state)
        time.sleep(0.03)
    return seen


def test_working_status_animates_while_tools_run(hx: HX, stub: Stub) -> None:
    """The working status moves for the whole turn: through each tool the model runs,
    and through the command after an approval. It counts the turn's time, not the time
    since the last model call. While the approval is open nothing spins, and the held
    call's "Took" is its own run, not the user's time to decide. The status goes when
    the turn ends, and a `!` command approved afterwards does not bring it back."""
    stub.script(
        call("Bash", "Looking first.", command="printf 'phase%s\\n' one && sleep 3"),
        call(
            "Bash",
            "Now the change.",
            command="touch made.txt && printf 'phase%s\\n' two && sleep 3",
        ),
        say("All done."),
    )
    term = hx.tui()
    term.submit("look, then change it")

    term.wait_for("phaseone")
    term.snapshot("a read-only command runs")
    during_read = _watch_working(term, 1.5)
    assert during_read, "the working status is shown while the command runs"
    assert len({frame for frame, _ in during_read}) >= 3, "and its spinner moves"
    assert during_read[-1][1] >= 1, "and its clock counts"

    screen = term.wait_for("Permission needed")
    assert "? $ touch made.txt" in screen, "the held call is shown waiting, not running"
    # Nothing is animating, so HX goes quiet - a spinner would keep painting.
    started = time.monotonic()
    term.settle(quiet=0.5, timeout=3)
    assert time.monotonic() - started < 1.5, "HX keeps writing while nothing moves"
    term.snapshot("an approval holds the turn")
    assert _working(term.text()) is None, "nothing claims to be working while it waits"
    still = term.text()
    time.sleep(4)  # the user thinks about it
    assert term.text() == still

    term.type("y")
    term.wait_for("phasetwo")
    term.snapshot("the approved command runs")
    during_change = _watch_working(term, 1.5)
    assert during_change, "the working status comes back once the approval is answered"
    assert len({frame for frame, _ in during_change}) >= 3, "and its spinner moves"
    assert during_change[0][1] >= 7, "the clock is the turn's, not reset by each model call"

    term.wait_for("All done.")
    term.settle()
    term.snapshot("the turn is over")
    screen = term.text()
    assert _working(screen) is None
    assert (hx.project / "made.txt").exists()
    took = [float(seconds) for seconds in re.findall(r"Took ([\d.]+)s", term.scrollback())]
    assert len(took) == 2 and all(3 <= seconds < 4 for seconds in took), took

    term.submit("!touch made-by-bang.txt")
    term.wait_for("Permission needed")
    term.type("y")
    term.wait_for("allowed once")
    term.settle(quiet=0.5)
    term.snapshot("after an approved ! command")
    assert "Working…" not in term.text(), "a shell line is not a turn"
