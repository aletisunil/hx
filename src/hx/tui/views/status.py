"""The two lines at the bottom, and the one above them.

Everything here is low contrast on purpose. It is reference material - what
model, what mode, how much context is left - that should be readable when
looked at and invisible when not. The one exception is the context gauge, which
changes colour as it approaches the point where the next turn compacts, because
that is the one field with a deadline attached.
"""

from __future__ import annotations

import time
from dataclasses import replace

from hx.core.usage import (
    RENEWAL_CLOCK_SECONDS,
    PlanWindow,
    format_percent,
    format_renewal,
    format_tokens,
)
from hx.term.component import Widget
from hx.term.width import cell_width, truncate_to_width
from hx.tui.format import meter_fill
from hx.tui.glyphs import ELLIPSIS, METER_EMPTY, METER_FULL, SEPARATOR
from hx.tui.paint import fg

GAP = 2
"""Minimum space between the two halves of a line."""

MIN_LEFT = 12
"""Below this the left half is not worth keeping, and the right half wins."""

WARN_FRACTION = 0.75
DANGER_FRACTION = 0.90

METER_CELLS = 6
"""Width of the context gauge. Six cells is one step per 17% - coarse, which
is the point: the number beside it is there for anyone who wants precision."""

METER_MIN_WIDTH = 60
"""Below this the gauge is dropped. It is the decoration on that field, and a
narrow pane should spend its columns on the counts instead."""

MODE_ROLES = {"plan": "border", "default": "dim", "acceptEdits": "warning", "bypass": "error"}


def justify(left: str, right: str, width: int, tail: str = "") -> str:
    """Left half flush left, right half flush right; the left half yields first.

    The right half is short and fixed - the model and the permission mode - and
    it is what a user glances at without reading. A long project path must not
    push either of them off the end, so the path is the half that is cut.

    ``tail`` ends the left half and is cut only after ``left`` is gone: a
    transient count such as the queue depth, which is the reason to look at the
    line at all, must not be the first thing a long path pushes off it.
    """
    right_width = cell_width(right)
    keep_right = bool(right_width) and width - right_width - GAP >= MIN_LEFT
    room = width - right_width - GAP if keep_right else width

    if cell_width(left) + cell_width(tail) > room:
        room_for_left = room - cell_width(tail) - cell_width(ELLIPSIS)
        if room_for_left > 0:
            left = truncate_to_width(left, room_for_left) + ELLIPSIS + tail
        else:
            left = truncate_to_width(left + tail, max(0, room - cell_width(ELLIPSIS))) + ELLIPSIS
    else:
        left += tail
    if not keep_right:
        return left
    return left + " " * max(0, width - cell_width(left) - right_width) + right


def fit_path(path: str, width: int) -> str:
    """``path`` cut from its start, at a separator, until it fits ``width``.

    The end of a path is the part that names the project. Cut from the right,
    a deep checkout showed ``/private/var/folders/…`` and lost the one thing
    the field is there to say.
    """
    if cell_width(path) <= width:
        return path
    parts = path.split("/")
    for start in range(1, len(parts)):
        candidate = ELLIPSIS + "/" + "/".join(parts[start:])
        if cell_width(candidate) <= width:
            return candidate
    # Even the last name alone is too long; it keeps its end, like the rest.
    name = parts[-1]
    while name and cell_width(ELLIPSIS + name) > width:
        name = name[1:]
    return ELLIPSIS + name if name else ""


def meter(fraction: float, cells: int, role: str) -> str:
    """A ``cells``-wide gauge: the filled run in ``role``, the rest dim.

    The fill maths is shared with the legacy bar, so the two renderers cannot
    disagree about how full the window is during the changeover.
    """
    filled = meter_fill(fraction, cells)
    return fg(role, METER_FULL * filled) + fg("dim", METER_EMPTY * (cells - filled))


def format_cost(usd: float) -> str:
    return f"${usd:.4f}" if usd < 0.01 else f"${usd:.2f}"


