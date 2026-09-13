"""Transcript blocks: what each one draws, and the grammar they share."""

from __future__ import annotations

from pathlib import Path

import pytest

from hx.term.width import cell_width, strip_ansi
from hx.tui import paint
from hx.tui.glyphs import SPINNER, TODO_ACTIVE, TODO_DONE, TODO_PENDING, TOOL_DONE, TOOL_FAILED
from hx.tui.renderers import ToolCall
from hx.tui.views.blocks import (
    AssistantMessage,
    Notice,
    ThinkingMessage,
    TodoBlock,
    ToolBlock,
    UserMessage,
)
from tests.term.conftest import assert_lines_fit, plain

CWD = Path("/project")


@pytest.fixture(autouse=True)
def _pinned_colors() -> None:
    """So an assertion about colour does not depend on the terminal the suite
    happens to run under."""
    paint.set_color_mode("truecolor")


def call(name: str, **params: object) -> ToolCall:
    return ToolCall(name=name, params=dict(params), cwd=CWD)


def tinted(lines: list[str]) -> bool:
    return all("48;2;" in line for line in lines)


# -- who is speaking --------------------------------------------------------


def test_the_user_block_is_tinted_the_full_width() -> None:
    """A block only as wide as its text reads as a ragged column, not a block."""
    lines = UserMessage("hello").render(40)
    assert tinted(lines)
    assert all(cell_width(line) == 40 for line in lines)


def test_the_assistant_block_has_no_background_at_all() -> None:
    """The asymmetry between the two is the speaker distinction. It needs no
    "You:" prefix, which would cost a column on every line."""
    lines = AssistantMessage("hello").render(40)
    assert not any("48;2;" in line for line in lines)


def test_a_user_message_renders_its_markdown() -> None:
    """Someone pasting a list meant it as a list."""
    assert "- one" in " ".join(plain(UserMessage("- one\n- two").render(40)))


def test_an_empty_assistant_message_draws_nothing() -> None:
    """A turn that has not produced text yet must not reserve a row and then
    push the transcript down when it does."""
    assert AssistantMessage("").render(40) == []
    assert AssistantMessage("   \n ").render(40) == []


def test_streaming_appends_without_redrawing_from_scratch() -> None:
    message = AssistantMessage("Hello")
    message.append(" there")
    assert "Hello there" in " ".join(plain(message.render(40)))


def test_thinking_collapses_to_one_line() -> None:
    block = ThinkingMessage("a long private reasoning trace", collapsed=True)
    assert plain(block.render(40)) == [" Thinking…"]
    block.set_collapsed(False)
    assert "reasoning" in " ".join(plain(block.render(40)))


# -- tool calls -------------------------------------------------------------


def test_the_tint_carries_the_state_not_a_label() -> None:
    """Three backgrounds rather than three words, so a long transcript can be
    skimmed for the failed one without reading any of it."""
    running = ToolBlock(call("bash", command="ls"))
    done = ToolBlock(ToolCall("bash", {"command": "ls"}, CWD, finished=True))
    failed = ToolBlock(ToolCall("bash", {"command": "ls"}, CWD, finished=True, is_error=True))

    backgrounds = {block.render(40)[0] for block in (running, done, failed)}
    assert len(backgrounds) == 3, "two states share a tint"


def test_a_running_call_spins_and_a_finished_one_does_not() -> None:
    running = ToolBlock(call("bash", command="ls"))
    assert SPINNER[0] in strip_ansi(running.render(40)[1])
    running.tick()
    assert SPINNER[1] in strip_ansi(running.render(40)[1])

    done = ToolBlock(ToolCall("bash", {"command": "ls"}, CWD, finished=True))
    before = done.render(40)
    done.tick()
    assert done.render(40) == before


def test_finished_and_failed_have_their_own_markers() -> None:
    done = ToolBlock(ToolCall("read", {"file_path": "a.py"}, CWD, finished=True))
    failed = ToolBlock(ToolCall("read", {"file_path": "a.py"}, CWD, finished=True, is_error=True))
    assert TOOL_DONE in strip_ansi(done.render(40)[1])
    assert TOOL_FAILED in strip_ansi(failed.render(40)[1])


def test_a_bash_call_keeps_its_prompt_character() -> None:
    """The same renderer draws this and the approval for it, so the user agrees
    to the shape of the thing they then watch run."""
    block = ToolBlock(call("bash", command="rm -rf build/"))
    assert "$ rm -rf build/" in strip_ansi(block.render(60)[1])


def test_the_body_is_indented_under_the_header_text() -> None:
    """Not under its marker, so the block has one left edge instead of two."""
    block = ToolBlock(ToolCall("bash", {"command": "ls"}, CWD, output="a.py", finished=True))
    lines = plain(block.render(40))
    header = next(line for line in lines if "$ ls" in line)
    body = next(line for line in lines if "a.py" in line)
    assert body.index("a.py") == header.index("$")


