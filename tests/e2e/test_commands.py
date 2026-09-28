"""Slash commands, the completion popups and the pickers."""

from __future__ import annotations

import re

from tests.e2e.conftest import HX, SANDBOX, SANDBOX_STATUS
from tests.e2e.stub import GPT5, Stub, call, say


def test_slash_opens_command_completion(hx: HX) -> None:
    """Typing `/mo` lists the matching commands above the prompt; tab completes."""
    term = hx.tui()
    term.type("/mo")
    screen = term.wait_for("/model        Choose the model")
    term.snapshot("completion for /mo")
    assert "/mode         Set the permission mode" in screen
    assert "/models" in screen
    assert "List commands and keys" not in screen, "/help does not match"


def test_at_completes_a_path(hx: HX) -> None:
    """`@` completes files and folders in the project."""
    (hx.project / "src").mkdir()
    (hx.project / "src" / "main.py").write_text("x = 1\n")
    term = hx.tui()
    term.type("look at @sr")
    term.wait_for("→ src/")
    term.press("tab")
    term.wait_for("look at @src/")
    term.type("ma")
    term.wait_for("src/main.py")
    term.snapshot("completing a file")


def test_help_lists_commands_and_keys(hx: HX) -> None:
    """/help lists every command and every key, in aligned columns."""
    term = hx.tui(rows=60)
    term.submit("/help")
    screen = term.wait_for("drag an image")
    term.snapshot("/help")
    assert "/rewind" in screen and "/trace" in screen
    rows = [line for line in screen.splitlines() if re.match(r"^\s{5}\S", line)]
    keys = [r for r in rows if not r.strip().startswith("/")]
    # Every description starts in the same column, however long its key.
    columns = [re.search(r"\S+\s{2,}(\S)", row) for row in keys]
    starts = {column.start(1) for column in columns if column}
    assert len(starts) == 1, "\n".join(keys)


def test_cost_and_context(hx: HX, stub: Stub) -> None:
    """/cost breaks down spend; /context shows what fills the window."""
    stub.script(say("Hello there.", prompt_tokens=1200, completion_tokens=40, cost=0.0042))
    term = hx.tui(rows=40)
    term.submit("hi")
    term.wait_for("Hello there.")

    term.submit("/cost")
    screen = term.wait_for("Session cost:")
    assert "$0.0084  over 2 API requests across 1 prompt" in screen

    term.submit("/context")
    screen = term.wait_for("Context:")
    term.snapshot("/cost and /context")
    for section in ("system", "tools", "history"):
        assert re.search(rf"^\s+{section}\s+\S+", screen, re.MULTILINE), section
    assert "✗" not in screen


def test_model_picker_switches_the_model(hx: HX, stub: Stub) -> None:
    """/model lists the catalogue with prices; choosing one routes the next turn to it."""
    stub.script(say("Answered by GPT-5."))
    term = hx.tui()
    term.submit("/model")
    screen = term.wait_for("Select model")
    term.snapshot("model picker")
    assert re.search(r"anthropic/claude-sonnet-4.5\s+200k\s+\$3.00/\$15.00", screen)
    assert re.search(r"openai/gpt-5\s+400k\s+\$1.25/\$10.00", screen)

    term.type("gpt")
    term.wait_for(lambda s: "deepseek" not in s)
    term.press("enter")
    term.wait_for(lambda s: s.splitlines()[-1].rstrip().endswith("gpt-5"))
    term.submit("who are you?")
    term.wait_for("Answered by GPT-5.")
    assert stub.requests[0].model == GPT5


def test_ctrl_p_palette_runs_a_command(hx: HX) -> None:
    """ctrl+p opens the palette; filtering and enter runs the command."""
    term = hx.tui(rows=40)
    term.press("ctrl+p")
    term.wait_for("Filter commands…")
    term.type("perm")
    term.wait_for(lambda s: "/permissions" in s and "/agents" not in s)
    term.press("enter")
    screen = term.wait_for("Permission mode: default")
    term.snapshot("/permissions from the palette")
    assert f"Sandbox: {SANDBOX.value}" in screen


