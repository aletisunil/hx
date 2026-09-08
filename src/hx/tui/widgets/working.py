"""The working indicator.

A spinner, what the agent is doing, how long it has been doing it, and how to
stop it. It sits directly above the prompt - the fixed dock, never the
scrollback - so it stays visible while the user reads back through the
transcript, which is exactly when they most want to know whether a turn is
still running.
"""

from __future__ import annotations

import time
from typing import ClassVar

from rich.console import RenderableType
from rich.text import Text
from textual.widgets import Static

from hx.tui.theme import THEME

FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
INTERVAL = 0.08


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

    def on_mount(self) -> None:
        """The timer runs for the app's lifetime but only repaints while busy;
        starting and stopping it per turn would race with the event stream."""
        self.set_interval(INTERVAL, self._tick)

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

    def render(self) -> RenderableType:
        if not self.busy:
            return Text("")
        label = self.LABELS.get(self.label, self.label or "Working")
        body = Text(f"{FRAMES[self._frame]} ", style=THEME.fg("accent"))
        body.append(f"{label}… ", style=THEME.fg("muted"))
        body.append(f"({self.elapsed:.0f}s · ", style=THEME.fg("dim"))
        body.append("esc", style=THEME.fg("muted"))
        body.append(" to interrupt)", style=THEME.fg("dim"))
        return body
