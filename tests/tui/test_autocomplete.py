"""Prompt completion: the popup, its keys, and what it offers."""

from __future__ import annotations

from pathlib import Path

from hx.tui.fuzzy import filter_items, match
from hx.tui.killring import KillRing
from hx.tui.widgets.autocomplete import Autocomplete, Candidate, Completion


def test_fuzzy_matches_characters_in_order() -> None:
    assert match("pm", "permissions") is not None
    assert match("zzz", "permissions") is None


def test_a_better_match_scores_lower() -> None:
    exact = match("copy", "copy")
    scattered = match("copy", "compact only")
    assert exact is not None and scattered is not None
    assert exact < scattered


def test_every_token_has_to_match() -> None:
    items = ["mcp server status", "model picker"]
    assert filter_items(items, "mcp status", key=lambda s: s) == ["mcp server status"]


def test_filtering_keeps_the_best_first() -> None:
    items = ["compact", "configure", "copy"]
    assert filter_items(items, "co", key=lambda s: s)[0] == "compact"


def test_an_empty_query_keeps_the_original_order() -> None:
    items = ["b", "a", "c"]
    assert filter_items(items, "  ", key=lambda s: s) == items


def test_the_popup_marks_the_selected_row() -> None:
    completion = Completion(
        start=0,
        prefix="/",
        candidates=[Candidate("copy", "/copy", "Copy"), Candidate("cost", "/cost", "Cost")],
    )
    popup = Autocomplete()
    popup.show(completion)
    rendered = "\n".join(str(row) for row in popup.render().renderables)

    assert "› /copy" in rendered
    assert "  /cost" in rendered


def test_the_popup_hides_itself_when_there_is_nothing_to_show() -> None:
    popup = Autocomplete()
    popup.show(Completion(start=0, prefix="/", candidates=[]))
    assert popup.display is False


def test_selection_wraps_around() -> None:
    completion = Completion(
        start=0, prefix="/", candidates=[Candidate("a", "a"), Candidate("b", "b")]
    )
    completion.move(-1)
    assert completion.index == 1
    completion.move(1)
    assert completion.index == 0


def test_descriptions_line_up_under_each_other() -> None:
    """Ragged columns are the difference between a list and a table."""
    completion = Completion(
        start=0,
        prefix="/",
        candidates=[Candidate("a", "/compact", "one"), Candidate("b", "/configure", "two")],
    )
    popup = Autocomplete()
    popup.show(completion)
    rows = [str(row) for row in popup.render().renderables]
    assert rows[0].index("one") == rows[1].index("two")


def test_path_completion_ignores_noise_directories(tmp_path: Path) -> None:
    from hx.tui.widgets.input import FileCompleter

    (tmp_path / "src").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / ".git").mkdir()

    completions = FileCompleter(tmp_path).complete("")
    assert "src/" in completions
    assert not any(name.startswith(("node_modules", ".git")) for name in completions)


def test_the_kill_ring_gives_back_the_last_kill_first() -> None:
    ring = KillRing()
    ring.kill("first")
    ring.kill("second")
    assert ring.yank() == "second"
    assert ring.yank_pop() == "first"
    assert ring.yank_pop() == "second"  # wraps


def test_an_empty_kill_is_not_recorded() -> None:
    ring = KillRing()
    ring.kill("")
    assert ring.yank() is None


def test_accepting_a_completion_keeps_the_rest_of_the_draft(tmp_path: Path) -> None:
    """The token is spliced, not the buffer rebuilt from its prefix.

    Rebuilding it threw away everything past the cursor, so tab-completing a
    path mentioned early in a sentence deleted the sentence.
    """
    from hx.tui.widgets.input import PromptInput

    (tmp_path / "src").mkdir()
    prompt = PromptInput(tmp_path)
    prompt.text = "@sr and then fix the bug"
    prompt.move_cursor((0, 3))

    prompt.completion = prompt._build_completion()
    assert prompt.completion is not None
    prompt.accept_completion()

    assert prompt.text == "@src/ and then fix the bug"
    assert prompt.cursor_location == (0, 5), "cursor lands after the completion, not at the end"


def test_accepting_a_completion_leaves_later_lines_intact(tmp_path: Path) -> None:
    from hx.tui.widgets.input import PromptInput

    (tmp_path / "src").mkdir()
    prompt = PromptInput(tmp_path)
    prompt.text = "look at @sr\nand keep this line"
    prompt.move_cursor((0, 11))

    prompt.completion = prompt._build_completion()
    assert prompt.completion is not None
    prompt.accept_completion()

    assert prompt.text == "look at @src/\nand keep this line"


def test_yank_pop_replaces_the_yank_it_follows(tmp_path: Path) -> None:
    from hx.tui.widgets.input import PromptInput

    prompt = PromptInput(tmp_path)
    prompt.text = "hello"
    prompt.move_cursor((0, 5))
    prompt.kill_ring.kill("second")
    prompt.kill_ring.kill("first")

    prompt._yank()
    assert prompt.text == "hellofirst"
    prompt._yank_pop()
    assert prompt.text == "hellosecond"


def test_yank_pop_does_nothing_once_the_buffer_has_moved_on(tmp_path: Path) -> None:
    """The recorded range is stale after any other edit, and replacing it blind
    mangled whatever had shifted into those columns."""
    from hx.tui.widgets.input import PromptInput

    prompt = PromptInput(tmp_path)
    prompt.text = "hello"
    prompt.move_cursor((0, 5))
    prompt.kill_ring.kill("second")
    prompt.kill_ring.kill("first")

    prompt._yank()
    prompt.move_cursor((0, 0))
    prompt.insert("X")
    assert prompt.text == "Xhellofirst"

    prompt._yank_pop()
    assert prompt.text == "Xhellofirst", "yank-pop is only valid immediately after a yank"
