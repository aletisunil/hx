"""Markdown rendering, and the palette roles that now reach it.

Rich's Markdown hard-codes its own styles for links, bullets, quotes, rules
and inline code, so ten of the palette's roles were settable by users and had
no effect on anything. Owning the renderer is what changes that, and these
assert the roles are actually asked for.
"""

from __future__ import annotations

import pytest

from hx.term.markdown import Markdown, Painter, render_markdown
from hx.term.width import cell_width, strip_ansi
from tests.term.conftest import assert_lines_fit


class Recording(Painter):
    """Records which roles were painted, and marks them visibly."""

    def __init__(self) -> None:
        self.roles: list[str] = []

    def paint(self, role: str, text: str, **kwargs: bool) -> str:
        self.roles.append(role)
        flags = "".join(sorted(k[0] for k, v in kwargs.items() if v))
        return f"<{role}{':' + flags if flags else ''}>{text}</{role}>"

    def code(self, source: str, language: str | None) -> list[str]:
        self.roles.append(f"code:{language}")
        return source.split("\n")


def plain(text: str, width: int = 60) -> list[str]:
    return [strip_ansi(line) for line in render_markdown(text, width)]


def test_h1_and_h2_drop_their_hashes() -> None:
    """A heading is already visibly a heading; the hashes are syntax, and
    leaving them in makes prose read like a source file."""
    assert plain("# Title") == ["Title"]
    assert plain("## Section") == ["Section"]


def test_h3_and_below_keep_theirs() -> None:
    """By then the level is worth stating, and bold-on-bold no longer
    distinguishes them."""
    assert plain("### Detail") == ["### Detail"]
    assert plain("#### Deeper") == ["#### Deeper"]


def test_heading_levels_are_actually_distinguished() -> None:
    """The old renderer had an if/else whose branches were identical, so an h1
    and an h4 came out the same."""
    painter = Recording()
    h1 = render_markdown("# One", 40, painter)[0]
    h2 = render_markdown("## Two", 40, painter)[0]
    h3 = render_markdown("### Three", 40, painter)[0]
    assert h1 != h2 != h3
    assert ":bu>" in h1, "h1 is bold and underlined"
    assert ":b>" in h2, "h2 is bold only"
    assert "###" in h3


def test_paragraphs_wrap_to_the_width() -> None:
    lines = plain("the quick brown fox jumps over the lazy dog", 20)
    assert all(cell_width(line) <= 20 for line in lines)
    assert " ".join(lines).split() == [
        "the",
        "quick",
        "brown",
        "fox",
        "jumps",
        "over",
        "the",
        "lazy",
        "dog",
    ]


def test_inline_code_uses_its_role() -> None:
    painter = Recording()
    out = render_markdown("some `code` here", 40, painter)
    assert "md_code" in painter.roles
    assert "<md_code>code</md_code>" in out[0]


def test_a_link_is_hyperlinked_and_uses_its_role() -> None:
    painter = Recording()
    out = render_markdown("[label](https://example.com)", 40, painter)
    assert "md_link" in painter.roles
    assert "\x1b]8;;https://example.com\x07" in out[0]
    assert "label" in strip_ansi(out[0])


def test_a_bare_link_still_renders_its_url() -> None:
    assert "example.com" in " ".join(plain("<https://example.com>"))


def test_list_bullets_use_their_own_role() -> None:
    painter = Recording()
    render_markdown("- one\n- two", 40, painter)
    assert "md_bullet" in painter.roles


def test_list_continuations_align_under_the_text() -> None:
    lines = plain("- an item long enough that it has to wrap somewhere", 24)
    assert lines[0].startswith("- ")
    assert lines[1].startswith("  "), "continuation sits under the text"
    assert not lines[1].startswith("- ")


def test_an_ordered_list_keeps_its_numbers() -> None:
    assert plain("1. one\n2. two") == ["1. one", "2. two"]


def test_a_nested_list_indents_under_its_parent() -> None:
    lines = plain("- outer\n  - inner")
    assert lines[0] == "- outer"
    assert lines[-1].strip() == "- inner"
    assert lines[-1].startswith("  ")


def test_a_blockquote_gets_a_rail_not_a_box() -> None:
    painter = Recording()
    out = render_markdown("> quoted", 40, painter)
    assert "md_quote_border" in painter.roles
    assert "md_quote" in painter.roles
    assert "│" in strip_ansi(out[0])


def test_a_horizontal_rule_uses_its_role_and_is_capped() -> None:
    """A rule spanning a very wide terminal reads as a divider in a book."""
    painter = Recording()
    out = render_markdown("---", 200, painter)
    assert "md_hr" in painter.roles
    assert 1 <= strip_ansi(out[0]).count("─") <= 80


def test_a_fenced_block_is_highlighted_and_indented() -> None:
    painter = Recording()
    out = render_markdown("```python\nx = 1\n```", 40, painter)
    assert "code:python" in painter.roles
    assert "md_code_block_border" in painter.roles
    body = [line for line in out if "x = 1" in line]
    assert body and strip_ansi(body[0]).startswith("  ")


def test_a_table_is_the_one_place_box_drawing_is_allowed() -> None:
    lines = plain("| a | b |\n| - | - |\n| 1 | 2 |")
    assert any("┼" in line for line in lines)
    assert all(line.startswith("│") for line in lines if line and "┼" not in line)


def test_a_wide_table_is_shrunk_rather_than_overflowing() -> None:
    text = "| a very wide column indeed | another wide one |\n| - | - |\n| x | y |"
    for line in render_markdown(text, 30):
        assert cell_width(line) <= 30


def test_blocks_are_separated_by_one_blank_line_never_two() -> None:
    lines = plain("para one\n\n\n\npara two")
    assert lines == ["para one", "", "para two"]


def test_leading_and_trailing_blanks_are_trimmed() -> None:
    """The producer emits the spacing between blocks; a block that also brings
    its own leaves a double gap."""
    lines = render_markdown("\n\nhello\n\n", 40)
    assert lines[0] != ""
    assert lines[-1] != ""


def test_a_blank_line_inside_a_list_item_stays_blank() -> None:
    """Indenting it would emit a run of spaces: invisible, but still counted."""
    for line in plain("- one\n\n  still one\n\n- two"):
        assert line == line.rstrip(), f"trailing whitespace in {line!r}"


@pytest.mark.parametrize("width", [12, 20, 40, 80, 200])
def test_every_line_fits_whatever_the_document(width: int) -> None:
    document = (
        "# Heading\n\nA paragraph with `code`, **bold**, and a "
        "[link](https://example.com/some/long/path).\n\n"
        "- a list item long enough to wrap\n  - and a nested one\n\n"
        "> a quote\n\n```python\ndef f():\n    return 1\n```\n\n"
        "| col | other |\n| --- | ----- |\n| a | b |\n\n---\n\n"
        "日本語のテキスト and 👨‍👩‍👧‍👦 emoji.\n"
    )
    assert_lines_fit(Markdown(document), width)


def test_the_component_caches_until_the_source_changes() -> None:
    component = Markdown("# One")
    first = component.render(40)
    assert component.render(40) is first
    component.set_source("# Two")
    assert component.render(40) != first