class StatusBar(Widget):
    """Two docked lines: where you are, and what this is costing."""

    def __init__(self) -> None:
        super().__init__()
        self.cwd = ""
        self.branch: str | None = None
        self.queued = 0
        self.model = ""
        self.subscription = False
        self.route = ""
        self.plans: dict[str, dict[tuple[int, str], PlanWindow]] = {}
        """Each subscription route's usage windows, as last reported. Kept per
        route, across model switches and /clear: they are the account's."""
        self._redraw_at: float | None = None
        self.effort: str | None = None
        self.mode = "default"
        self.sandbox_active = True
        self.sandbox_backend = ""
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read = 0
        self.cache_write = 0
        self.cache_hit_rate = 0.0
        self.cost_usd = 0.0
        self.latency_ms = 0.0
        self.context_used = 0
        self.context_window = 0
        self.auto_compact = True

    def update(self, **fields: object) -> None:
        for name, value in fields.items():
            setattr(self, name, value)
        self.invalidate()

    # Kept as named setters because the event consumer calls them by name and
    # those call sites carry over from the old app unchanged.
    def set_location(self, cwd: str, branch: str | None) -> None:
        self.update(cwd=cwd, branch=branch)

    def set_model(self, model_id: str, *, subscription: bool = False, route: str = "") -> None:
        self.update(model=model_id, subscription=subscription, route=route)

    def set_plan(self, route: str, windows: tuple[PlanWindow, ...]) -> None:
        """Take a route's report of its windows over what it said before.

        Window by window: Claude reports one per frame, and a frame about the
        week says nothing about the five hours. Nor does a report without a
        figure undo one for the same window and renewal: an older Claude Code
        gives the figure only past its warning, and it is still true.
        """
        known = self.plans.setdefault(route, {})
        for window in windows:
            before = known.get(window.key)
            if (
                window.used_percent is None
                and before is not None
                and before.used_percent is not None
                and before.resets_at == window.resets_at
            ):
                window = replace(window, used_percent=before.used_percent)
            known[window.key] = window
        self.invalidate()

    def plan_windows(self, now: float | None = None) -> list[PlanWindow]:
        """The shown route's windows as they stand now, shortest first."""
        if not self.subscription:
            return []
        current = (w.at(now) for w in self.plans.get(self.route, {}).values())
        return sorted((w for w in current if w is not None), key=lambda w: w.key)

    def redraw_due(self, now: float | None = None) -> bool:
        """Whether a window shown on the last frame has reset since, or come
        within a day of it - where its renewal is given as a clock time.

        Nothing else redraws an idle screen, and a figure that reset an hour
        ago reads as an hour-old warning.
        """
        now = time.time() if now is None else now
        return self._redraw_at is not None and now >= self._redraw_at

    def set_context(self, used: int, window: int) -> None:
        self.update(context_used=used, context_window=window)

    def set_tokens(self, input_tokens: int, output_tokens: int) -> None:
        self.update(input_tokens=input_tokens, output_tokens=output_tokens)

    def set_cache(self, read_tokens: int, write_tokens: int, hit_rate: float) -> None:
        self.update(cache_read=read_tokens, cache_write=write_tokens, cache_hit_rate=hit_rate)

    def set_effort(self, effort: str | None) -> None:
        """Reasoning depth, resolved per model.

        It can change without the user asking - a new model may not offer the
        level the last one ran at - so the bar has to say so when it happens.
        """
        self.update(effort=effort)

    def set_cost(self, cost_usd: float) -> None:
        self.update(cost_usd=cost_usd)

    def set_queued(self, count: int) -> None:
        self.update(queued=count)

    def set_mode(self, mode: str, sandbox_active: bool, backend: str = "") -> None:
        self.update(mode=mode, sandbox_active=sandbox_active, sandbox_backend=backend)

    @property
    def context_fraction(self) -> float:
        return self.context_used / self.context_window if self.context_window else 0.0

    def draw(self, width: int) -> list[str]:
        from hx.term.ansi import fill_line

        inner = max(1, width - 2)
        mode, queue = self._mode(), self._queue()
        keeps_mode = inner - cell_width(mode) - GAP >= MIN_LEFT
        room = inner - cell_width(queue) - (cell_width(mode) + GAP if keeps_mode else 0)
        model = self._model()
        keeps_model = inner - cell_width(model) - GAP >= MIN_LEFT
        stats_room = inner - (cell_width(model) + GAP if keeps_model else 0)
        windows = self.plan_windows()
        now = time.time()
        changes = [
            moment
            for w in windows
            if w.resets_at is not None
            for moment in (w.resets_at - RENEWAL_CLOCK_SECONDS, w.resets_at)
            if moment > now
        ]
        self._redraw_at = min(changes) if changes else None
        lines = [fill_line(" " + justify(self._location(room), mode, inner, queue), width)]
        if windows:
            lines.append(fill_line(" " + self._plan(windows, inner), width))
        lines.append(
            fill_line(
                " " + justify(self._stats(inner, stats_room, bool(windows)), model, inner), width
            )
        )
        return lines

    # -- fields ------------------------------------------------------------

    def _location(self, room: int) -> str:
        """The project path and its branch, the path shortened first.

        ``room`` is what :func:`justify` will leave this half. The path is cut
        here, from its start, so the cut lands where it costs least. When even
        the project's own name and the branch do not fit side by side, the
        branch goes: the name says where you are, the branch only which line
        of work.
        """
        cwd = self.cwd or ""
        branch = f" ({self.branch})" if self.branch else ""
        name = cwd.rsplit("/", 1)[-1]
        shortest = min(cell_width(cwd), cell_width(ELLIPSIS + "/" + name))
        if branch and room - cell_width(branch) < shortest:
            branch = ""
        return fg("dim", fit_path(cwd, max(0, room - cell_width(branch))) + branch)

    def _queue(self) -> str:
        return fg("muted", f"  ⧗{self.queued} queued") if self.queued else ""

    def _mode(self) -> str:
        field = fg(MODE_ROLES.get(self.mode, "dim"), self.mode)
        if not self.sandbox_active:
            field += fg("warning", " · no-sandbox")
        elif self.sandbox_backend and self.sandbox_backend != "none":
            field += fg("dim", f" · sandbox {self.sandbox_backend}")
        return field

    def _stats(self, width: int, room: int, has_plan: bool) -> str:
        """What this is costing, in fields dropped whole to fit ``room``.

        Cut from the right, a narrow pane lost the context gauge and the
        usage windows - the two fields with a deadline - to token counts
        that have none. So the counts go first, then the cache, and a field
        is never left cut mid-number.
        """
        # (priority, field): the lowest number is the last to go.
        fields: list[tuple[int, str]] = []
        if self.input_tokens:
            fields.append((4, fg("dim", f"↑{format_tokens(self.input_tokens)}")))
        if self.output_tokens:
            fields.append((5, fg("dim", f"↓{format_tokens(self.output_tokens)}")))
        fields.append((3, self._cache_field()))
        # A subscription turn has no per-token price, so "$0.00" would be a
        # claim about spend rather than the absence of one. What it has is a
        # share of the plan's windows, on a line of its own once the route
        # has said how much.
        if not self.subscription:
            fields.append((2, fg("muted", format_cost(self.cost_usd))))
        elif not has_plan:
            fields.append((2, fg("muted", "sub")))
        if self.latency_ms >= 100:
            fields.append((6, fg("dim", f"{self.latency_ms / 1000:.1f}s")))
        fields.append((0, self._context(width)))

        def joined() -> str:
            return " ".join(field for _, field in fields)

        while len(fields) > 1 and cell_width(joined()) > room:
            fields.remove(max(fields, key=lambda item: item[0]))
        return joined()

    def _plan(self, windows: list[PlanWindow], width: int) -> str:
        """``5h 16% (renews 06:30 am) | 7d 14% (renews Mon Oct 26 04:00 am)``.

        How much of each usage window is spent, and when it renews. The
        figure is coloured as the context gauge is, by how near the limit: a
        window at nine tenths is the one that ends a session. A window the
        route gave no figure for shows only when it renews, since a number
        would be a guess.

        Too narrow for all of it, the line gives up the least first: the
        clock time of a renewal days away, then the renewals, then every
        window but the one nearest its limit. Never a cut mid-number.
        """
        now = time.time()
        fullest = max(windows, key=lambda w: (_used(w), -w.minutes))
        for level in range(4):
            shown = [fullest] if level == 3 else windows
            line = fg("dim", " | ").join(_window(w, level, now) for w in shown)
            if cell_width(line) <= width:
                return line
        return truncate_to_width(line, width)

    def _cache_field(self) -> str:
        """Cache tokens read and written, plus the hit rate.

        A warm prefix shows a high read count; a rate that collapses after an
        edit is the visible symptom of a busted prefix.
        """
        if not self.cache_read and not self.cache_write:
            return fg("dim", "no cache")
        role = "success" if self.cache_hit_rate >= 0.5 else "warning"
        return (
            fg(role, f"R{format_tokens(self.cache_read)}")
            + fg("dim", f" W{format_tokens(self.cache_write)}")
            + fg(role, f" CH{self.cache_hit_rate * 100:.0f}%")
        )

    def _context(self, width: int) -> str:
        """``▰▰▱▱▱▱ 24k/200k`` - a gauge, then what it is a gauge of.

        The only coloured field down here, and it earns it by having a
        deadline: it decides whether the next turn compacts. The bar is what
        makes that deadline legible without reading - a glance sees how full
        the window is, and the counts are there when the exact figure matters.
        """
        fraction = self.context_fraction
        role = "success"
        if fraction >= DANGER_FRACTION:
            role = "error"
        elif fraction >= WARN_FRACTION:
            role = "warning"

        window = format_tokens(self.context_window) if self.context_window else "?"
        field = fg(role, f"{format_tokens(self.context_used)}/{window}")
        if width >= METER_MIN_WIDTH:
            field = meter(fraction, METER_CELLS, role) + " " + field
        if self.auto_compact:
            field += fg("dim", " (auto)")
        return field

    def _model(self) -> str:
        """``gpt-5.6-terra (sub)`` - the bare name, plus the route when it is
        not the default one.

        The namespace is stripped because ``anthropic/claude-sonnet-4.5`` is
        four wasted columns on the line that matters most. But stripping it
        from ``openai-codex/gpt-5.6-terra`` also erased the only on-screen
        trace of which account a turn was billed to, so the route comes back
        as a tag.
        """
        if not self.model:
            return fg("muted", "no model")
        field = fg("muted", self.model.split("/")[-1])
        if self.subscription:
            field += fg("success", " (sub)")
        if self.effort:
            field += fg("dim", f"{SEPARATOR}{self.effort}")
        return field


