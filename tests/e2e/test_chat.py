"""The interactive session: typing, answers, the dock, interrupting, leaving."""

from __future__ import annotations

import threading

from tests.e2e.conftest import HX, SANDBOX_STATUS
from tests.e2e.stub import Reply, Stub, say


def test_first_screen(hx: HX) -> None:
    """Startup shows the banner, an empty prompt docked at the bottom, and the status bar."""
    term = hx.tui()
    screen = term.lines()
    term.snapshot("first screen")

    assert screen[0].startswith(" hx v")
    assert "Ask HX…" in term.text()
    assert f"default · {SANDBOX_STATUS}" in term.text()
    assert screen[-1].rstrip().endswith("claude-sonnet-4.5")
    # The dock sits on the last rows: the prompt, its rules, hints, then two status lines.
    assert "Ask HX…" in screen[-5]


def test_conversation_turn(hx: HX, stub: Stub) -> None:
    """A question and its answer land in the transcript; the status bar counts tokens and cost."""
    stub.script(say("Paris is the capital of France.", prompt_tokens=1500, cost=0.0123))
    term = hx.tui()
    term.submit("What is the capital of France?")
    term.wait_for("Paris is the capital of France.")
    term.settle()
    term.snapshot("after one exchange")

    screen = term.text()
    assert "What is the capital of France?" in screen
    assert "↑1.8k" in screen, "tokens in: the answer plus the naming call"
    assert "$0.02" in screen
    (request,) = stub.requests
    assert request.last_user_text().endswith("What is the capital of France?")


def test_history_is_sent_on_the_next_turn(hx: HX, stub: Stub) -> None:
    """The second turn carries the first exchange, so the model has the conversation."""
    stub.script(say("Nice to meet you, Ada."), say("Your name is Ada."))
    term = hx.tui()
    term.submit("My name is Ada.")
    term.wait_for("Nice to meet you, Ada.")
    term.submit("What is my name?")
    term.wait_for("Your name is Ada.")

    second = stub.requests[1]
    roles = [m["role"] for m in second.messages]
    assert roles[0] == "system"
    assert roles[-3:] == ["user", "assistant", "user"]
    assert "Nice to meet you, Ada." in str(second.messages[-2]["content"])


def test_markdown_is_rendered(hx: HX, stub: Stub) -> None:
    """Headings, lists and code fences are drawn, not shown as raw markdown."""
    stub.script(say("## Steps\n\n1. Install it\n2. Run **hx**\n\n```python\nprint('hi')\n```\n"))
    term = hx.tui()
    term.submit("how?")
    term.wait_for("print('hi')")
    term.settle()
    term.snapshot("rendered markdown")

    screen = term.text()
    assert "Steps" in screen
    assert "## Steps" not in screen
    assert "**hx**" not in screen
    assert "```" not in screen
    assert "1. Install it" in screen


def test_long_answer_scrolls_into_scrollback(hx: HX, stub: Stub) -> None:
    """An answer taller than the window goes to the terminal's scrollback, not lost."""
    lines = "\n\n".join(f"Paragraph number {n}." for n in range(1, 41))
    stub.script(say(lines))
    term = hx.tui(rows=24)
    term.submit("write a lot")
    term.wait_for("Paragraph number 40.")
    term.settle()

    history = term.scrollback()
    for n in (1, 20, 40):
        assert f"Paragraph number {n}." in history
    assert "Ask HX…" in term.lines()[-5]


def test_escape_interrupts_a_running_turn(hx: HX, stub: Stub) -> None:
    """esc stops the model mid-answer; the partial answer stays, and the next turn works."""
    hold = threading.Event()
    stub.script(
        Reply(text="Counting slowly: one, two, three, four", hold=hold, hold_after=16),
        say("Fresh answer."),
    )
    term = hx.tui()
    term.submit("count")
    term.wait_for("Counting slowly")
    term.press("esc", "esc", "esc")
    term.wait_for("Interrupted")
    term.settle()
    term.snapshot("after esc")
    assert term.text().count("Interrupted") == 1, "said once, however often esc is pressed"
    hold.set()

    assert "three, four" not in term.text()
    term.submit("again")
    term.wait_for("Fresh answer.")


