"""The startup header.

The first thing in the transcript: what this is, and the handful of keys worth
knowing before the first prompt. Expandable to the full list, because a wall of
twenty shortcuts on launch is a wall nobody reads - pi makes the same trade.
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.text import Text
from textual.widgets import Static

from hx.keys import KEYMAP
from hx.tui.theme import THEME
from hx.tui.widgets.hints import LITERAL_HINTS


class StartupHeader(Static):
    """Name, version, and the keys - compact by default, expandable."""

    def __init__(self, version: str) -> None:
        super().__init__()
        self.version = version
        self.expanded = False
        self.add_class("startup-header")

    def toggle(self) -> None:
        self.expanded = not self.expanded
        self.refresh(layout=True)

    def on_click(self) -> None:
        self.toggle()

    def _title(self) -> Text:
        title = Text("hx", style=THEME.fg("accent", bold=True))
        title.append(f" v{self.version}", style=THEME.fg("dim"))
        return title

    def _key_row(self, keys: str, description: str) -> Text:
        row = Text(f"  {keys:<16}", style=THEME.fg("dim"))
        row.append(description, style=THEME.fg("muted"))
        return row

    def render(self) -> RenderableType:
        rows: list[RenderableType] = [self._title()]

        if not self.expanded:
            # The shortcuts themselves live on the hints bar, permanently on
            # screen; repeating them here would just be the same line twice.
            rows.append(
                Text(
                    "A terminal coding agent. Ask a question, or start with /help.",
                    style=THEME.fg("muted"),
                )
            )
            rows.append(
                Text(
                    f"{KEYMAP.primary('app.tools.expand')} shows every key.",
                    style=THEME.fg("dim"),
                )
            )
            return Group(*rows)

        # Expanded: every action that has a key, in registry order.
        rows.append(Text(""))
        for action, binding in KEYMAP.bindings.items():
            keys = KEYMAP.text(action)
            if keys:
                rows.append(self._key_row(keys, binding.description))
        rows.append(Text(""))
        for literal, description in LITERAL_HINTS:
            rows.append(self._key_row(literal, description))
        return Group(*rows)
