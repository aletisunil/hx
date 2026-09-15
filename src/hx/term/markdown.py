"""Rendering markdown to styled terminal lines.

Parsing is :mod:`markdown_it`'s job - the same CommonMark parser Rich uses, so
the token stream is familiar and the edge cases are somebody else's. Rendering
is ours, and owning it is the point: Rich's ``Markdown`` hard-codes its own
styles for links, list bullets, block quotes, rules and inline code, so ten of
the palette's roles were defined, documented, settable by users, and had no
effect on anything. Here they do.

Two conventions worth naming, both taken from pi:

* **H1 and H2 drop their ``#``.** A heading is already visibly a heading; the
  hashes are syntax, and leaving them in makes prose look like a source file.
  H3 and below keep theirs, because by then the level is worth stating and
  bold-on-bold no longer distinguishes them.
* **Code blocks lose their fences.** ``` is syntax too, and printing it back
  out tells the reader only that the renderer gave up. The block is a band of
  its own colour instead, with the language named at the top of it - and a rule
  above and below for a theme that has no colour to tint with, which is what
  the ``ansi`` one deliberately is.
* **Tables are the one place box-drawing is allowed.** A table without rules is
  not a table. Everywhere else, a rule above and below - never a rectangle.
"""

from __future__ import annotations

from hx.term.ansi import fill_line, hyperlink, wrap
from hx.term.component import Widget
from hx.term.width import cell_width, truncate_to_width

LIST_INDENT = 4
"""Columns per nesting level, wide enough that a marker and its text both fit."""

CODE_INDENT = "  "
CODE_RULE = "─"
QUOTE_RAIL = "│ "
HR_MAX = 80
"""A rule spanning a very wide terminal reads as a divider in a book, not a
paragraph break. Capping it keeps it proportionate to the text."""


class Painter:
    """How each markdown element is coloured.

    Injected rather than imported so that :mod:`hx.term` stays independent of
    HX's palette, and so a test can assert on structure without colour.
    """

    def paint(self, role: str, text: str, **kwargs: bool) -> str:
        return text

    def fill(self, role: str, text: str) -> str | None:
        """``text`` on a background, or ``None`` if this role has none.

        A theme is allowed to have no opinion about colour - the ``ansi`` one
        deliberately has none, deferring to the terminal's own palette - and a
        band tinted in a colour that resolves to nothing is not a band. So the
        renderer asks rather than assumes, and rules the block instead when the
        answer is no.
        """
        return None

    def code(self, source: str, language: str | None) -> list[str]:
        return source.split("\n")


def render_markdown(text: str, width: int, painter: Painter | None = None) -> list[str]:
    """Render ``text`` into lines at most ``width`` cells wide."""
    from markdown_it import MarkdownIt
    from markdown_it.tree import SyntaxTreeNode

    painter = painter or Painter()
    parser = MarkdownIt("commonmark", {"breaks": False}).enable("table").enable("strikethrough")
    tree = SyntaxTreeNode(parser.parse(text))
    renderer = _Renderer(painter, width)
    renderer.block(tree, indent=0)
    return renderer.trim()


