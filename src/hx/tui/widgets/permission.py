"""Permission approval modal."""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from hx.permissions.engine import GrantScope, PermissionAnswer
from hx.tui.renderers import render_diff
from hx.tui.theme import THEME
from hx.tui.widgets.rule import Rule


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
            yield Rule(id="permission-rule", role="warning")
            with VerticalScroll(id="permission-detail"):
                yield Static(self._detail())
            yield Static(self._actions(), id="permission-actions")

    def _title(self) -> Text:
        who = f" requested by {self.origin}" if self.origin else ""
        return Text.assemble(
            ("Permission needed", THEME.fg("text", bold=True)),
            (f"{who}: ", THEME.fg("dim")),
            (self.request.tool_name, THEME.fg("warning", bold=True)),
        )

    def _actions(self) -> Text:
        """``y allow once · s session · a always · n deny``."""
        choices = (
            ("y", "allow once", "success"),
            ("s", "session", "text"),
            ("a", "always", "text"),
            ("n", "deny", "error"),
        )
        line = Text()
        for index, (key, label, role) in enumerate(choices):
            if index:
                line.append(" · ", style=THEME.fg("dim"))
            line.append(key, style=THEME.fg(role, bold=True))
            line.append(f" {label}", style=THEME.fg("muted"))
        return line

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

    def action_allow_once(self) -> None:
        self.dismiss(PermissionAnswer(True, GrantScope.ONCE))

    def action_allow_session(self) -> None:
        self.dismiss(PermissionAnswer(True, GrantScope.SESSION))

    def action_allow_always(self) -> None:
        self.dismiss(PermissionAnswer(True, GrantScope.ALWAYS))

    def action_deny(self) -> None:
        self.dismiss(PermissionAnswer(allowed=False))
