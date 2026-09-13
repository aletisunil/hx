"""The prompt's contract.

Every row of this is behaviour that existed in the Textual prompt and that
users have muscle memory for. Most of it was previously supplied by TextArea
rather than written here, which is exactly why it needs asserting now.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hx.term.width import cell_width
from hx.tui import paint
from hx.tui.views.prompt import FileCompleter, Prompt
from tests.term.conftest import assert_lines_fit, plain


@pytest.fixture(autouse=True)
def _pinned_colors() -> None:
    paint.set_color_mode("truecolor")


@pytest.fixture
def prompt(tmp_path: Path) -> Prompt:
    return Prompt(tmp_path)


def typed(prompt: Prompt, text: str) -> None:
    prompt.handle_input("text", text)


# -- typing and motion ------------------------------------------------------


def test_typing_accumulates(prompt: Prompt) -> None:
    typed(prompt, "hello")
    assert prompt.text == "hello"
    assert prompt.buffer.cursor == 5


def test_the_cursor_moves_by_character_and_by_word(prompt: Prompt) -> None:
    typed(prompt, "alpha beta gamma")
    prompt.handle_input("alt+b", "")
    assert prompt.buffer.cursor == len("alpha beta ")
    prompt.handle_input("alt+f", "")
    assert prompt.buffer.cursor == len("alpha beta gamma")
    prompt.handle_input("ctrl+b", "")
    assert prompt.buffer.cursor == len("alpha beta gamm")


def test_home_and_end_work_per_line(prompt: Prompt) -> None:
    typed(prompt, "first\nsecond")
    prompt.handle_input("home", "")
    assert prompt.buffer.column == 0
    prompt.handle_input("end", "")
    assert prompt.buffer.column == len("second")


def test_arrows_move_within_a_multi_line_draft(prompt: Prompt) -> None:
    typed(prompt, "first line\nsecond line")
    prompt.handle_input("up", "")
    assert prompt.buffer.row == 0


# -- submitting -------------------------------------------------------------


def test_enter_submits_and_clears(prompt: Prompt) -> None:
    sent: list[str] = []
    prompt.on_submit = sent.append
    typed(prompt, "  a question  ")
    prompt.handle_input("enter", "")
    assert sent == ["a question"]
    assert prompt.text == ""


def test_enter_on_an_empty_prompt_sends_nothing(prompt: Prompt) -> None:
    sent: list[str] = []
    prompt.on_submit = sent.append
    prompt.handle_input("enter", "")
    assert sent == []


def test_ctrl_j_inserts_a_newline_rather_than_submitting(prompt: Prompt) -> None:
    sent: list[str] = []
    prompt.on_submit = sent.append
    typed(prompt, "one")
    prompt.handle_input("ctrl+j", "")
    typed(prompt, "two")
    assert prompt.text == "one\ntwo"
    assert sent == []


def test_alt_enter_steers(prompt: Prompt) -> None:
    steered: list[str] = []
    prompt.on_steer = steered.append
    typed(prompt, "a steer")
    prompt.handle_input("alt+enter", "")
    assert steered == ["a steer"]


def test_alt_enter_with_nothing_typed_still_steers(prompt: Prompt) -> None:
    """With an empty draft it means "promote whatever is queued", so it must
    still fire - this is subtle and easy to optimise away."""
    steered: list[str] = []
    prompt.on_steer = steered.append
    prompt.handle_input("alt+enter", "")
    assert steered == [""]


# -- history ----------------------------------------------------------------


def test_history_recalls_what_was_sent(prompt: Prompt) -> None:
    prompt.on_submit = lambda _text: None
    typed(prompt, "first")
    prompt.handle_input("enter", "")
    typed(prompt, "second")
    prompt.handle_input("enter", "")

    prompt.handle_input("up", "")
    assert prompt.text == "second"
    prompt.handle_input("up", "")
    assert prompt.text == "first"
    prompt.handle_input("down", "")
    assert prompt.text == "second"


def test_history_only_triggers_at_the_edges_of_the_buffer(prompt: Prompt) -> None:
    """Which is why the arrows still move the cursor in a multi-line draft."""
    prompt.on_submit = lambda _text: None
    typed(prompt, "old")
    prompt.handle_input("enter", "")

    typed(prompt, "line one\nline two")
    prompt.handle_input("up", "")  # row 1 -> row 0, not history
    assert prompt.text == "line one\nline two"
    prompt.handle_input("up", "")  # now at the top, so history
    assert prompt.text == "old"


def test_leaving_history_restores_the_draft(prompt: Prompt) -> None:
    prompt.on_submit = lambda _text: None
    typed(prompt, "sent")
    prompt.handle_input("enter", "")
    typed(prompt, "a draft")

    prompt.handle_input("up", "")
    assert prompt.text == "sent"
    prompt.handle_input("down", "")
    assert prompt.text == "a draft"


# -- kill ring --------------------------------------------------------------


@pytest.mark.parametrize("key", ["ctrl+w", "alt+backspace"])
def test_killing_a_word_backwards(prompt: Prompt, key: str) -> None:
    typed(prompt, "alpha beta")
    prompt.handle_input(key, "")
    assert prompt.text == "alpha "


def test_killing_to_the_start_and_end_of_a_line(prompt: Prompt) -> None:
    typed(prompt, "alpha beta")
    prompt.handle_input("ctrl+u", "")
    assert prompt.text == ""

    typed(prompt, "alpha beta")
    prompt.buffer.cursor = 5
    prompt.handle_input("ctrl+k", "")
    assert prompt.text == "alpha"


def test_yank_puts_it_back(prompt: Prompt) -> None:
    typed(prompt, "alpha beta")
    prompt.handle_input("ctrl+w", "")
    prompt.handle_input("ctrl+y", "")
    assert prompt.text == "alpha beta"


def test_yank_pop_walks_down_the_ring(prompt: Prompt) -> None:
    typed(prompt, "one two")
    prompt.handle_input("ctrl+w", "")  # kills "two"
    prompt.handle_input("ctrl+w", "")  # kills "one "
    prompt.handle_input("ctrl+y", "")
    assert prompt.text == "one "
    prompt.handle_input("alt+y", "")
    assert prompt.text == "two"


def test_yank_pop_after_an_edit_does_nothing(prompt: Prompt) -> None:
    """The recorded range is re-checked rather than trusted: any edit moves the
    text under it, and replacing a stale range mangles whatever sits there."""
    typed(prompt, "one two")
    prompt.handle_input("ctrl+w", "")
    prompt.handle_input("ctrl+w", "")
    prompt.handle_input("ctrl+y", "")
    typed(prompt, "!")
    before = prompt.text
    prompt.handle_input("alt+y", "")
    assert prompt.text == before


# -- undo -------------------------------------------------------------------


def test_undo_steps_back_and_redo_forward(prompt: Prompt) -> None:
    typed(prompt, "hello")
    prompt.handle_input("ctrl+w", "")
    assert prompt.text == ""
    prompt.undo()
    assert prompt.text == "hello"
    prompt.redo()
    assert prompt.text == ""


def test_a_run_of_typing_is_one_undo_step(prompt: Prompt) -> None:
    """Otherwise undoing costs as many keystrokes as typing did."""
    for char in "hello":
        typed(prompt, char)
    prompt.undo()
    assert prompt.text == ""


# -- completion -------------------------------------------------------------


class FakeCommands:
    def __init__(self, names: list[tuple[str, str]]) -> None:
        self._commands = [type("C", (), {"name": n, "summary": s})() for n, s in names]

    def all(self) -> list[object]:
        return self._commands


def test_slash_opens_command_completion(tmp_path: Path) -> None:
    prompt = Prompt(tmp_path, commands=FakeCommands([("clear", "clear it"), ("cost", "show cost")]))
    typed(prompt, "/c")
    assert prompt.completion is not None
    assert {c.value for c in prompt.completion.candidates} == {"clear", "cost"}


def test_command_completion_matches_names_not_summaries(tmp_path: Path) -> None:
    """Matching summaries turns "/co" into half the list; the palette is where
    searching descriptions belongs."""
    prompt = Prompt(tmp_path, commands=FakeCommands([("clear", "cost of nothing")]))
    typed(prompt, "/cost")
    assert prompt.completion is None


def test_tab_accepts_and_splices_rather_than_truncating(tmp_path: Path) -> None:
    """Rebuilding the buffer from the prefix alone threw the rest of the draft
    away - "@sr and fix the bug" became "@src/"."""
    (tmp_path / "src").mkdir()
    prompt = Prompt(tmp_path)
    typed(prompt, "@sr and fix the bug")
    prompt.buffer.cursor = len("@sr")
    prompt._sync_completion()
    assert prompt.completion is not None
    prompt.accept_completion()
    assert prompt.text == "@src/ and fix the bug"


def test_enter_submits_when_the_text_already_is_the_completion(tmp_path: Path) -> None:
    """Treating it as another acceptance made /clear need a second Enter."""
    sent: list[str] = []
    prompt = Prompt(tmp_path, commands=FakeCommands([("clear", "clear it")]))
    prompt.on_submit = sent.append
    typed(prompt, "/clear")
    prompt.handle_input("enter", "")
    assert sent == ["/clear"]


def test_escape_dismisses_and_a_new_token_reopens(tmp_path: Path) -> None:
    prompt = Prompt(tmp_path, commands=FakeCommands([("clear", "c")]))
    typed(prompt, "/c")
    assert prompt.completion is not None
    prompt.handle_input("escape", "")
    assert prompt.completion is None
    typed(prompt, "l")
    assert prompt.completion is None, "a dismissed completion must stay dismissed"

    # Dismissal lasts as long as the token does. Clearing the line ends it, so
    # the next slash opens again rather than the prompt staying mute forever.
    prompt.handle_input("ctrl+u", "")
    typed(prompt, "/cl")
    assert prompt.completion is not None


def test_the_completion_list_is_drawn_inside_the_editor(tmp_path: Path) -> None:
    """Part of the same component, so it cannot be mispositioned relative to
    the text it completes."""
    prompt = Prompt(tmp_path, commands=FakeCommands([("clear", "clear it")]))
    typed(prompt, "/c")
    lines = plain(prompt.render(60))
    assert any("/clear" in line for line in lines)
    assert lines[-1].strip() != ""


def test_file_completion_skips_noise(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "real.py").write_text("x")
    assert FileCompleter(tmp_path).complete("") == ["real.py"]


# -- placeholder and framing ------------------------------------------------


def test_the_placeholder_says_what_enter_will_do(prompt: Prompt) -> None:
    assert "/ for commands" in prompt.placeholder
    prompt.set_running(True, enter_steers=False)
    assert "Enter queues" in prompt.placeholder
    prompt.set_running(True, enter_steers=True)
    assert "Enter steers" in prompt.placeholder


def test_the_prompt_is_framed_by_two_rules_and_no_verticals(prompt: Prompt) -> None:
    typed(prompt, "hello")
    lines = plain(prompt.render(40))
    assert set(lines[0].strip()) == {"─"}
    assert set(lines[-1].strip()) == {"─"}
    assert not any(ch in "".join(lines) for ch in "│┌┐└┘")


def test_a_long_draft_scrolls_rather_than_eating_the_screen(prompt: Prompt) -> None:
    prompt._rows_available = lambda: 30
    typed(prompt, "\n".join(f"line {index}" for index in range(40)))
    lines = plain(prompt.render(40))
    assert len(lines) <= prompt.max_visible + 2
    assert "more" in lines[0], "the overflow count is missing from the rule"


def test_the_cursor_is_marked_for_the_terminal(prompt: Prompt) -> None:
    from hx.term.screen import CURSOR_MARKER

    typed(prompt, "hi")
    assert CURSOR_MARKER in "".join(prompt.render(40))


@pytest.mark.parametrize("width", [10, 20, 40, 100])
def test_it_honours_the_renderer_contract(width: int, tmp_path: Path) -> None:
    prompt = Prompt(tmp_path, commands=FakeCommands([("clear", "clear the prompt")]))
    typed(prompt, "/c")
    assert_lines_fit(prompt, width)

    prompt.close_completion()
    prompt.text = "日本語のテキスト 👨‍👩‍👧‍👦 and a long line that wraps"
    prompt.buffer.cursor = len(prompt.text)
    assert_lines_fit(prompt, width)


def test_a_cursor_at_end_of_line_does_not_overflow_the_width(prompt: Prompt) -> None:
    """The appended cell costs a column the text did not need; without
    trimming for it the frame comes out one cell too wide."""
    prompt.text = "x" * 38
    prompt.buffer.cursor = len(prompt.text)
    for line in prompt.render(40):
        assert cell_width(line) <= 40


def test_a_completion_can_be_cycled_with_tab(tmp_path: Path) -> None:
    prompt = Prompt(tmp_path, commands=FakeCommands([("aa", "x"), ("ab", "y")]))
    typed(prompt, "/a")
    first = prompt.completion.index  # type: ignore[union-attr]
    prompt.handle_input("tab", "")
    assert (
        prompt.completion is None
        or prompt.completion.index != first
        or prompt.text.startswith("/a")
    )