class _Renderer:
    def __init__(self, painter: Painter, width: int) -> None:
        self.painter = painter
        self.width = width
        self.lines: list[str] = []

    # -- assembly ----------------------------------------------------------

    def emit(self, line: str, indent: int) -> None:
        self.lines.append(" " * indent + line if line else "")

    def blank(self) -> None:
        """One blank line between blocks, never two."""
        if self.lines and self.lines[-1] != "":
            self.lines.append("")

    def trim(self) -> list[str]:
        while self.lines and self.lines[-1] == "":
            self.lines.pop()
        while self.lines and self.lines[0] == "":
            self.lines.pop(0)
        return self.lines

    def wrapped(self, text: str, indent: int, hanging: int = 0) -> list[str]:
        room = max(1, self.width - indent - hanging)
        return wrap(text, room)

    # -- blocks ------------------------------------------------------------

    def block(self, node: object, indent: int) -> None:
        for child in node.children:  # type: ignore[attr-defined]
            self.node(child, indent)

    def node(self, node: object, indent: int) -> None:
        kind = node.type  # type: ignore[attr-defined]
        handler = getattr(self, f"_{kind}", None)
        if handler is not None:
            handler(node, indent)
        else:
            self.block(node, indent)

    def _heading(self, node: object, indent: int) -> None:
        level = int(node.tag[1:])  # type: ignore[attr-defined]
        body = self.inline(node)
        self.blank()
        if level == 1:
            text = self.painter.paint("md_heading", body, bold=True, underline=True)
        elif level == 2:
            text = self.painter.paint("md_heading", body, bold=True)
        else:
            text = self.painter.paint("md_heading", "#" * level + " " + body, bold=True)
        for line in self.wrapped(text, indent):
            self.emit(line, indent)
        self.blank()

    def _paragraph(self, node: object, indent: int) -> None:
        body = self.inline(node)
        if not body:
            return
        self.blank()
        for line in self.wrapped(body, indent):
            self.emit(line, indent)
        self.blank()

    def _fence(self, node: object, indent: int) -> None:
        language = (node.info or "").strip().split(" ")[0] or None  # type: ignore[attr-defined]
        available = max(1, self.width - indent)
        room = max(1, available - 2 * len(CODE_INDENT))
        body = [
            truncate_to_width(line, room)
            for line in self.painter.code(node.content.rstrip("\n"), language)  # type: ignore[attr-defined]
        ]

        self.blank()
        if self.painter.fill("md_code_block_bg", "") is None:
            self._ruled_block(body, language, available, indent)
        else:
            self._tinted_block(body, language, available, indent)
        self.blank()

    def _tinted_block(
        self, body: list[str], language: str | None, available: int, indent: int
    ) -> None:
        """Code on a band of its own colour, the language named at the top.

        The band spans the column rather than the longest line, for the same
        reason a tinted message does: a block whose right edge follows the code
        is a ragged column, not a block. The language label and a blank row
        below the code are the padding - inside the tint, so the band is a
        rectangle rather than a highlighted run of text.
        """
        span = available
        label = truncate_to_width(language or "", max(0, span - 2 * len(CODE_INDENT)))
        head = self.painter.paint("md_code_block", label) if label else ""
        rows = [head, *body, ""]
        for row in rows:
            padding = " " * max(0, span - len(CODE_INDENT) - cell_width(row))
            filled = self.painter.fill("md_code_block_bg", CODE_INDENT + row + padding)
            self.emit(filled if filled is not None else CODE_INDENT + row, indent)

    def _ruled_block(
        self, body: list[str], language: str | None, available: int, indent: int
    ) -> None:
        """The same block for a theme with no colour of its own to tint with.

        Capped like a thematic break, but never shorter than the code it
        encloses: a rule the content overhangs reads as a broken block.
        """
        widest = max((cell_width(line) for line in body), default=0) + len(CODE_INDENT)
        span = min(available, max(HR_MAX, widest))
        self.emit(self._code_rule(span, language), indent)
        for line in body:
            self.emit(CODE_INDENT + line, indent)
        self.emit(self.painter.paint("md_code_block_border", CODE_RULE * span), indent)

    def _code_rule(self, span: int, language: str | None) -> str:
        """The rule opening a code block, carrying the language if there is one.

        The fence markers themselves are syntax, the same way a heading's
        hashes are: the reader is looking at source either way, and ``` in the
        middle of rendered prose only tells them the renderer gave up. A rule
        says the same thing in the vocabulary the rest of the UI uses, and the
        language sits in it rather than on a line of its own.
        """
        paint = self.painter.paint
        label = truncate_to_width(language or "", max(0, span - 4))
        if not label:
            return paint("md_code_block_border", CODE_RULE * span)
        trail = CODE_RULE * (span - 4 - cell_width(label))
        return (
            paint("md_code_block_border", CODE_RULE * 2)
            + " "
            + paint("md_code_block", label)
            + " "
            + paint("md_code_block_border", trail)
        )

    _code_block = _fence

    def _hr(self, _node: object, indent: int) -> None:
        span = min(self.width - indent, HR_MAX)
        self.blank()
        self.emit(self.painter.paint("md_hr", "─" * max(1, span)), indent)
        self.blank()

    def _blockquote(self, node: object, indent: int) -> None:
        inner = _Renderer(self.painter, self.width - indent - cell_width(QUOTE_RAIL))
        inner.block(node, 0)
        rail = self.painter.paint("md_quote_border", QUOTE_RAIL)
        self.blank()
        for line in inner.trim():
            if line:
                self.emit(rail + self.painter.paint("md_quote", line, italic=True), indent)
            else:
                self.emit(rail, indent)
        self.blank()

    def _bullet_list(self, node: object, indent: int) -> None:
        self._list(node, indent, ordered=False)

    def _ordered_list(self, node: object, indent: int) -> None:
        self._list(node, indent, ordered=True)

    def _list(self, node: object, indent: int, *, ordered: bool) -> None:
        start = int(getattr(node, "attrs", {}).get("start", 1) or 1)
        self.blank()
        for offset, item in enumerate(node.children):  # type: ignore[attr-defined]
            marker = f"{start + offset}." if ordered else "-"
            self._list_item(item, indent, marker)
        self.blank()

    def _list_item(self, node: object, indent: int, marker: str) -> None:
        painted = self.painter.paint("md_bullet", marker)
        gutter = cell_width(marker) + 1

        inner = _Renderer(self.painter, self.width - indent - gutter)
        inner.block(node, 0)
        body = inner.trim()
        if not body:
            self.emit(painted, indent)
            return

        for position, line in enumerate(body):
            if position == 0:
                self.emit(painted + " " + line, indent)
            elif line:
                # Continuation aligns under the item's text, not its marker.
                self.emit(" " * gutter + line, indent)
            else:
                # A blank line stays blank; indenting it would emit a run of
                # spaces that is invisible but still counts toward the width.
                self.emit("", indent)

    def _table(self, node: object, indent: int) -> None:
        rows: list[list[str]] = []
        header = 0
        for section in node.children:  # type: ignore[attr-defined]
            for row in section.children:
                rows.append([self.inline(cell) for cell in row.children])
            if section.type == "thead":
                header = len(rows)
        if not rows:
            return

        count = max(len(row) for row in rows)
        widths = [
            max((cell_width(row[i]) for row in rows if i < len(row)), default=0)
            for i in range(count)
        ]
        # Shrink to fit rather than overflowing the terminal.
        budget = self.width - indent - (3 * count + 1)
        while sum(widths) > budget and max(widths) > 3:
            widths[widths.index(max(widths))] -= 1

        def line(row: list[str]) -> str:
            cells = []
            for i in range(count):
                cell = row[i] if i < len(row) else ""
                cell = truncate_to_width(cell, widths[i])
                cells.append(cell + " " * (widths[i] - cell_width(cell)))
            return "│ " + " │ ".join(cells) + " │"

        divider = "├─" + "─┼─".join("─" * w for w in widths) + "─┤"

        self.blank()
        for index, row in enumerate(rows):
            if index == header and header:
                self.emit(self.painter.paint("md_code_block_border", divider), indent)
            self.emit(line(row), indent)
        self.blank()

    # -- inline ------------------------------------------------------------

    def inline(self, node: object) -> str:
        out: list[str] = []
        for child in getattr(node, "children", None) or []:
            out.append(self._inline_node(child))
        return "".join(out)

    def _inline_node(self, node: object) -> str:
        kind = node.type  # type: ignore[attr-defined]
        paint = self.painter.paint

        if kind == "text":
            return str(node.content)  # type: ignore[attr-defined]
        if kind == "code_inline":
            return paint("md_code", str(node.content))  # type: ignore[attr-defined]
        if kind == "softbreak":
            return " "
        if kind == "hardbreak":
            return "\n"
        if kind == "strong":
            return paint("text", self.inline(node), bold=True)
        if kind == "em":
            return paint("text", self.inline(node), italic=True)
        if kind == "s":
            return self.inline(node)
        if kind == "link":
            url = node.attrs.get("href", "")  # type: ignore[attr-defined]
            label = self.inline(node) or str(url)
            painted = paint("md_link", label, underline=True)
            return hyperlink(str(url), painted) if url else painted
        if kind == "image":
            alt = self.inline(node) or "image"
            return paint("md_link_url", f"[{alt}]")
        if kind == "inline":
            return self.inline(node)
        return str(getattr(node, "content", "") or "") or self.inline(node)


class Markdown(Widget):
    """A block of markdown as a component."""

    __slots__ = ("_painter", "_source")

    def __init__(self, source: str = "", painter: Painter | None = None) -> None:
        super().__init__()
        self._source = source
        self._painter = painter or Painter()

    def set_source(self, source: str) -> None:
        if source != self._source:
            self._source = source
            self.invalidate()

    def draw(self, width: int) -> list[str]:
        # Padded and terminated here rather than by the caller, so the
        # component honours the renderer's contract like every other one.
        return [
            fill_line(line, width) for line in render_markdown(self._source, width, self._painter)
        ]
