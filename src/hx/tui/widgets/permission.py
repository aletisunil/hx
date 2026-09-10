"""Inline permission prompt.

An approval is part of the conversation, not an interruption to it: the model
said what it was about to do, and the answer to "may it?" belongs on the next
line, where that sentence is still on screen. So this is a transcript block
that takes the keyboard, not a modal that covers the transcript.

Answering does not remove the block. It collapses in place into a one-line
record of what was granted and how widely, because a session that quietly
accumulates ``always`` rules in ``.hx/settings.local.json`` should be able to
show its work.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.text import Text
from textual.binding import Binding, BindingType
from textual.widgets import Static

from hx.permissions.engine import GrantScope, PermissionAnswer, PermissionRequest
from hx.tui.renderers import render_diff
from hx.tui.theme import THEME

MAX_DETAIL_LINES = 24
"""Lines of command or diff shown before the block offers to expand.

A 600-line diff is worth reading before approving and is not worth pasting into
the scrollback forever, where it buries every exchange that came before it.
"""

SCOPE_LABELS: dict[GrantScope, str] = {
    GrantScope.ONCE: "allowed once",
    GrantScope.SESSION: "allowed for this session",
    GrantScope.ALWAYS: "always allowed",
}
"""How each grant is described after the fact.

Spelled out rather than abbreviated: ``always`` wrote a rule to a file, and the
record of it should say so in the words the user would use to undo it.
"""


def _trimmed(renderable: RenderableType) -> RenderableType:
    """Drop a renderable's trailing blank line.

    ``render_diff`` terminates every line, itself included, which inside a tool
    block is invisible and here would put two blank rows between the diff and
    the keys instead of the one this block puts there on purpose.
    """
    if isinstance(renderable, Text):
        renderable.rstrip()
    return renderable


class PermissionPrompt(Static):
    """One pending approval, rendered in the transcript.

    Focus is what routes the keys here, rather than app-level bindings. A global
    binding on ``y`` would have to be live for the whole session and silently
    swallow the letter whenever the composer did not have focus; taking focus
    for the moment the loop is blocked anyway costs nothing and cannot misfire.
    """

    can_focus = True

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "allow_once", "Allow once"),
        Binding("s", "allow_session", "Allow for session"),
        Binding("a", "allow_always", "Always allow"),
        Binding("n", "deny", "Deny"),
        Binding("escape", "deny", "Deny"),
        Binding("v", "toggle_detail", "Show the rest"),
    ]

    def __init__(
        self,
        request: PermissionRequest,
        future: asyncio.Future[PermissionAnswer] | None = None,
        origin: str | None = None,
    ) -> None:
        super().__init__()
        self.request = request
        self.future = future
        """Resolved with the answer. Absent when the block is rendered on its own,
        which is how the tests inspect it without driving a whole turn."""
        self.origin = origin or getattr(request, "origin", None)
        self.answer: PermissionAnswer | None = None
        self.expanded = False
        self.abandoned = False
        """Set when the turn was interrupted before anyone answered."""
        self.add_class("permission-prompt")

    # -- state -------------------------------------------------------------

    @property
    def answered(self) -> bool:
        return self.answer is not None or self.abandoned

    def resolve(self, answer: PermissionAnswer) -> None:
        """Record the outcome and collapse. Idempotent, so a late second key
        press cannot overwrite the decision that was already acted on."""
        if self.answered:
            return
        self.answer = answer
        self.remove_class("permission-prompt")
        self.refresh(layout=True)
        if self.future is not None and not self.future.done():
            self.future.set_result(answer)

    def abandon(self) -> None:
        """The turn was interrupted, or the transcript went out from under this.

        It has to stop looking like a live question - the keys no longer reach
        anything - without claiming a decision nobody made.

        The future is settled as a refusal all the same. Whoever is blocked on
        it has to be released, and the one answer that is safe to give on behalf
        of a user who never answered is no.
        """
        if self.answered:
            return
        self.abandoned = True
        self.remove_class("permission-prompt")
        self.refresh(layout=True)
        if self.future is not None and not self.future.done():
            self.future.set_result(PermissionAnswer(allowed=False))

    # -- rendering ---------------------------------------------------------

    def render(self) -> RenderableType:
        """Pending, this wears the same tint a running tool block wears; answered,
        it is a plain line like any other notice.

        The tint is what makes an unanswered prompt findable in a long
        scrollback, and keeping it after the answer would leave the transcript
        looking like it were still waiting on something.
        """
        if self.answered:
            return self._record()
        body = Group(self._question(), *self._detail_lines(), Text(""), self._actions())
        return Padding(body, (0, 1), style=THEME.bg("tool_pending_bg"))

    def _question(self) -> Text:
        who = f" requested by {self.origin}" if self.origin else ""
        line = Text("? ", style=THEME.fg("warning", bold=True))
        line.append("Permission needed", style=THEME.fg("text", bold=True))
        line.append(f"{who}: ", style=THEME.fg("dim"))
        line.append(self.request.tool_name, style=THEME.fg("warning", bold=True))
        return line

    def _actions(self) -> Text:
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

    def _detail_lines(self) -> list[RenderableType]:
        """What is actually about to happen.

        A prompt that hides the command or the diff is not asking for consent,
        it is asking for a reflex.
        """
        detail = (self.request.detail or "").strip("\n")
        if not detail:
            specifier = self.request.specifier or self.request.description
            return [Text(str(specifier), style=THEME.fg("text"))]

        if detail.lstrip().startswith(("---", "+++", "@@")):
            return [_trimmed(render_diff(self._clip(detail))), *self._overflow_note(detail)]
        return [Text(self._clip(detail), style=THEME.fg("text")), *self._overflow_note(detail)]

    def _clip(self, detail: str) -> str:
        if self.expanded:
            return detail
        lines = detail.splitlines()
        return detail if len(lines) <= MAX_DETAIL_LINES else "\n".join(lines[:MAX_DETAIL_LINES])

    def _overflow_note(self, detail: str) -> list[RenderableType]:
        hidden = len(detail.splitlines()) - MAX_DETAIL_LINES
        if self.expanded or hidden <= 0:
            return []
        note = Text(f"… {hidden} more line{'s' if hidden != 1 else ''} · ", style=THEME.fg("dim"))
        note.append("v", style=THEME.fg("accent", bold=True))
        note.append(" show all", style=THEME.fg("muted"))
        return [note]

    def _record(self) -> Text:
        """The one line this collapses to once it has been answered."""
        target = self.request.specifier or self.request.description
        if self.abandoned:
            line = Text("· ", style=THEME.fg("muted"))
            line.append("not answered - the turn was interrupted", style=THEME.fg("muted"))
            line.append(f" · {target}", style=THEME.fg("dim"))
            return line

        answer = self.answer
        assert answer is not None  # answered and not abandoned
        if answer.allowed:
            line = Text("✓ ", style=THEME.fg("success", bold=True))
            line.append(SCOPE_LABELS[answer.scope], style=THEME.fg("success"))
        else:
            line = Text("✗ ", style=THEME.fg("error", bold=True))
            line.append("denied", style=THEME.fg("error"))
        line.append(f" · {target}", style=THEME.fg("dim"))
        return line

    # -- input -------------------------------------------------------------

    def on_click(self) -> None:
        """Clicking a clipped prompt shows the rest, as it does on a tool block."""
        self.action_toggle_detail()

    def action_toggle_detail(self) -> None:
        if self.answered:
            return
        self.expanded = not self.expanded
        self.refresh(layout=True)

    def action_allow_once(self) -> None:
        self.resolve(PermissionAnswer(True, GrantScope.ONCE))

    def action_allow_session(self) -> None:
        self.resolve(PermissionAnswer(True, GrantScope.SESSION))

    def action_allow_always(self) -> None:
        self.resolve(PermissionAnswer(True, GrantScope.ALWAYS))

    def action_deny(self) -> None:
        self.resolve(PermissionAnswer(allowed=False))
