"""The document: a transcript that is only appended to, and the dock below it."""

from __future__ import annotations

import pyte
import pytest

from hx.term.screen import MainScreen
from hx.term.terminal import FakeTerminal
from hx.term.width import strip_ansi
from hx.tui import paint
from hx.tui.views.blocks import AssistantMessage, Notice, UserMessage
from hx.tui.views.transcript import Dock, Session, StubPrompt, Transcript, WorkingRule
from tests.term.conftest import assert_lines_fit, plain


@pytest.fixture(autouse=True)
def _pinned_colors() -> None:
    paint.set_color_mode("truecolor")


def test_blocks_are_separated_by_exactly_one_blank_line() -> None:
    transcript = Transcript()
    transcript.append(Notice("one"))
    transcript.append(Notice("two"))
    assert plain(transcript.render(40)) == [" · one", "", " · two"]


def test_the_first_block_brings_no_leading_blank() -> None:
    transcript = Transcript()
    transcript.append(Notice("only"))
    assert plain(transcript.render(40)) == [" · only"]


def test_the_transcript_reports_its_blocks_without_the_spacers() -> None:
    transcript = Transcript()
    first = transcript.append(Notice("one"))
    second = transcript.append(Notice("two"))
    assert transcript.blocks == [first, second]
    assert transcript.last() is second


# -- the working rule -------------------------------------------------------


def test_the_prompt_is_framed_whether_or_not_a_turn_is_running() -> None:
    """A turn starting must cost no layout, or the transcript jumps when the
    spinner appears."""
    rule = WorkingRule()
    idle = rule.render(60)
    rule.start()
    rule.tick(1.0)
    assert len(rule.render(60)) == len(idle) == 1


def test_the_working_status_lives_in_the_rule() -> None:
    rule = WorkingRule()
    rule.start()
    rule.tick(12.0)
    drawn = strip_ansi(rule.render(70)[0])
    assert "Working…" in drawn
    assert "12s" in drawn
    assert "to interrupt" in drawn
    assert drawn.startswith("──")


def test_a_stopped_rule_is_just_a_rule() -> None:
    rule = WorkingRule()
    rule.start()
    rule.tick(1.0)
    rule.stop()
    assert set(strip_ansi(rule.render(40)[0])) == {"─"}


def test_a_finished_turn_does_not_keep_spinning() -> None:
    rule = WorkingRule()
    rule.stop()
    before = rule.render(40)
    rule.tick(5.0)
    assert rule.render(40) == before


# -- the stub prompt --------------------------------------------------------


def test_typing_accumulates_and_backspace_removes() -> None:
    prompt = StubPrompt()
    prompt.handle_input("text", "hel")
    prompt.handle_input("text", "lo")
    assert prompt.value == "hello"
    prompt.handle_input("backspace", "")
    assert prompt.value == "hell"


def test_an_empty_prompt_shows_its_placeholder() -> None:
    assert "Ask HX…" in strip_ansi(StubPrompt().render(40)[0])


def test_a_paste_does_not_break_the_single_line() -> None:
    """The stub is one line; a newline in a paste would make it two and shift
    the whole dock."""
    prompt = StubPrompt()
    prompt.handle_input("paste", "one\ntwo")
    assert len(prompt.render(60)) == 1
    assert "\n" not in prompt.value


def test_the_prompt_marks_where_the_hardware_cursor_belongs() -> None:
    from hx.term.screen import CURSOR_MARKER

    prompt = StubPrompt()
    prompt.handle_input("text", "hi")
    assert CURSOR_MARKER in prompt.render(40)[0]


def test_unknown_keys_are_passed_on() -> None:
    """So a binding the prompt does not own still reaches whatever does."""
    assert StubPrompt().handle_input("ctrl+p", "") is False


# -- the whole document -----------------------------------------------------


def test_the_document_is_header_then_transcript_then_dock() -> None:
    session = Session("1.2.3")
    session.transcript.append(UserMessage("ask"))
    session.transcript.append(AssistantMessage("answer"))

    lines = plain(session.render(60))
    text = [line for line in lines if line.strip()]
    order = [
        text.index(line)
        for line in text
        if "hx v1.2.3" in line or "ask" in line or "answer" in line
    ]
    assert order == sorted(order)

    rules = [index for index, line in enumerate(lines) if set(line.strip()) == {"─"}]
    answer = next(index for index, line in enumerate(lines) if "answer" in line)
    assert all(index > answer for index in rules), "the dock drew above the transcript"


def test_the_dock_is_the_tail_of_the_document() -> None:
    """Keeping it last is what makes a redraw of the live parts touch only the
    end, rather than everything."""
    session = Session("1.2.3")
    lines = plain(session.render(60))
    assert "no model" in lines[-1]


def test_the_session_routes_keys_to_the_prompt() -> None:
    session = Session("1.2.3")
    session.handle_input("text", "typed")
    assert session.dock.prompt.value == "typed"


def test_appending_only_changes_the_tail_of_the_screen() -> None:
    """The point of the renderer: finished lines become the terminal's own
    scrollback and are never rewritten."""
    terminal = FakeTerminal(60, 24)
    session = Session("1.2.3")
    screen = MainScreen(terminal, session)

    session.transcript.append(AssistantMessage("the first answer"))
    screen.render()
    terminal.clear_output()

    session.transcript.append(AssistantMessage("the second answer"))
    screen.render()

    assert "the first answer" not in terminal.output
    assert "the second answer" in terminal.output
    assert "\x1b[2J" not in terminal.output


def test_a_streaming_reply_does_not_repaint_the_whole_screen() -> None:
    terminal = FakeTerminal(60, 24)
    session = Session("1.2.3")
    screen = MainScreen(terminal, session)
    session.transcript.append(UserMessage("a question"))
    reply = AssistantMessage("Start")
    session.transcript.append(reply)
    screen.render()
    terminal.clear_output()

    reply.append(" more")
    screen.render()
    assert "a question" not in terminal.output
    assert "\x1b[2J" not in terminal.output


def test_what_a_terminal_would_show_for_a_short_session() -> None:
    terminal = FakeTerminal(60, 24)
    session = Session("1.2.3")
    screen = MainScreen(terminal, session)
    session.transcript.append(UserMessage("hello"))
    session.transcript.append(AssistantMessage("hi there"))
    screen.render()

    vt = pyte.Screen(60, 24)
    pyte.Stream(vt).feed(terminal.output)
    shown = [line.rstrip() for line in vt.display if line.strip()]
    assert shown[0] == " hx v1.2.3"
    assert " hello" in shown
    assert " hi there" in shown
    assert shown.index(" hello") < shown.index(" hi there")


@pytest.mark.parametrize("width", [24, 40, 80, 160])
def test_the_document_honours_the_renderer_contract(width: int) -> None:
    session = Session("1.2.3")
    session.transcript.append(UserMessage("a question with 日本語 and 👨‍👩‍👧‍👦"))
    session.transcript.append(AssistantMessage("## Answer\n\n- one\n- two"))
    session.dock.working.start()
    session.dock.working.tick(3.0)
    session.dock.hints.set_hints([("esc", "interrupt"), ("ctrl+c", "clear")])
    assert_lines_fit(session, width)


def test_the_dock_keeps_its_height_whatever_happens() -> None:
    """A dock that grows or shrinks pushes the transcript around under it."""
    dock = Dock()
    idle = len(dock.render(60))
    dock.working.start()
    dock.working.tick(9.0)
    dock.hints.set_hints([("esc", "interrupt")])
    assert len(dock.render(60)) == idle
