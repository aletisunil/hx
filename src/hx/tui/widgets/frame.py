"""The prompt, framed.

Textual cannot draw text into a CSS border, so pi's editor frame - a rule
above, the input, a rule below, with the working status set into the top rule -
is built from real widgets here. The payoff is that the status has somewhere to
live that does not cost a row of layout when a turn starts.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import RenderableType
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from hx.tui.theme import THEME
from hx.tui.widgets.input import PromptInput
from hx.tui.widgets.working import WorkingIndicator, centred_rule


class BottomRule(Static):
    """The closing rule, carrying ``↓ N more`` when the draft runs past it."""

    def __init__(self) -> None:
        super().__init__(id="prompt-rule-bottom")
        self.hidden_below = 0
        self.style_muted = True

    def set_hidden_below(self, lines: int) -> None:
        if lines != self.hidden_below:
            self.hidden_below = lines
            self.refresh()

    def set_focused_style(self, focused: bool) -> None:
        if focused == self.style_muted:
            self.style_muted = not focused
            self.refresh()

    def render_rule(self, width: int) -> Text:
        style = THEME.fg("border_muted" if self.style_muted else "border_accent")
        label = f" ↓ {self.hidden_below} more " if self.hidden_below else ""
        return centred_rule(width, style, label, THEME.fg("dim"))

    def render(self) -> RenderableType:
        return self.render_rule(self.size.width)


class PromptFrame(Vertical):
    """Rule, prompt, rule - one unit, so the dock height never jitters."""

    def __init__(self, cwd: Path) -> None:
        super().__init__(id="prompt-frame")
        self._cwd = cwd

    def compose(self) -> ComposeResult:
        yield WorkingIndicator()
        yield PromptInput(self._cwd)
        yield BottomRule()
