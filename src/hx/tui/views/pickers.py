"""Filtered lists: slash commands, models, effort, sessions, rewind points.

All of them are the same object - a title, a line to type on, and a list - so
they are all the same shape. The old UI drew two of these with different border
colours because CSS made it free to differ; there is one dialog grammar here
and they cannot.

Rows are laid out by measuring the data rather than padding to a guessed width.
Eleven such guesses used to be scattered across this file, each one a bet about
the longest id or title that would ever appear.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

from hx.core.usage import format_tokens
from hx.term.component import Widget
from hx.term.primitives import Lines, Rule, Spacer, Text
from hx.term.sanitize import plain_text
from hx.tui.format import columns, one_line
from hx.tui.fuzzy import filter_items
from hx.tui.glyphs import CURRENT, CURSOR, GUTTER
from hx.tui.limits import LIST_MINIMUM, LIST_VISIBLE, RECORD_WIDTH
from hx.tui.paint import fg, rule
from hx.tui.views.dialog import Framed, Hint, hints_line


class Picker(Widget, Framed):
    """A filter box over a list. Typing narrows, Enter picks, Escape cancels.

    The visible window is centred on the selection, so moving through a long
    list scrolls rather than paging, and the row Enter will take is always on
    screen. A list where nothing is marked gives no clue what Enter does.
    """

    title: str = ""
    placeholder: str = "Filter…"

    CHROME = 9
    """Lines the frame spends on itself: two rules, four blank lines, the
    title, the filter line and the hint line. Counted rather than guessed,
    because it is what the list is then measured against."""

    def __init__(self, initial: str = "") -> None:
        super().__init__()
        # Pre-filled so a query typed as `/model opus` stays visible and
        # editable, rather than silently narrowing a list whose reason for
        # being short the user cannot see.
        self.query = initial
        self.selected = 0
        self.result: str | None = None
        self.done = False
        self._rows: list[tuple[str, list[str]]] = []
        self._rows_available: Any = None
        """Rows the overlay may use, supplied by whoever put this on screen.

        Without it the list falls back to :data:`LIST_VISIBLE`, which is what
        every picker used to show on every terminal: eight rows of a
        two-hundred model catalogue, with two thirds of a tall window empty
        underneath."""
        self._refresh()

    # -- the room this has to work with ------------------------------------

    def set_rows_available(self, rows: Any) -> None:
        """Tell the picker how many rows the overlay has. ``rows`` is called
        at draw time, so a resize is picked up without re-opening anything."""
        self._rows_available = rows
        self.invalidate()

    def visible_rows(self) -> int:
        """How many list rows fit, given the room and what the frame costs."""
        if self._rows_available is None:
            return LIST_VISIBLE
        try:
            room = int(self._rows_available())
        except Exception:  # pragma: no cover - a caller that cannot measure
            return LIST_VISIBLE
        chrome = self.CHROME - 2 if self.docked else self.CHROME
        # The (n/total) counter appears exactly when the list is truncated,
        # which is what this is deciding - so its line is always reserved
        # rather than resolved, which would not terminate.
        if len(self._rows) > 1:
            chrome += 1
        return max(LIST_MINIMUM, room - chrome)

    # -- data --------------------------------------------------------------

    def rows(self, query: str) -> list[tuple[str, list[str]]]:
        """``(value, cells)`` pairs matching ``query``. Cells become columns."""
        raise NotImplementedError

    def current_value(self) -> str | None:
        """The value already in force, marked separately from the selection."""
        return None

    def _refresh(self) -> None:
        self._rows = self.rows(self.query)
        self.selected = min(self.selected, max(0, len(self._rows) - 1))
        self.invalidate()

    # -- rendering ---------------------------------------------------------

    def draw(self, width: int) -> list[str]:
        laid_out = columns([cells for _value, cells in self._rows])
        current = self.current_value()

        body: list[str] = []
        for index in self._window():
            value, _cells = self._rows[index]
            chosen = index == self.selected
            cursor = fg("accent", CURSOR) if chosen else GUTTER
            marker = fg("accent", CURRENT) if value == current else GUTTER
            body.append(cursor + marker + laid_out[index])

        if not self._rows:
            body.append(fg("muted", "no matches"))
        elif len(self._rows) > self.visible_rows():
            body.append(fg("muted", f"({self.selected + 1}/{len(self._rows)})"))

        # An empty filter shows a hint, not entered text. Keep the cursor at the
        # insertion point before that hint; once typing starts it follows the
        # query as usual.
        filter_line = (
            fg("text", self.query) + _cursor_cell()
            if self.query
            else _cursor_cell() + fg("dim", self.placeholder)
        )
        parts = [
            Rule(rule("border")),
            Spacer(1),
            Text(fg("accent", self.title, bold=True), 1, 0),
            Text(filter_line, 1, 0),
            Spacer(1),
            Lines(body),
            Spacer(1),
            Text(
                hints_line(
                    [
                        Hint("↑↓", "move"),
                        Hint("enter", "select"),
                        Hint("esc", "cancel"),
                    ]
                ),
                1,
                0,
            ),
            *self.closing(),
        ]
        return [line for part in parts for line in part.render(width)]

    def _window(self) -> range:
        """The visible slice, centred on the selection where it can be."""
        total = len(self._rows)
        visible = self.visible_rows()
        if total <= visible:
            return range(total)
        half = visible // 2
        start = max(0, min(self.selected - half, total - visible))
        return range(start, start + visible)

    # -- input -------------------------------------------------------------

    def handle_input(self, key: str, data: str) -> bool:
        if self.done:
            return False

        if key == "text":
            self.query += data
            self.selected = 0
            self._refresh()
            return True
        if key == "backspace":
            self.query = self.query[:-1]
            self.selected = 0
            self._refresh()
            return True
        if key in ("up", "down") and self._rows:
            step = 1 if key == "down" else -1
            self.selected = (self.selected + step) % len(self._rows)
            self.invalidate()
            return True
        if key in ("pageup", "pagedown") and self._rows:
            visible = self.visible_rows()
            step = visible if key == "pagedown" else -visible
            self.selected = max(0, min(len(self._rows) - 1, self.selected + step))
            self.invalidate()
            return True
        if key == "enter":
            self.result = self._rows[self.selected][0] if self._rows else None
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


class CommandPalette(Picker):
    title = "Commands"
    placeholder = "Filter commands…"

    def __init__(self, commands: list[Any], initial: str = "") -> None:
        self.commands = commands
        super().__init__(initial)

    def rows(self, query: str) -> list[tuple[str, list[str]]]:
        # Fuzzy over name *and* summary: in a palette the user is often looking
        # for a capability ("cost") rather than a name they already know.
        matches = filter_items(
            self.commands,
            query.strip().lstrip("/"),
            key=lambda command: f"{command.name} {command.summary}",
        )
        return [
            (command.name, [fg("text", f"/{command.name}"), fg("muted", command.summary)])
            for command in matches
        ]


class ModelPicker(Picker):
    """Model chooser.

    Each row shows id, context window, what pays for it, and whether the model
    supports prompt caching - switching to a model without caching has a real
    and otherwise invisible cost.

    "What pays for it" is a column rather than an inference from the id: with
    two credentials installed, a subscription model priced at $0.00/$0.00 is
    indistinguishable from a free OpenRouter one, and the difference is the
    whole question of which account a turn lands on.
    """

    title = "Select model"
    placeholder = "Filter models…"

    LIMIT: ClassVar[int] = 200

    def __init__(self, models: list[Any], current: str, initial: str = "") -> None:
        self.models = models
        self.current = current
        super().__init__(initial)

    def current_value(self) -> str | None:
        return self.current

    def rows(self, query: str) -> list[tuple[str, list[str]]]:
        from hx.providers.models import match_models

        # The same matcher `/model <query>` uses, so typing here narrows
        # exactly the way typing there does.
        matches = match_models(self.models, query)
        return [(model.id, self._cells(model)) for model in matches[: self.LIMIT]]

    def _cells(self, model: Any) -> list[str]:
        mode = str(model.cache_mode)
        cache = {"explicit": "cache✓", "implicit": "cache~", "none": "cache✗"}[mode]
        cache_role = {"explicit": "success", "implicit": "warning", "none": "dim"}[mode]
        return [
            fg("text", model.id, bold=model.id == self.current),
            fg("muted", format_tokens(model.context_window)),
            self._billing(model),
            fg(cache_role, cache),
        ]

    def _billing(self, model: Any) -> str:
        """What pays for this model.

        A subscription has no per-token price, so printing $0.00 would be a
        claim about its cost rather than the absence of one.
        """
        if model.is_subscription:
            return fg("success", "subscription")
        prompt = model.pricing.prompt * 1_000_000
        completion = model.pricing.completion * 1_000_000
        return fg("dim", f"${prompt:.2f}/${completion:.2f}")


DEFAULT_EFFORT_ROW = "default"
"""Value of the row that clears the setting. Never a level name - ``none`` is a
real effort (no reasoning at all), so it cannot double as "unset"."""


class EffortPicker(Picker):
    """Reasoning-depth chooser for the model in force.

    Only the levels that model advertises are offered: they differ per model,
    and a row that gets silently lowered on the way out is a row that lied.
    """

    placeholder = "Filter levels…"

    def __init__(self, info: Any, current: str | None) -> None:
        self.info = info
        self.current = current
        self.title = f"Reasoning effort for {info.id.split('/')[-1]}"
        super().__init__()

    def current_value(self) -> str | None:
        return self.current or DEFAULT_EFFORT_ROW

    def rows(self, query: str) -> list[tuple[str, list[str]]]:
        default_level = self.info.default_reasoning_level or "the model's default"
        options: list[tuple[str, str]] = [
            (DEFAULT_EFFORT_ROW, f"leave it to the model - runs at {default_level}")
        ]
        options.extend((level, "") for level in self.info.reasoning_levels)

        needle = query.strip().lower()
        rows = []
        for value, note in options:
            if needle and needle not in value:
                continue
            if not note and value == self.info.default_reasoning_level:
                note = "this model's default"
            rows.append((value, [fg("text", value), fg("muted", note)]))
        return rows


class SessionPicker(Picker):
    """Resume a previous session in this directory."""

    title = "Resume session"
    placeholder = "Filter sessions…"

    def __init__(self, sessions: list[Any]) -> None:
        self.sessions = sessions
        super().__init__()

    def rows(self, query: str) -> list[tuple[str, list[str]]]:
        def plain(meta: Any) -> str:
            return f"{_when(meta.updated_at)} {_size(meta)} {meta.title or meta.session_id}"

        return [
            (
                meta.session_id,
                [
                    fg("dim", _when(meta.updated_at)),
                    fg("muted", _size(meta)),
                    # A session title is written by the model, at the end of
                    # the session it names.
                    fg("text", plain_text(meta.title or meta.session_id)),
                ],
            )
            for meta in filter_items(self.sessions, query, key=plain)
        ]


class RewindPicker(Picker):
    """Pick the prompt to take the session back to.

    Newest first: a rewind is nearly always undoing the last thing that
    happened, and that should be the row Enter is already on.
    """

    title = "Rewind to"
    placeholder = "Filter prompts…"

    def __init__(self, points: list[Any]) -> None:
        self.points = list(reversed(points))
        super().__init__()

    def rows(self, query: str) -> list[tuple[str, list[str]]]:
        def plain(point: Any) -> str:
            return f"{one_line(point.text, RECORD_WIDTH)} {point.index}"

        rows = []
        for point in filter_items(self.points, query, key=plain):
            cells = [
                fg("dim", time.strftime("%H:%M", time.localtime(point.timestamp))),
                fg("text", one_line(point.text, RECORD_WIDTH) or "(empty prompt)"),
            ]
            if point.compacted:
                cells.append(fg("muted", "compacted"))
            rows.append((str(point.index), cells))
        return rows


def _when(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))


def _size(meta: Any) -> str:
    """How big a session is, led by the number the user recognises.

    A prompt is a thing they typed; a message is a wire-format record, and
    there are roughly two of those per provider call. Listing only the second
    made a one-prompt session read as "31 msgs", which describes the protocol
    rather than the conversation.

    Sessions recorded before prompts were counted have nothing to lead with, so
    they keep the old shape rather than claiming zero prompts.
    """
    prompts = getattr(meta, "prompt_count", 0)
    if not prompts:
        return f"{meta.message_count} msgs"
    unit = "prompt" if prompts == 1 else "prompts"
    return f"{prompts} {unit} · {meta.message_count} msgs"


__all__ = [
    "DEFAULT_EFFORT_ROW",
    "CommandPalette",
    "EffortPicker",
    "ModelPicker",
    "Picker",
    "RewindPicker",
    "SessionPicker",
]
