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
        fence = self.painter.paint("md_code_block_border", "```" + (language or ""))
        self.blank()
        self.emit(fence, indent)
        room = max(1, self.width - indent - len(CODE_INDENT))
        for line in self.painter.code(node.content.rstrip("\n"), language):  # type: ignore[attr-defined]
            self.emit(CODE_INDENT + truncate_to_width(line, room), indent)
        self.emit(self.painter.paint("md_code_block_border", "```"), indent)
        self.blank()

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
