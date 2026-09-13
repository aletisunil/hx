"""The hints line under the prompt.

Every key it shows is read from the keybinding registry, so rebinding a key in
``~/.hx/keybindings.json`` moves the hint with it. That is the whole point:
before, the keys lived in three hand-written lists and the one on screen was
the one most likely to be wrong.
"""

from __future__ import annotations

from rich.console import RenderableType
from rich.text import Text
from textual.widgets import Static

from hx.keys import KEYMAP
from hx.tui.theme import THEME

#: Compact hints, in the order a new user needs them. pi shows the same shape:
#: dim key, muted description, separated by a middle dot.
HINTS: tuple[tuple[str, str], ...] = (
    ("app.interrupt", "interrupt"),
    ("app.clear", "clear"),
    ("app.commands", "commands"),
    ("app.tools.expand", "expand"),
    ("app.message.copy", "copy"),
)

#: Hints with no keybinding behind them: prefixes the parser looks for.
LITERAL_HINTS: tuple[tuple[str, str], ...] = (
    ("/", "commands"),
    ("!", "bash"),
    ("@", "files"),
)


class HintsBar(Static):
    """One dim line of shortcuts, always visible under the prompt."""

    def __init__(self) -> None:
        super().__init__(id="hints")

    def parts(self) -> list[Text]:
        """One styled hint per entry, in priority order."""
        parts: list[Text] = []
        for action, description in HINTS:
            keys = KEYMAP.primary(action)
            if not keys:
                continue
            part = Text(keys, style=THEME.fg("dim"))
            part.append(f" {description}", style=THEME.fg("muted"))
            parts.append(part)
        for literal, description in LITERAL_HINTS:
            part = Text(literal, style=THEME.fg("dim"))
            part.append(f" {description}", style=THEME.fg("muted"))
            parts.append(part)
        return parts

    def hints_line(self, width: int) -> Text:
        """As many hints as fit, dropped whole.

        A hint clipped mid-word is worse than one that is absent: the reader
        cannot tell whether the key is ``ctrl+x`` or ``ctrl+x…something``.
        """
        line = Text()
        for part in self.parts():
            addition = 3 + len(part) if len(line) else len(part)
            if width and len(line) + addition > width:
                break
            if len(line):
                line.append(" · ", style=THEME.fg("dim"))
            line.append_text(part)
        return line

    def render(self) -> RenderableType:
        return self.hints_line(self.size.width)
