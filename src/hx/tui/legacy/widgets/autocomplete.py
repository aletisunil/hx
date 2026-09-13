"""Completion popup above the prompt.

Two triggers, both of which HX already advertised and only half delivered: a
leading ``/`` completes slash commands, and ``@`` completes paths. The popup
replaces the old behaviour of silently filling in a path when - and only when -
exactly one file matched, which meant the feature appeared broken every time
the user had two.

The widget is presentation only. The prompt owns the state, because the prompt
is what the keys arrive at.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.console import Group, RenderableType
from rich.text import Text
from textual.widgets import Static

from hx.tui.theme import THEME

MAX_VISIBLE = 8


@dataclass
class Candidate:
    """One row: what gets inserted, and what the row says."""

    value: str
    label: str
    detail: str = ""


@dataclass
class Completion:
    """An open completion session over the prompt's current token."""

    #: Offset in the prompt text where the replaced token starts.
    start: int
    #: Text inserted before the value, ``/`` or ``@``.
    prefix: str
    candidates: list[Candidate] = field(default_factory=list)
    index: int = 0

    def move(self, delta: int) -> None:
        if not self.candidates:
            return
        self.index = (self.index + delta) % len(self.candidates)

    @property
    def current(self) -> Candidate | None:
        if not self.candidates:
            return None
        return self.candidates[self.index]


class Autocomplete(Static):
    """Renders the open completion, or nothing at all."""

    def __init__(self) -> None:
        super().__init__(id="autocomplete")
        self.completion: Completion | None = None
        self.display = False

    def show(self, completion: Completion | None) -> None:
        self.completion = completion if completion and completion.candidates else None
        self.display = self.completion is not None
        self.refresh(layout=True)

    def render(self) -> RenderableType:
        completion = self.completion
        if completion is None:
            return Text("")

        candidates = completion.candidates
        # Keep the selection on screen without letting the popup grow past the
        # window: scroll the visible slice around the cursor, as the pickers do.
        start = max(0, min(completion.index - MAX_VISIBLE // 2, len(candidates) - MAX_VISIBLE))
        start = max(0, start)
        visible = candidates[start : start + MAX_VISIBLE]

        # Pad against every candidate, not just the visible ones, so the
        # descriptions do not slide sideways as the list scrolls.
        label_width = max((len(item.label) for item in candidates), default=0)

        rows: list[RenderableType] = []
        for offset, candidate in enumerate(visible):
            selected = start + offset == completion.index
            row = Text()
            marker = "› " if selected else "  "  # noqa: RUF001 - pi's selection marker
            row.append(marker, style=THEME.fg("accent"))
            label = candidate.label.ljust(label_width) if candidate.detail else candidate.label
            row.append(label, style=THEME.fg("text", bold=selected))
            if candidate.detail:
                row.append(f"  {candidate.detail}", style=THEME.fg("muted"))
            if selected:
                row.stylize(THEME.bg("selected_bg"))
            rows.append(row)

        if len(candidates) > len(visible):
            rows.append(
                Text(f"  ({completion.index + 1}/{len(candidates)})", style=THEME.fg("dim"))
            )
        return Group(*rows)
