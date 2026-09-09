"""The working indicator, drawn into the prompt's top border.

A spinner, what the agent is doing, how long it has been doing it, and how to
stop it - riding the rule that frames the prompt, the way pi does it. Putting
it in the border rather than on a line of its own means the layout does not
shift by a row every time a turn starts, and the status sits against the thing
the user is about to type into.

The same rule carries ``↑ N more`` when the draft has scrolled out of view, so
one line does the work of a border, a status and a scroll indicator.
"""

from __future__ import annotations

import time
from typing import ClassVar

from rich.console import RenderableType
from rich.text import Text
from textual.widgets import Static

from hx.keys import KEYMAP
from hx.tui.theme import THEME

FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
INTERVAL = 0.08


def centred_rule(width: int, style: str, label: str, label_style: str) -> Text:
    """``────── ↓ 4 more ──────`` - a rule with a label set into the middle.

    Falls back to a plain rule when the label would not fit, because a label
    squeezed against the edge reads as a rendering bug.
    """
    if width <= 0:
        return Text("")
    if not label or width < len(label) + 2:
        return Text("─" * width, style=style)
    lead = max(1, (width - len(label)) // 2)
    line = Text("─" * lead, style=style)
    line.append(label, style=label_style)
    line.append("─" * (width - lead - len(label)), style=style)
    return line


class WorkingIndicator(Static):
    """Animated while a turn runs, blank the rest of the time."""

    LABELS: ClassVar[dict[str, str]] = {
        "thinking": "Thinking",
        "compacting": "Compacting",
    }

    def __init__(self) -> None:
        super().__init__(id="working")
        self.busy = False
        self.label = ""
        self._frame = 0
        self._started = 0.0
        #: Lines of the draft scrolled off the top of the prompt.
        self.hidden_above = 0
        #: Dimmed while the prompt is unfocused, so focus is visible at a glance.
        self.style_muted = True

    def on_mount(self) -> None:
        """The timer runs for the app's lifetime but only repaints while busy;
        starting and stopping it per turn would race with the event stream."""
        self.set_interval(INTERVAL, self._tick)

    def set_hidden_above(self, lines: int) -> None:
        if lines != self.hidden_above:
            self.hidden_above = lines
            self.refresh()

    def set_focused_style(self, focused: bool) -> None:
        if focused == self.style_muted:
            self.style_muted = not focused
            self.refresh()

    def _tick(self) -> None:
        if not self.busy:
            return
        self._frame = (self._frame + 1) % len(FRAMES)
        self.refresh()

    def start(self, label: str = "") -> None:
        if not self.busy:
            self._started = time.monotonic()
        self.busy = True
        self.label = label
        self.set_class(True, "busy")
        self.refresh()

    def stop(self) -> None:
        self.busy = False
        self.label = ""
        self.set_class(False, "busy")
        self.refresh()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started if self.busy else 0.0

    def status(self) -> Text:
        """``⠹ Thinking… (12s · esc to interrupt)``, or empty when idle."""
        if not self.busy:
            return Text("")
        label = self.LABELS.get(self.label, self.label or "Working")
        interrupt = KEYMAP.primary("app.interrupt")
        body = Text(f"{FRAMES[self._frame]} ", style=THEME.fg("accent"))
        body.append(f"{label}… ", style=THEME.fg("muted"))
        body.append(f"({self.elapsed:.0f}s · ", style=THEME.fg("dim"))
        body.append(interrupt, style=THEME.fg("muted"))
        body.append(" to interrupt)", style=THEME.fg("dim"))
        return body

    def spinner(self) -> Text:
        """Just the spinner, for a rule too narrow to hold the whole status."""
        return Text(FRAMES[self._frame], style=THEME.fg("accent")) if self.busy else Text("")

    def render_rule(self, width: int) -> Text:
        """The top rule at ``width``: dashes, with the status set into them.

        Three cases, in pi's order of preference: the full status inline; the
        status plus a centred overflow label when both fit; and the spinner
        alone when the terminal is too narrow for either.
        """
        rule_style = THEME.fg("border_muted" if self.style_muted else "border_accent")
        if width <= 0:
            return self.status()

        overflow = f" ↑ {self.hidden_above} more " if self.hidden_above else ""
        status = self.status()
        if len(status) and width < len(status) + 5:
            status = self.spinner()

        line = Text()
        if len(status):
            line.append("── ", style=rule_style)
            line.append_text(status)
            line.append(" ", style=rule_style)

        remaining = width - len(line)
        if remaining <= 0:
            return line

        line.append_text(centred_rule(remaining, rule_style, overflow, THEME.fg("dim")))
        return line

    def render(self) -> RenderableType:
        return self.render_rule(self.size.width)