def _window(window: PlanWindow, level: int, now: float) -> str:
    """One window at a level of detail: 0 is everything, 1 drops the clock
    from a renewal days away, 2 and up drop renewals - except for a window
    without a figure, whose renewal is all it has to say."""
    renewal = ""
    if window.resets_at is not None:
        when = format_renewal(window.resets_at, short=level >= 1, now=now)
        if level == 0:
            renewal = f" (renews {when})"
        elif level == 1 or window.used_percent is None:
            renewal = f" ({when})"
    if window.used_percent is None:
        return fg("muted", window.label) + fg("dim", renewal)
    role = "muted"
    if window.used_percent >= DANGER_FRACTION * 100:
        role = "error"
    elif window.used_percent >= WARN_FRACTION * 100:
        role = "warning"
    return fg(role, f"{window.label} {format_percent(window.used_percent)}") + fg("dim", renewal)


def _used(window: PlanWindow) -> float:
    """How spent a window is, for choosing between them: one with no figure
    loses to one with any."""
    return window.used_percent if window.used_percent is not None else -1.0


class HintsBar(Widget):
    """One dim line of shortcuts under the prompt.

    Hints are dropped whole rather than clipped mid-word: half a key name
    teaches nothing and looks like a rendering fault.
    """

    def __init__(self) -> None:
        super().__init__()
        self._hints: list[tuple[str, str]] = []

    def set_hints(self, hints: list[tuple[str, str]]) -> None:
        self._hints = hints
        self.invalidate()

    def draw(self, width: int) -> list[str]:
        from hx.term.ansi import fill_line

        room = max(1, width - 2)
        parts: list[str] = []
        used = 0
        for key, description in self._hints:
            rendered = fg("dim", key) + fg("muted", f" {description}")
            step = cell_width(rendered) + (cell_width(SEPARATOR) if parts else 0)
            if used + step > room:
                break
            parts.append(rendered)
            used += step
        return [fill_line(" " + fg("dim", SEPARATOR).join(parts), width)]