def test_mode_command(hx: HX) -> None:
    """/mode plan changes the mode and the status bar says so."""
    term = hx.tui()
    term.submit("/mode plan")
    term.wait_for("✓ Permission mode: plan")
    assert f"plan · {SANDBOX_STATUS}" in term.lines()[-2]


def test_prompt_command_names_the_agents_md_files(hx: HX) -> None:
    """/prompt shows the running system prompt, then every AGENTS.md in force, user's first."""
    (hx.hx_home / "AGENTS.md").write_text("Sign off every answer.\n")
    (hx.project / "AGENTS.md").write_text("Always answer in haiku.\n")
    term = hx.tui(columns=250, rows=60)
    term.submit("/prompt")
    screen = term.wait_for("Instructions: " + str(hx.project / "AGENTS.md"))
    term.snapshot("/prompt")
    user = screen.index("Instructions: " + str(hx.hx_home / "AGENTS.md"))
    assert screen.index("Source: built-in") < user < screen.index(str(hx.project / "AGENTS.md"))


def test_prompt_command_names_the_agents_md_in_force_not_on_disk(hx: HX) -> None:
    """/prompt lists the AGENTS.md files the session started with; one written, edited or
    emptied since is flagged as applying next run rather than claimed to be in force."""
    (hx.project / "AGENTS.md").write_text("Always answer in haiku.\n")
    term = hx.tui(columns=250, rows=60)
    user_md = hx.hx_home / "AGENTS.md"
    user_md.write_text("Sign off every answer.\n")
    (hx.project / "AGENTS.md").write_text("")
    term.submit("/prompt")
    screen = term.wait_for("applies next run")
    term.snapshot("/prompt after AGENTS.md changed on disk")
    assert "Instructions: " + str(hx.project / "AGENTS.md") in screen
    assert "Instructions: " + str(user_md) not in screen
    assert "An AGENTS.md has changed on disk since startup; it applies next run." in screen


def test_title_command(hx: HX, stub: Stub) -> None:
    """/title shows the model's name for the session, and /title <text> renames it."""
    stub.title = "Greeting exchange"
    stub.script(say("Hello."))
    term = hx.tui()
    term.submit("hi")
    term.wait_for("Hello.")
    term.submit("/title")
    term.wait_for("Session title: Greeting exchange")
    term.submit("/title Release planning")
    term.wait_for("✓ Session title: Release planning")
    term.press("ctrl+d")
    term.wait_exit()
    assert hx.sessions()[-1]["title"] == "Release planning"


def test_copy_puts_the_last_reply_on_the_clipboard(hx: HX, stub: Stub) -> None:
    """/copy copies the last answer - not the command's own echo - and says how."""
    stub.script(say("Copy me, please."))
    term = hx.tui()
    term.submit("hi")
    term.wait_for("Copy me, please.")
    term.submit("/copy")
    term.wait_for("Copied the last reply to the clipboard")
    assert hx.clipboard.read_text() == "Copy me, please."


def test_theme_command(hx: HX) -> None:
    """/theme light switches the palette and names it; an unknown name is refused."""
    term = hx.tui()
    term.submit("/theme light")
    term.wait_for("✓ Theme: light")
    term.snapshot("light theme")
    term.submit("/theme neon")
    term.wait_for("Unknown theme 'neon'")


def test_compact_with_nothing_to_compact(hx: HX, stub: Stub) -> None:
    """/compact on a short session says there is nothing to do - and does not claim to start."""
    stub.script(say("Hello."))
    term = hx.tui()
    term.submit("hi")
    term.wait_for("Hello.")
    term.submit("/compact")
    screen = term.wait_for("Nothing to compact yet.")
    assert "Compacting" not in screen
    assert "2.2k/200k" in screen, "the context gauge is left alone"


