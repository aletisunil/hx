"""Permission approval modal."""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from hx.permissions.engine import GrantScope, PermissionAnswer
from hx.tui.widgets.diff import render_diff


class PermissionModal(ModalScreen[PermissionAnswer]):
    """Approve or refuse one tool call.

    Shows exactly what will run - the full command, or the unified diff for an
    edit - before offering the choice. Options: allow once, allow for this
    session, always allow (persists a rule), or refuse.

    When the request came from a subagent the modal names it; an approval whose
    origin is unclear is not an informed approval.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        ("y", "allow_once", "Allow once"),
        ("s", "allow_session", "Allow for session"),
        ("a", "allow_always", "Always allow"),
        ("n", "deny", "Deny"),
        ("escape", "deny", "Deny"),
    ]

    def __init__(self, request: Any, origin: str | None = None) -> None:
        super().__init__()
        self.request = request
        self.origin = origin

    def compose(self) -> ComposeResult:
        with Vertical(id="permission"):
            yield Static(self._title(), id="permission-title")
            with VerticalScroll(id="permission-detail"):
                yield Static(self._detail())
            with Horizontal(id="permission-actions"):
                yield Button("Allow once (y)", variant="primary", id="once")
                yield Button("Session (s)", id="session")
                yield Button("Always (a)", id="always")
                yield Button("Deny (n)", variant="error", id="deny")

    def _title(self) -> Text:
        who = f" requested by {self.origin}" if self.origin else ""
        return Text.assemble(
            ("Permission needed", "bold"),
            (f"{who}: ", "dim"),
            (self.request.tool_name, "bold cyan"),
        )

    def _detail(self) -> Any:
        """Render what is actually about to happen.

        A prompt that hides the command or the diff is not asking for consent,
        it is asking for a reflex.
        """
        detail = getattr(self.request, "detail", "") or ""
        if detail.lstrip().startswith(("---", "+++", "@@")):
            return render_diff(detail)
        if detail:
            return Text(detail)

        specifier = getattr(self.request, "specifier", None)
        return Text(str(specifier or self.request.description))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        mapping = {
            "once": GrantScope.ONCE,
            "session": GrantScope.SESSION,
            "always": GrantScope.ALWAYS,
        }
        if event.button.id == "deny":
            self.dismiss(PermissionAnswer(allowed=False))
        else:
            self.dismiss(PermissionAnswer(True, mapping[str(event.button.id)]))

    def action_allow_once(self) -> None:
        self.dismiss(PermissionAnswer(True, GrantScope.ONCE))

    def action_allow_session(self) -> None:
        self.dismiss(PermissionAnswer(True, GrantScope.SESSION))

    def action_allow_always(self) -> None:
        self.dismiss(PermissionAnswer(True, GrantScope.ALWAYS))

    def action_deny(self) -> None:
        self.dismiss(PermissionAnswer(allowed=False))