def test_typing_while_busy_queues_the_message(hx: HX, stub: Stub) -> None:
    """enter while a turn runs queues the message; it is sent when the turn ends."""
    hold = threading.Event()
    stub.script(
        Reply(text="Working on the first thing.", hold=hold, hold_after=10),
        say("Now the second thing."),
    )
    term = hx.tui()
    term.submit("first")
    term.wait_for("Working on")
    term.submit("second")
    term.wait_for("second")
    term.settle()
    term.snapshot("second message queued while the first runs")
    assert len(stub.requests) == 1

    hold.set()
    term.wait_for("Now the second thing.")
    assert stub.requests[1].last_user_text().endswith("second")


def test_alt_enter_steers_the_running_turn(hx: HX, stub: Stub) -> None:
    """alt+enter cuts the running answer off and the model continues from the new message."""
    hold = threading.Event()
    stub.script(
        Reply(text="Writing it in Java, starting with", hold=hold, hold_after=17),
        say("Switching to Python."),
    )
    term = hx.tui()
    term.submit("write a script")
    term.wait_for("Writing it in Ja")
    term.type("use python instead")
    term.press("alt+enter")
    term.wait_for("Switching to Python.")
    term.settle()
    term.snapshot("after steering")
    hold.set()

    steered = stub.requests[1]
    assert "use python instead" in steered.last_user_text()


def test_ctrl_c_clears_the_prompt(hx: HX) -> None:
    """ctrl+c on a typed prompt clears it rather than exiting."""
    term = hx.tui()
    term.type("half a thought")
    term.wait_for("half a thought")
    term.press("ctrl+c")
    term.wait_gone("half a thought")
    assert term.alive


def test_ctrl_d_exits_and_restores_the_terminal(hx: HX, stub: Stub) -> None:
    """ctrl+d on an empty prompt exits 0 and takes the interface off the screen."""
    stub.script(say("Bye for now."))
    term = hx.tui()
    term.submit("hello")
    term.wait_for("Bye for now.")
    term.press("ctrl+d")
    assert term.wait_exit() == 0
    screen = term.text()
    term.snapshot("after exit")
    assert "Ask HX…" not in screen
    assert "Bye for now." not in screen
    assert b"\x1b[?25h" in term.raw, "the cursor is shown again"
    assert term.raw.rfind(b"\x1b[?2004l") > term.raw.rfind(b"\x1b[?2004h"), "bracketed paste off"
    assert term.kitty_stacks == {"main": [], "alt": []}, "keyboard protocol popped"


def test_quit_command(hx: HX) -> None:
    """/quit leaves the same way ctrl+d does."""
    term = hx.tui()
    term.submit("/quit")
    assert term.wait_exit() == 0


def test_resize_reflows_the_screen(hx: HX, stub: Stub) -> None:
    """Narrowing the window rewraps the dock and status bar to the new width."""
    stub.script(say("A sentence that is long enough to need wrapping once the window gets narrow."))
    term = hx.tui(columns=100)
    term.submit("hi")
    term.wait_for("wrapping once")
    term.resize(60, 30)
    term.settle(0.5)
    term.snapshot("after narrowing to 60 columns")

    lines = term.lines()
    assert all(len(line) <= 60 for line in lines)
    assert "Ask HX…" in "\n".join(lines[-6:])
    assert lines[-1].rstrip().endswith("claude-sonnet-4.5")


def test_provider_error_is_shown(hx: HX, stub: Stub) -> None:
    """A refused request is reported in the transcript and the session carries on."""
    stub.script(Reply(status=402, error="Insufficient credits"), say("Back again."))
    term = hx.tui()
    term.submit("hello")
    term.wait_for("Insufficient credits")
    term.settle()
    term.snapshot("error in the transcript")
    term.submit("retry")
    term.wait_for("Back again.")
