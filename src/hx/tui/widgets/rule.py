"""A full-width horizontal rule.

pi's ``DynamicBorder``: one ``─`` across whatever width it is given, in the
border colour. It is the only separator the design needs - a box around every
section turns a dialog into a stack of rectangles.
"""

from __future__ import annotations

from rich.console import RenderableType
from rich.text import Text
from textual.widgets import Static

from hx.tui.theme import THEME


class Rule(Static):
    """A themed horizontal rule that fills its container."""

    def __init__(self, id: str | None = None, role: str = "border") -> None:
        super().__init__(id=id)
        self.role = role

    def render(self) -> RenderableType:
        width = self.size.width
        return Text("─" * width, style=THEME.fg(self.role)) if width > 0 else Text("")