class Header(Widget):
    """The block printed once, at the top of the session."""

    def __init__(self, version: str = "", quiet: bool = False) -> None:
        super().__init__()
        self._version = version
        self.quiet = quiet
        self._expanded = False

    @property
    def version(self) -> str:
        return self._version

    @version.setter
    def version(self, value: str) -> None:
        self._version = value
        self.invalidate()

    @property
    def expanded(self) -> bool:
        return self._expanded

    @expanded.setter
    def expanded(self, value: bool) -> None:
        self._expanded = value
        self.invalidate()

    def toggle(self) -> None:
        self.expanded = not self._expanded

    def draw(self, width: int) -> list[str]:
        if self.quiet:
            return []

        from hx.keys import KEYMAP
        from hx.term.primitives import Lines

        title = fg("accent", "hx", bold=True) + fg("dim", f" v{self._version}")
        lines = [title]

        if not self._expanded:
            from hx.term.ansi import wrap

            # Prose, so wrapped between words on a narrow terminal rather than
            # cut off mid-word the way the key table's columns are.
            inner = max(1, width - 2)
            lines += wrap(
                fg("muted", "An agent harness. Ask a question, or start with /help."), inner
            )
            lines += wrap(
                fg("dim", f"{KEYMAP.primary('app.tools.expand')} shows every key."), inner
            )
        else:
            rows = [
                (fg("dim", KEYMAP.text(binding.id)), fg("muted", binding.description))
                for binding in KEYMAP.bindings.values()
                # An action with no key, like suspend by default, has nothing
                # to show here but a blank where its key would be.
                if KEYMAP.keys_for(binding.id)
            ]
            from hx.tui.format import columns

            lines.extend(columns(rows))
            lines.append(fg("dim", "/ commands  ! bash  @ files"))
        return Lines(lines).render(width)
