"""The shape every dialog in HX has.

One grammar, taken from pi::

    ─────────────────────────────────────────────
                                                    <- Spacer(1)
     Title                                          <- accent, bold
     subtitle or context                            <- muted

     body text

     → y  the selected option
       s  another option

     key hint  key hint  key hint                   <- dim key, muted label

    ─────────────────────────────────────────────

A rule above and below, never a rectangle. The old UI had five different border
treatments, and two dialogs that were the same kind of object - a centred panel
with a title and a list - drew their frames in different colours, because CSS
made it free to differ. There is one :class:`~hx.term.primitives.Rule` here and
nothing else, so they cannot.

Options carry two markers in a two-column gutter: where Enter will land, and
what is already in force. Collapsing those into one is why a picker row used to
signal its highlight three separate ways at once.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from hx.term.component import Component, Widget
from hx.term.primitives import Lines, Rule, Spacer, Text
from hx.tui.glyphs import CURRENT, CURSOR, GUTTER
from hx.tui.paint import fg, rule


@dataclass(frozen=True, slots=True)
class Option:
    """One row of a dialog's list."""

    key: str = ""
    """A single key that answers immediately, for a dialog that offers one."""

    label: str = ""
    note: str = ""
    """Consequence worth stating - what this option will actually do."""

    value: object = None
    current: bool = False
    """Already in force. Marked separately from being selected."""


@dataclass(frozen=True, slots=True)
class Hint:
    key: str
    description: str


def hints_line(hints: Sequence[Hint]) -> str:
    """``esc deny  enter confirm`` - dim key, muted description, two spaces.

    One shape everywhere, so a user learns "here is what you can press" once.
    """
    return "  ".join(fg("dim", h.key) + fg("muted", f" {h.description}") for h in hints)


class Framed:
    """The rule above, the rule below, and the one case that drops the lower one.

    Mixed into every dialog, because the seam they share is where it went
    wrong: the dock draws a blank line and then the prompt's own rule directly
    under whatever is on the overlay, so a dialog that closed itself with a
    rule produced rule, blank, rule - three lines of frame for one edge, which
    reads as a rendering fault rather than as a frame.
    """

    docked: bool = False
    """Drawn directly above the dock, which supplies the closing rule."""

    def set_docked(self, docked: bool) -> None:
        if docked == self.docked:
            return
        self.docked = docked
        self.invalidate()  # type: ignore[attr-defined]  # always mixed into a Widget

    def closing(self, border: str = "border") -> list[Component]:
        """The bottom of the frame: nothing at all when the dock is under it."""
        if self.docked:
            return []
        return [Spacer(1), Rule(rule(border))]


@dataclass
class Dialog(Widget, Framed):
    """A framed block of title, body, options and hints."""

    title: str = ""
    subtitle: str = ""
    body: list[str] = field(default_factory=list)
    options: list[Option] = field(default_factory=list)
    hints: list[Hint] = field(default_factory=list)
    selected: int = 0
    border: str = "border"

    def __post_init__(self) -> None:
        Widget.__init__(self)

    def draw(self, width: int) -> list[str]:
        parts: list[object] = [Rule(rule(self.border)), Spacer(1)]

        if self.title:
            parts.append(Text(self.title, 1, 0))
        if self.subtitle:
            parts.append(Text(self.subtitle, 1, 0))
        if self.title or self.subtitle:
            parts.append(Spacer(1))

        if self.body:
            parts.append(Lines(self.body))
            parts.append(Spacer(1))

        if self.options:
            parts.extend(self._rows())
            parts.append(Spacer(1))

        if self.hints:
            parts.append(Text(hints_line(self.hints), 1, 0))

        parts.extend(self.closing(self.border))
        return [line for part in parts for line in part.render(width)]  # type: ignore[attr-defined]

    def _rows(self) -> list[Text]:
        rows = []
        for index, option in enumerate(self.options):
            chosen = index == self.selected
            cursor = fg("accent", CURSOR) if chosen else GUTTER
            current = (
                fg("accent", CURRENT)
                if option.current
                else GUTTER
                if _any_current(self.options)
                else ""
            )
            # Weight follows selection, never meaning: an option is not
            # emphasised for being the dangerous one, because then the eye
            # learns to go there.
            key = fg("accent" if chosen else "dim", option.key) + "  " if option.key else ""
            label = fg("accent" if chosen else "text", option.label)
            note = fg("dim", f" · {option.note}") if option.note else ""
            rows.append(Text(f"{cursor}{current}{key}{label}{note}", 1, 0))
        return rows

    # -- selection ---------------------------------------------------------

    def move(self, delta: int) -> None:
        if not self.options:
            return
        self.selected = (self.selected + delta) % len(self.options)
        self.invalidate()

    def select(self, index: int) -> None:
        self.selected = max(0, min(len(self.options) - 1, index))
        self.invalidate()

    @property
    def choice(self) -> Option | None:
        if not self.options:
            return None
        return self.options[self.selected]

    def by_key(self, key: str) -> Option | None:
        for option in self.options:
            if option.key and option.key == key:
                return option
        return None


def _any_current(options: Sequence[Option]) -> bool:
    return any(option.current for option in options)
