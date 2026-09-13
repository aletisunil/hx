"""Asking permission.

An approval is part of the conversation, not an interruption to it: the model
said what it was about to do, and the answer to "may it?" belongs on the next
line, where that sentence is still on screen. So this is a transcript block
that takes the keyboard, not a modal that covers the transcript.

It is drawn in the dialog grammar - a rule above and below, a title, the thing
being asked about, the options, a hint line - which is a change of shape from
what came before. The old prompt wore the same tint as a running tool call and
crammed four options onto one line, so the single most consequential block on
screen had the weight of the most routine one.

Answering does not remove the block. It collapses in place into a one-line
record of what was granted and how widely, because a session that quietly
accumulates ``always`` rules in ``.hx/settings.local.json`` should be able to
show its work.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from hx.permissions.engine import GrantScope, PermissionAnswer, PermissionRequest
from hx.term.component import Widget
from hx.term.primitives import Text
from hx.tui.format import one_line
from hx.tui.glyphs import NOTICE, TODO_DONE, TOOL_FAILED
from hx.tui.limits import PREVIEW_LINES, RECORD_WIDTH
from hx.tui.paint import fg
from hx.tui.renderers import ToolCall, expand_note, looks_like_diff, render_diff, renderer_for
from hx.tui.views.dialog import Dialog, Hint, Option

SCOPE_LABELS: dict[GrantScope, str] = {
    GrantScope.ONCE: "allowed once",
    GrantScope.SESSION: "allowed for this session",
    GrantScope.ALWAYS: "always allowed",
}
"""How each grant is described after the fact.

Spelled out rather than abbreviated: ``always`` wrote a rule to a file, and the
record of it should say so in the words the user would use to undo it.
"""

CHOICES: tuple[tuple[str, GrantScope | None, str, str], ...] = (
    ("y", GrantScope.ONCE, "allow once", ""),
    ("s", GrantScope.SESSION, "allow for this session", ""),
    ("a", GrantScope.ALWAYS, "always allow", "writes a rule to .hx/settings.local.json"),
    ("n", None, "deny", ""),
)
"""The four answers, in the order they are offered.

``always`` carries its consequence in words rather than in colour. Emphasis
here follows what is *selected*, never what is dangerous - an option styled to
stand out is one the eye learns to go to, which is the opposite of what a
permission prompt is for.
"""


class PermissionPrompt(Widget):
    """One pending approval, rendered in the transcript."""

    def __init__(
        self,
        request: PermissionRequest,
        future: asyncio.Future[PermissionAnswer] | None = None,
        origin: str | None = None,
        cwd: Path | None = None,
    ) -> None:
        super().__init__()
        self.request = request
        self.cwd = cwd or Path.cwd()
        """So a path in the detail is shown the way the transcript shows it -
        relative to the project, rather than as forty characters of prefix."""
        self.future = future
        """Resolved with the answer. Absent when the block is rendered on its
        own, which is how the tests inspect it without driving a whole turn."""
        self.origin = origin or getattr(request, "origin", None)
        self.answer: PermissionAnswer | None = None
        self.expanded = False
        self.abandoned = False
        """Set when the turn was interrupted before anyone answered."""
        self.selected = 0

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
        self.invalidate()
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
        self.invalidate()
        if self.future is not None and not self.future.done():
            self.future.set_result(PermissionAnswer(allowed=False))

    # -- rendering ---------------------------------------------------------

    def draw(self, width: int) -> list[str]:
        if self.answered:
            # The same one-column indent the pending block has, so answering
            # does not shift the text sideways.
            return Text(self._record(), 1, 0).render(width)

        dialog = Dialog(
            title=fg("text", "Permission needed", bold=True) + self._who(),
            subtitle=fg("warning", self.request.tool_name, bold=True),
            body=self._detail(),
            options=[
                Option(key=key, label=label, note=note) for key, _scope, label, note in CHOICES
            ],
            hints=self._hints(),
            selected=self.selected,
            border="warning",
        )
        return dialog.render(width)

    def _who(self) -> str:
        if not self.origin:
            return ""
        return fg("dim", f"  (requested by {self.origin})")

    def _detail(self) -> list[str]:
        """What is actually about to happen.

        Drawn by the tool's own renderer, so a command keeps the ``$`` it will
        have in the transcript and an edit keeps its line numbers. A prompt
        that describes the thing differently from the way it is about to be
        shown is asking for a reflex, not consent.
        """
        lines = self._detail_lines()
        if self.expanded or len(lines) <= PREVIEW_LINES:
            return lines
        return [*lines[:PREVIEW_LINES], expand_note(len(lines) - PREVIEW_LINES)]

    def _detail_lines(self) -> list[str]:
        detail = (self.request.detail or "").strip("\n")
        kind = getattr(self.request, "detail_kind", "text")

        if not detail:
            specifier = self.request.specifier or self.request.description
            return [fg("text", str(specifier))]

        if kind == "diff" or (kind != "command" and looks_like_diff(detail)):
            return render_diff(detail)

        call = ToolCall(
            name=self.request.tool_name,
            params=dict(self.request.params or {}),
            cwd=self.cwd,
        )
        if kind == "command":
            return renderer_for(self.request.tool_name).detail(call)
        return [fg("text", line) for line in detail.split("\n")]

    def _hints(self) -> list[Hint]:
        """Every key that does something, including the two the old prompt
        bound and never mentioned."""
        from hx.keys import primary_key

        return [
            Hint("y/s/a/n", "answer"),
            Hint("↑↓", "select"),
            Hint("enter", "confirm"),
            Hint("esc", "deny"),
            Hint(primary_key("app.tools.expand"), "expand"),
        ]

    def _record(self) -> str:
        """The one line this collapses to once it has been answered."""
        target = one_line(str(self.request.specifier or self.request.description), RECORD_WIDTH)
        if self.abandoned:
            return (
                fg("muted", NOTICE["info"])
                + fg("muted", "not answered - the turn was interrupted")
                + fg("dim", f" · {target}")
            )

        answer = self.answer
        assert answer is not None  # answered and not abandoned
        if answer.allowed:
            head = fg("success", f"{TODO_DONE} ", bold=True) + fg(
                "success", SCOPE_LABELS[answer.scope]
            )
        else:
            head = fg("error", f"{TOOL_FAILED} ", bold=True) + fg("error", "denied")
        return head + fg("dim", f" · {target}")

    # -- input -------------------------------------------------------------

    def handle_input(self, key: str, data: str) -> bool:
        if self.answered:
            return False

        # A printable key arrives as "text" with the character in data; the
        # single-key answers are printable, so that is where to look for them.
        pressed = data if key == "text" and len(data) == 1 else key

        for index, (choice, scope, _label, _note) in enumerate(CHOICES):
            if pressed == choice:
                self.selected = index
                self.resolve(PermissionAnswer(scope is not None, scope or GrantScope.ONCE))
                return True

        if pressed in ("up", "down"):
            self.selected = (self.selected + (1 if pressed == "down" else -1)) % len(CHOICES)
            self.invalidate()
            return True
        if pressed == "enter":
            _choice, scope, _label, _note = CHOICES[self.selected]
            self.resolve(PermissionAnswer(scope is not None, scope or GrantScope.ONCE))
            return True
        if pressed == "escape":
            self.resolve(PermissionAnswer(allowed=False))
            return True

        from hx.keys import KEYMAP

        if pressed in KEYMAP.keys_for("app.tools.expand") or pressed == "v":
            self.expanded = not self.expanded
            self.invalidate()
            return True
        return False
