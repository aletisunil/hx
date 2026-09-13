"""Configuration modal.

Shows what the session is actually running with, and lets the OpenRouter key be
set or replaced without quitting. A key that can only be set once, at first
run, leaves a user with a revoked key no way out but editing JSON by hand.

The key is masked on screen, entered without echo, and never written to the
transcript or the session file.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from hx.tui.theme import THEME


class ConfigureModal(ModalScreen[str | None]):
    """Session settings, and an entry field for the API key.

    Dismisses with the new key, or ``None`` when nothing changed.
    """

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "cancel", "Cancel")]

    def __init__(self, summary: list[tuple[str, str]], key_hint: str, warning: str = "") -> None:
        super().__init__()
        self.summary = summary
        self.key_hint = key_hint
        self.warning = warning

    def compose(self) -> ComposeResult:
        with Vertical(id="configure"):
            yield Static(Text("Configuration", style=THEME.fg("text", bold=True)))
            yield Static(self._summary_text(), id="configure-summary")
            if self.warning:
                yield Static(Text(self.warning, style=THEME.fg("warning")), id="configure-warning")
            yield Static(
                Text(f"OpenRouter API key: {self.key_hint}", style=THEME.fg("muted")),
                id="configure-key",
            )
            yield Input(
                placeholder="Paste a new key to replace it, or leave blank",
                password=True,
                id="configure-input",
            )
            with Horizontal(id="configure-actions"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#configure-input", Input).focus()

    def _summary_text(self) -> Text:
        body = Text()
        width = max((len(label) for label, _ in self.summary), default=0)
        for label, value in self.summary:
            body.append(f"{label:<{width}}  ", style=THEME.fg("dim"))
            body.append(f"{value}\n", style=THEME.fg("text"))
        return body

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self._save()
        else:
            self.dismiss(None)

    def _save(self) -> None:
        key = self.query_one("#configure-input", Input).value.strip()
        self.dismiss(key or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


def build_summary(app: Any) -> list[tuple[str, str]]:
    """The settings worth seeing before changing anything."""
    sandbox = app._sandbox_backend if app.sandbox_active else "none"
    return [
        ("Model", app.loop.model),
        ("Theme", app.settings.theme),
        ("Permission mode", app.mode.value),
        ("Sandbox", sandbox),
        ("Project", str(app.settings.cwd)),
    ]
