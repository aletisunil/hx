"""The two lines at the bottom, and the one above them.

Everything here is low contrast on purpose. It is reference material - what
model, what mode, how much context is left - that should be readable when
looked at and invisible when not. The one exception is the context gauge, which
changes colour as it approaches the point where the next turn compacts, because
that is the one field with a deadline attached.
"""

from __future__ import annotations

from hx.core.usage import format_tokens
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


def justify(left: str, right: str, width: int) -> str:
    """Left half flush left, right half flush right; the left half yields first.

    The right half is short and fixed - the model and the permission mode - and
    it is what a user glances at without reading. A long project path must not
    push either of them off the end, so the path is the half that is cut.
    """
    right_width = cell_width(right)
    keep_right = bool(right_width) and width - right_width - GAP >= MIN_LEFT
    room = width - right_width - GAP if keep_right else width

    if cell_width(left) > room:
        left = truncate_to_width(left, max(0, room - cell_width(ELLIPSIS))) + ELLIPSIS
    if not keep_right:
        return left
    return left + " " * max(0, width - cell_width(left) - right_width) + right


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

    def set_model(self, model_id: str, *, subscription: bool = False) -> None:
        self.update(model=model_id, subscription=subscription)

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
        return [
            fill_line(" " + justify(self._location(), self._mode(), inner), width),
            fill_line(" " + justify(self._stats(inner), self._model(), inner), width),
        ]

    # -- fields ------------------------------------------------------------

    def _location(self) -> str:
        field = fg("dim", self.cwd or "")
        if self.branch:
            field += fg("dim", f" ({self.branch})")
        if self.queued:
            field += fg("muted", f"  ⧗{self.queued} queued")
        return field

    def _mode(self) -> str:
        field = fg(MODE_ROLES.get(self.mode, "dim"), self.mode)
        if not self.sandbox_active:
            field += fg("warning", " · no-sandbox")
        elif self.sandbox_backend and self.sandbox_backend != "none":
            field += fg("dim", f" · sandbox {self.sandbox_backend}")
        return field

    def _stats(self, width: int) -> str:
        parts: list[str] = []
        if self.input_tokens:
            parts.append(fg("dim", f"↑{format_tokens(self.input_tokens)}"))
        if self.output_tokens:
            parts.append(fg("dim", f"↓{format_tokens(self.output_tokens)}"))
        parts.append(self._cache_field())
        # A subscription turn has no per-token price, so "$0.00" would be a
        # claim about spend rather than the absence of one.
        parts.append(
            fg("muted", "sub") if self.subscription else fg("muted", format_cost(self.cost_usd))
        )
        if self.latency_ms >= 100:
            parts.append(fg("dim", f"{self.latency_ms / 1000:.1f}s"))
        parts.append(self._context(width))
        return " ".join(parts)

    def _cache_field(self) -> str:
        """Cache tokens read and written, plus the hit rate.

        A warm prefix shows a high read count; a rate that collapses after an
        edit is the visible symptom of a busted prefix.
        """
        if not self.cache_read and not self.cache_write:
            return fg("dim", "cache -")
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
            lines.append(fg("muted", "An agent harness. Ask a question, or start with /help."))
            lines.append(fg("dim", f"{KEYMAP.primary('app.tools.expand')} shows every key."))
        else:
            rows = [
                (fg("dim", KEYMAP.text(binding.id)), fg("muted", binding.description))
                for binding in KEYMAP.bindings.values()
            ]
            from hx.tui.format import columns

            lines.extend(columns(rows))
            lines.append(fg("dim", "/ commands  ! bash  @ files"))
        return Lines(lines).render(width)
