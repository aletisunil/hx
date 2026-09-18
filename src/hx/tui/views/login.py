"""Signing in, and configuring a key.

These two screens referenced five CSS ids that appeared in no stylesheet, so
they rendered with no border, no padding and no width constraint for as long as
they existed - and they were the only place in the app using stock Textual
buttons. Built on the shared dialog grammar they cannot drift that way again:
there is nothing to reference and nothing to forget.

The flow contract they implement is kept exactly. Its methods can be called
before anything is on screen, so each records state and repaints rather than
touching a widget - which is the fix for a crash where the flow spoke first.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from hx.term.component import Component, Widget
from hx.term.primitives import Lines, Rule, Spacer, Text
from hx.tui.glyphs import CURRENT, CURSOR, GUTTER
from hx.tui.paint import fg, link, rule
from hx.tui.views.dialog import Framed, Hint, hints_line


class ProviderDialog(Widget, Framed):
    """Which account to sign in to."""

    def __init__(self, providers: list[tuple[str, str, bool]]) -> None:
        """``providers`` is ``(value, label, has_credential)``."""
        super().__init__()
        self.providers = providers
        self.selected = 0
        self.result: str | None = None
        self.done = False

    def draw(self, width: int) -> list[str]:
        rows = []
        for index, (_value, label, present) in enumerate(self.providers):
            chosen = index == self.selected
            cursor = fg("accent", CURSOR) if chosen else GUTTER
            marker = fg("accent", CURRENT) if present else GUTTER
            rows.append(cursor + marker + fg("accent" if chosen else "text", label))

        parts = [
            Rule(rule("border")),
            Spacer(1),
            Text(fg("accent", "Sign in", bold=True), 1, 0),
            Spacer(1),
            Lines(rows),
            Spacer(1),
            Text(
                hints_line([Hint("↑↓", "move"), Hint("enter", "select"), Hint("esc", "cancel")]),
                1,
                0,
            ),
            *self.closing(),
        ]
        return [line for part in parts for line in part.render(width)]

    def handle_input(self, key: str, data: str) -> bool:
        if self.done or not self.providers:
            return False
        if key in ("up", "down"):
            step = 1 if key == "down" else -1
            self.selected = (self.selected + step) % len(self.providers)
            self.invalidate()
            return True
        if key == "enter":
            self.result = self.providers[self.selected][0]
            self.done = True
            return True
        if key == "escape":
            self.result = None
            self.done = True
            return True
        return False


class LoginDialog(Widget, Framed):
    """A sign-in flow in progress.

    Implements the flow's interaction protocol. Every method records state and
    repaints instead of reaching for a widget, because a flow can talk before
    this is on screen - which is exactly how it used to crash.
    """

    def __init__(self, title: str = "Sign in", on_change: Callable[[], None] | None = None) -> None:
        """``on_change`` asks for a frame. The flow updates this dialog from a
        background task, where no keystroke will draw it."""
        super().__init__()
        self.title = title
        self._on_change = on_change
        self._instructions = "Starting…"
        """What to do, from the flow. Kept for the whole sign-in: the route's
        own advice (Devin's Enterprise button) matters most while it is waiting."""
        self._note = ""
        """The latest progress line - a busy port, the token exchange."""
        self._highlight = ""
        self._highlight_url = ""
        self._paste_label = ""
        self._pasteable = False
        self._typed = ""
        self._paste_future: asyncio.Future[str] | None = None
        self.cancelled = False

    def _changed(self) -> None:
        self.invalidate()
        if self._on_change is not None:
            self._on_change()

    # -- the flow's protocol ----------------------------------------------

    def show_url(self, url: str, instructions: str) -> None:
        self._highlight = url
        self._highlight_url = url
        self._instructions = instructions
        self._changed()

    def show_device_code(self, user_code: str, verification_uri: str) -> None:
        self._instructions = f"Open {verification_uri} and enter this code:"
        self._highlight = user_code
        self._highlight_url = verification_uri
        # Nothing to paste in this flow; the poll decides when it is done.
        self._pasteable = False
        self._changed()

    def progress(self, message: str) -> None:
        self._note = message
        self._changed()

    async def prompt_paste(self, message: str) -> str:
        """Resolve when the user submits, never otherwise.

        The browser callback usually wins, in which case this future is simply
        cancelled along with the rest of the flow.
        """
        self._paste_label = message
        self._pasteable = True
        self._typed = ""
        self._changed()
        self._paste_future = asyncio.get_running_loop().create_future()
        return await self._paste_future

    # -- rendering ---------------------------------------------------------

    def draw(self, width: int) -> list[str]:
        # Prose wraps; the URL is a link and is cut to the width instead, since
        # the whole of it is still what gets opened.
        parts: list[Component] = [
            Rule(rule("border")),
            Spacer(1),
            Text(fg("accent", self.title, bold=True), 1, 0),
            Spacer(1),
            Text(fg("muted", self._instructions), 1, 0),
        ]
        if self._highlight:
            parts.append(
                Lines(
                    [
                        link(self._highlight_url, self._highlight)
                        if self._highlight_url
                        else fg("accent", self._highlight, bold=True)
                    ]
                )
            )
        if self._note:
            parts += [Spacer(1), Text(fg("text", self._note), 1, 0)]
        if self._pasteable:
            parts += [
                Spacer(1),
                Text(fg("muted", self._paste_label), 1, 0),
                Lines([fg("text", self._typed or "") + _cursor_cell()]),
            ]

        hints = [Hint("esc", "cancel")]
        if self._pasteable:
            hints.insert(0, Hint("enter", "submit"))
        parts += [Spacer(1), Text(hints_line(hints), 1, 0), *self.closing()]
        return [line for part in parts for line in part.render(width)]

    def handle_input(self, key: str, data: str) -> bool:
        if key == "escape":
            self.cancelled = True
            if self._paste_future is not None and not self._paste_future.done():
                self._paste_future.cancel()
            return True
        if not self._pasteable:
            return False
        if key in ("text", "paste"):
            self._typed += data.replace("\n", "").strip()
            self.invalidate()
            return True
        if key == "backspace":
            self._typed = self._typed[:-1]
            self.invalidate()
            return True
        if key == "enter" and self._paste_future is not None:
            if not self._paste_future.done():
                self._paste_future.set_result(self._typed.strip())
            self._pasteable = False
            self.invalidate()
            return True
        return False


class ConfigureDialog(Widget, Framed):
    """Set the API key for a provider."""

    def __init__(self, title: str, summary: list[tuple[str, str]], warning: str = "") -> None:
        super().__init__()
        self.title = title
        self.summary = summary
        self.warning = warning
        self._typed = ""
        self.result: str | None = None
        self.done = False

    def draw(self, width: int) -> list[str]:
        from hx.tui.format import columns

        body: list[str] = columns(
            [[fg("muted", label), fg("text", value)] for label, value in self.summary]
        )
        if self.warning:
            body.append(fg("warning", self.warning))
        body.append("")
        body.append(fg("muted", "API key: ") + fg("text", "•" * len(self._typed)) + _cursor_cell())

        parts = [
            Rule(rule("border")),
            Spacer(1),
            Text(fg("accent", self.title, bold=True), 1, 0),
            Spacer(1),
            Lines(body),
            Spacer(1),
            Text(hints_line([Hint("enter", "save"), Hint("esc", "cancel")]), 1, 0),
            *self.closing(),
        ]
        return [line for part in parts for line in part.render(width)]

    def handle_input(self, key: str, data: str) -> bool:
        if self.done:
            return False
        if key in ("text", "paste"):
            self._typed += data.replace("\n", "").strip()
            self.invalidate()
            return True
        if key == "backspace":
            self._typed = self._typed[:-1]
            self.invalidate()
            return True
        if key == "enter":
            self.result = self._typed.strip() or None
            self.done = True
            return True
        if key == "escape":
            self.result = None
            self.done = True
            return True
        return False


def _cursor_cell() -> str:
    from hx.term.ansi import inverse
    from hx.term.screen import CURSOR_MARKER

    return CURSOR_MARKER + inverse(" ")


def provider_rows(auth: Any) -> list[tuple[str, str, bool]]:
    """``(value, label, has_credential)`` for each provider that can be used."""
    rows: list[tuple[str, str, bool]] = []
    for provider in getattr(auth, "providers", lambda: [])():
        value = getattr(provider, "id", str(provider))
        label = getattr(provider, "label", value)
        present = bool(getattr(provider, "has_credential", False))
        rows.append((value, label, present))
    return rows
