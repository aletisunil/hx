"""Transcript blocks: state tints, expansion, notices, and the working line."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from hx.tui.renderers import expand_hint
from hx.tui.theme import THEME
from hx.tui.widgets.transcript import MessageBlock, Notice, ToolBlock, markdown
from hx.tui.widgets.working import WorkingIndicator


def _plain(renderable: object, width: int = 90) -> str:
    console = Console(width=width, record=True, force_terminal=False)
    console.print(renderable)
    return console.export_text()


def _block(**kwargs: object) -> ToolBlock:
    return ToolBlock("Bash", {"command": "pytest -q"}, Path("/proj"), **kwargs)  # type: ignore[arg-type]


def test_a_tool_block_reports_its_state_before_it_is_read() -> None:
    """Pending, done and failed each get their own tint and marker."""
    block = _block()
    assert block.state == "running"
    assert "○" in _plain(block.render())

    block.finish("236 passed", is_error=False)
    assert block.state == "done"
    assert "●" in _plain(block.render())

    failed = _block()
    failed.finish("error", is_error=True)
    assert failed.state == "error"
    assert "✗" in _plain(failed.render())


def test_a_failed_block_says_why_it_failed() -> None:
    """The reason reaches the screen, not just the model.

    Tools report failures as a constant summary plus a message, so a block that
    drew only the summary was a red band that said nothing.
    """
    block = _block()
    block.finish("error", is_error=True, detail="String to replace was not found")
    assert "String to replace was not found" in _plain(block.render())


def test_a_failure_that_streamed_output_keeps_it() -> None:
    """What the tool printed is the better account of the same failure."""
    block = _block()
    block.append("pytest: error: unrecognized arguments\n")
    block.finish("error", is_error=True, detail="exit code 2")
    rendered = _plain(block.render())
    assert "unrecognized arguments" in rendered
    assert "exit code 2" not in rendered


def test_a_failure_with_nothing_to_show_costs_no_blank_row() -> None:
    """An empty body under the header reads as a rendering fault."""
    block = _block()
    block.finish("error", is_error=True)
    assert len(_plain(block.render()).strip("\n").splitlines()) == 1


def test_a_failed_block_is_tinted_differently_from_a_finished_one() -> None:
    done, failed = _block(), _block()
    done.finish("ok", is_error=False)
    failed.finish("boom", is_error=True)
    assert done.STATE_STYLES[done.state] != failed.STATE_STYLES[failed.state]


def test_expanding_a_block_shows_the_output_it_was_hiding() -> None:
    block = _block()
    block.append("\n".join(f"line {i}" for i in range(40)))
    block.finish("done", is_error=False)

    collapsed = _plain(block.render())
    assert expand_hint() in collapsed
    assert "line 0" not in collapsed

    block.expanded = True
    assert "line 0" in _plain(block.render())


def test_a_user_message_is_set_apart_from_the_answer() -> None:
    """The user's own turn anchors an exchange, so it carries a background."""
    user = MessageBlock("user", "add retry to the fetcher")
    assert THEME.color("user_bg") in str(user.render().style)
    assert "add retry to the fetcher" in _plain(user.render())


def test_thinking_is_visibly_not_the_answer() -> None:
    thinking = MessageBlock("thinking", "weighing two options")
    assert "italic" in str(thinking.render().renderable.style)  # type: ignore[union-attr]


def test_notices_carry_their_level() -> None:
    assert "✗" in _plain(Notice("provider refused", "error").render())
    assert "✓" in _plain(Notice("resumed", "success").render())
    assert "·" in _plain(Notice("compacting", "info").render())


def test_markdown_keeps_the_space_after_inline_code() -> None:
    """Routing code spans through a highlighter eats the following space, which
    turns "`fetch` now retries" into "fetchnow retries"."""
    assert "fetch now retries" in _plain(markdown("`fetch` now retries"))


def test_headings_are_coloured_text_not_a_centred_banner() -> None:
    rendered = _plain(markdown("## What changed\n\nbody"))
    assert rendered.splitlines()[0].startswith("What changed")


def test_the_working_line_is_blank_until_something_is_working() -> None:
    indicator = WorkingIndicator()
    assert _plain(indicator.render()).strip() == ""

    indicator.start("Bash")
    line = _plain(indicator.render())
    assert "Bash" in line
    assert "esc to interrupt" in line

    indicator.stop()
    assert _plain(indicator.render()).strip() == ""