def test_bash_output_shows_the_tail_because_the_answer_is_at_the_end() -> None:
    output = "\n".join(f"line {index}" for index in range(30))
    block = ToolBlock(ToolCall("bash", {"command": "x"}, CWD, output=output, finished=True))
    shown = " ".join(plain(block.render(60)))
    assert "line 29" in shown
    assert "line 0" not in shown


def test_a_collapsed_read_shows_nothing() -> None:
    """The model read the file; the user did not."""
    block = ToolBlock(ToolCall("read", {"file_path": "a.py"}, CWD, output="x = 1", finished=True))
    assert len(plain(block.render(40))) == 3, "header plus the block's own padding"


def test_expanding_a_block_shows_more() -> None:
    output = "\n".join(f"line {index}" for index in range(30))
    block = ToolBlock(ToolCall("bash", {"command": "x"}, CWD, output=output, finished=True))
    collapsed = len(block.render(60))
    block.toggle()
    assert len(block.render(60)) > collapsed


def test_an_edit_shows_its_diff_even_collapsed() -> None:
    """The diff is the whole point of an edit."""
    diff = "--- a\n+++ b\n@@ -1,2 +1,2 @@\n same\n-before\n+after\n"
    block = ToolBlock(
        ToolCall("edit", {"file_path": "a.py"}, CWD, metadata={"diff": diff}, finished=True)
    )
    shown = " ".join(plain(block.render(60)))
    assert "before" in shown and "after" in shown
    assert "+1" in shown and "-1" in shown


# -- notices ----------------------------------------------------------------


def test_a_notice_hangs_its_bullet() -> None:
    """Half the slash commands used to compensate for the lack of this by hand
    and half did not, so output arrived at two different indents."""
    lines = plain(Notice("a notice long enough that it has to wrap somewhere").render(30))
    assert lines[0].startswith(" · ")
    assert lines[1].startswith("   ")
    assert not lines[1].startswith(" · ")


def test_a_multi_line_notice_indents_every_line() -> None:
    lines = plain(Notice("first\nsecond\nthird").render(40))
    assert lines[0] == " · first"
    assert lines[1] == "   second"
    assert lines[2] == "   third"


@pytest.mark.parametrize(
    ("level", "bullet"), [("info", "·"), ("warning", "!"), ("error", "✗"), ("success", "✓")]
)
def test_each_level_has_its_own_bullet(level: str, bullet: str) -> None:
    assert plain(Notice("x", level).render(40))[0].startswith(f" {bullet} ")


# -- todos ------------------------------------------------------------------


def test_the_plan_is_a_block_not_a_sidebar() -> None:
    """A scrollback-native UI has no column to put a sidebar in."""
    todos = [
        {"content": "done thing", "status": "completed"},
        {"content": "doing thing", "active_form": "Doing thing", "status": "in_progress"},
        {"content": "later thing", "status": "pending"},
    ]
    lines = plain(TodoBlock(todos).render(40))
    assert lines[0].strip() == "Todos  1/3"
    assert TODO_DONE in lines[1] and "done thing" in lines[1]
    assert TODO_ACTIVE in lines[2] and "Doing thing" in lines[2]
    assert TODO_PENDING in lines[3] and "later thing" in lines[3]


def test_an_empty_plan_draws_nothing() -> None:
    assert TodoBlock([]).render(40) == []
    assert TodoBlock(None).render(40) == []


# -- the shared contract ----------------------------------------------------


def _every_block() -> list[object]:
    diff = "--- a\n+++ b\n@@ -1,2 +1,2 @@\n same\n-before this\n+after that\n"
    return [
        UserMessage("a user message with `code` and a very long line that will need wrapping"),
        AssistantMessage("## Heading\n\n- a list item\n- another\n\n> quoted"),
        ThinkingMessage("private reasoning", collapsed=False),
        ToolBlock(call("bash", command="rg -n pattern src/")),
        ToolBlock(ToolCall("read", {"file_path": "a.py"}, CWD, output="x = 1", finished=True)),
        ToolBlock(
            ToolCall("edit", {"file_path": "a.py"}, CWD, metadata={"diff": diff}, finished=True)
        ),
        ToolBlock(ToolCall("mcp_thing", {"a": [1, 2], "b": {"c": 1}}, CWD, finished=True)),
        Notice("an informational notice"),
        Notice("日本語の通知 with 👨‍👩‍👧‍👦", "error"),
        TodoBlock([{"content": "a todo", "status": "pending"}]),
    ]


@pytest.mark.parametrize("width", [20, 40, 80, 120])
def test_every_block_honours_the_renderer_contract(width: int) -> None:
    for block in _every_block():
        assert_lines_fit(block, width)  # type: ignore[arg-type]


def test_no_block_emits_two_blank_lines_in_a_row() -> None:
    """Spacing is the producer's job, one line, never doubled."""
    for block in _every_block():
        lines = plain(block.render(60))  # type: ignore[attr-defined]
        blanks = 0
        for line in lines:
            blanks = blanks + 1 if not line.strip() else 0
            assert blanks < 2, f"{type(block).__name__} emitted two blank lines"
