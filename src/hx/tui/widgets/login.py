"""Sign-in modals.

Two screens: a provider chooser, and the one that runs an OAuth flow. The flow
screen is where the browser callback and the paste field race each other - over
SSH the browser opens on the wrong machine and can never reach the loopback
port, so pasting the redirect URL has to be a first-class path, not a fallback
buried in an error message.

No token ever reaches the transcript or the screen.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from hx.tui.theme import THEME


class ProviderPicker(ModalScreen[str | None]):
    """Choose which provider to sign in to. Dismisses with a provider id."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "cancel", "Cancel")]

    def __init__(self, options: list[tuple[str, str, str]]) -> None:
        """``options`` is ``(provider_id, label, status)`` in display order."""
        super().__init__()
        self.options = options

    def compose(self) -> ComposeResult:
        with Vertical(id="login"):
            yield Static(Text("Sign in", style=THEME.fg("text", bold=True)))
            with Vertical(id="login-options"):
                for provider_id, label, status in self.options:
                    yield Button(f"{label}  -  {status}", id=f"provider-{provider_id}")
            yield Button("Cancel", id="provider-cancel")

    def on_mount(self) -> None:
        buttons = self.query(Button)
        if buttons:
            buttons.first().focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "provider-cancel":
            self.dismiss(None)
            return
        self.dismiss(button_id.removeprefix("provider-"))

    def action_cancel(self) -> None:
        self.dismiss(None)


class LoginModal(ModalScreen[None]):
    """Runs one OAuth flow and reports progress.

    The screen *is* the :class:`~hx.auth.oauth.codex.LoginInteraction`: the flow
    calls into it to show the URL and to ask for a pasted code, and it stays up
    until the flow resolves.

    Every ``LoginInteraction`` call is recorded as state and rendered from it,
    so a flow that starts talking before the screen has composed is not an
    error. It used to be: the first call raised ``NoMatches`` and killed the
    flow, leaving a modal that took keystrokes and could never finish.
    """

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "cancel", "Cancel")]

    def __init__(self, label: str) -> None:
        super().__init__()
        self.label = label
        self._paste: asyncio.Future[str] | None = None
        self._status = "Starting…"
        self._highlight = ""
        """URL or device code - whichever this flow has to show."""
        self._pasteable = True

    def compose(self) -> ComposeResult:
        with Vertical(id="login"):
            yield Static(Text(f"Signing in to {self.label}", style=THEME.fg("text", bold=True)))
            yield Static(Text(self._status, style=THEME.fg("muted")), id="login-status")
            yield Static(Text("", style=THEME.fg("accent")), id="login-url")
            yield Input(
                placeholder="Paste the redirect URL or authorization code, then Enter",
                id="login-input",
            )
            with Horizontal(id="login-actions"):
                yield Button("Cancel", id="login-cancel")

    def on_mount(self) -> None:
        self._repaint()
        self.query_one("#login-input", Input).focus()

    # --- LoginInteraction -------------------------------------------------

    def show_url(self, url: str, instructions: str) -> None:
        self._highlight = url
        self._status = instructions
        self._repaint()

    def show_device_code(self, user_code: str, verification_uri: str) -> None:
        self._status = f"Open {verification_uri} and enter this code:"
        self._highlight = user_code
        # Nothing to paste in this flow; the poll decides when it is done.
        self._pasteable = False
        self._repaint()

    def progress(self, message: str) -> None:
        self._status = message
        self._repaint()

    async def prompt_paste(self, message: str) -> str:
        """Resolve when the user submits the input, never otherwise.

        The browser callback usually wins, in which case this future is simply
        cancelled with the rest of the flow.
        """
        self._paste = asyncio.get_running_loop().create_future()
        return await self._paste

    # --- events -----------------------------------------------------------

    def _repaint(self) -> None:
        """Paint the recorded state, or do nothing until there is a screen to paint.

        The flow and the mount race, and the flow must win either way: it is the
        only one of the two that can fail the login.
        """
        if not self.is_mounted:
            return
        self.query_one("#login-status", Static).update(Text(self._status, style=THEME.fg("muted")))
        bold = not self._pasteable
        self.query_one("#login-url", Static).update(
            Text(self._highlight, style=THEME.fg("accent", bold=bold))
        )
        self.query_one("#login-input", Input).display = self._pasteable

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value:
            return
        if self._paste is None or self._paste.done():
            # Nothing is waiting on this, so pretending it was accepted would
            # be a lie the user only discovers by waiting.
            self._status = "This sign-in is no longer waiting for a code. Press Escape."
            self._repaint()
            return
        self._paste.set_result(value)
        event.input.value = ""
        self._status = "Exchanging the authorization code…"
        self._repaint()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.action_cancel()

    def action_cancel(self) -> None:
        if self._paste is not None and not self._paste.done():
            self._paste.cancel()
        self.dismiss(None)


def provider_options(specs: Any, resolver: Any) -> list[tuple[str, str, str]]:
    """Label each route with whether it already has a credential."""
    options = []
    for spec in specs:
        status = "signed in" if resolver.has_credential(spec.id) else "not signed in"
        options.append((spec.id, spec.label, status))
    return options