def test_compact_summarises_older_turns(hx: HX, stub: Stub) -> None:
    """/compact on a long session asks the model for a summary and replaces older turns with it."""
    replies = [say(f"Answer {n}.") for n in range(1, 7)]
    stub.script(*replies, say("SUMMARY: we discussed six things."), say("Carrying on."))
    term = hx.tui()
    for n in range(1, 7):
        term.submit(f"question {n}")
        term.wait_for(f"Answer {n}.")
    term.submit("/compact focus on the questions")
    screen = term.wait_for("Compacted")
    term.snapshot("after /compact")
    assert "Compacting (/compact)" in term.scrollback()

    summary_request = stub.requests[6]
    assert "focus on the questions" in summary_request.last_user_text()

    term.submit("what next?")
    term.wait_for("Carrying on.")
    after = stub.requests[7]
    assert "SUMMARY: we discussed six things." in str(after.messages)
    assert "question 1" not in str(after.messages)
    gauge = re.search(r"([\d.]+k?)/200k", screen)
    assert gauge and gauge.group(1).endswith("k"), "the gauge still counts the system prompt"


def test_rewind_restores_files_and_the_prompt(hx: HX, stub: Stub) -> None:
    """/rewind goes back to an earlier prompt, undoing the edits made since, and puts that prompt
    back in the editor."""
    target = hx.project / "a.txt"
    target.write_text("original\n")
    stub.script(
        call("Read", file_path=str(target)),
        call("Edit", file_path=str(target), old_string="original", new_string="changed"),
        say("Changed it."),
    )
    term = hx.tui()
    term.submit("change a.txt")
    term.wait_for("Permission needed")
    term.type("y")
    term.wait_for("Changed it.")
    assert target.read_text() == "changed\n"

    term.submit("/rewind")
    term.wait_for("Rewind to")
    term.snapshot("rewind picker")
    term.press("enter")
    screen = term.wait_for("Rewound. 1 file(s) restored.")
    term.snapshot("after rewinding")
    assert target.read_text() == "original\n"
    assert "Changed it." not in screen
    assert "change a.txt" in term.lines()[-5], "the prompt is back in the editor"


def test_clear_and_resume(hx: HX, stub: Stub) -> None:
    """/clear starts a fresh session; /resume lists the old one by title and brings it back."""
    stub.title = "Talking about owls"
    stub.script(say("Owls are nocturnal."))
    term = hx.tui()
    term.submit("tell me about owls")
    term.wait_for("Owls are nocturnal.")

    term.submit("/clear")
    screen = term.wait_for("New session started.")
    assert "Owls are nocturnal." not in screen

    term.submit("/resume")
    screen = term.wait_for("Resume session")
    term.snapshot("resume picker")
    assert "Talking about owls" in screen
    term.press("enter")
    term.wait_for("Owls are nocturnal.")
    term.snapshot("resumed session")


def test_trace_writes_a_self_contained_page(hx: HX, stub: Stub) -> None:
    """/trace writes the session to one HTML file that needs no network to open."""
    stub.script(say("Traced answer."))
    term = hx.tui(columns=140)
    term.submit("trace me")
    term.wait_for("Traced answer.")
    term.submit("/trace")
    term.wait_for("Trace written to")
    (trace,) = hx.hx_home.glob("sessions/*/trace.html")
    page = trace.read_text()
    assert "Traced answer." in page
    assert "trace me" in page
    assert "<script src=" not in page and '<link rel="stylesheet" href="http' not in page


def test_unknown_command(hx: HX) -> None:
    """A mistyped command is reported and not sent to the model."""
    term = hx.tui()
    term.submit("/definitely-not-a-command")
    term.wait_for("definitely-not-a-command")
    term.settle()
    term.snapshot("unknown command")
    assert "Unknown command" in term.text() or "unknown command" in term.text()
