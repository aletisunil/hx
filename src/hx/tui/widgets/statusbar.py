"""Footer.

Two lines, the way pi lays them out::

    ~/src/hx (main)                                    default · sandbox seatbelt
    ↑57k ↓3.5k R244k W12k CH81% $0.1652 1.4s 12%/200k (auto)   claude-sonnet-4.5

A model on a subscription route is tagged ``gpt-5.6-terra (sub)`` and its cost
field reads ``sub`` rather than ``$0.00``: which credential paid for a turn has
to be readable off the bar, not inferred from a model id it does not show. The
reasoning depth rides along as ``· high`` where the model has one, since it is
set per model and changes underfoot when the model does.

The first line answers "where am I and what am I allowed to do"; the second
answers "what is this costing and how much room is left". Cache and cost are
the point of the bar: they are the only place the user can see whether the
prefix is staying warm.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from hx.core.usage import format_cost, format_tokens
from hx.tui.theme import THEME

GAP = 2
"""Minimum space between the left and right halves of a line."""

MIN_LEFT = 12
"""Below this the left half is not worth keeping, so the right half is dropped
instead of squeezing both into nonsense."""

WARN_FRACTION = 0.75
"""Amber past here - close enough to compaction that the user should know."""
DANGER_FRACTION = 0.90


class StatusBar(Static):
    def __init__(self) -> None:
        super().__init__(id="statusbar")
        self.model = ""
        self.context_used = 0
        self.context_window = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read = 0
        self.cache_write = 0
        self.cache_hit_rate = 0.0
        self.cost_usd = 0.0
        self.latency_ms = 0.0
        self.mode = "default"
        self.effort: str | None = None
        """Reasoning depth in force, or ``None`` where the model has no say in
        it. Shown because it is resolved per model: switching models can change
        it without anyone typing anything."""
        self.subscription = False
        """True on a route billed to a subscription, where a per-token cost is
        not a number the user can act on."""
        self.sandbox_active = True
        self.sandbox_backend = ""
        self.auto_compact = True
        self.cwd = ""
        self.branch: str | None = None
        self.queued = 0
        """Messages waiting for the running turn to end."""

    def set_queued(self, count: int) -> None:
        """Show how much the user is waiting on - a queue nobody can see is a
        queue they forget they filled."""
        if count == self.queued:
            return
        self.queued = count
        self.refresh()

    def set_model(self, model_id: str, *, subscription: bool = False) -> None:
        self.model = model_id
        self.subscription = subscription
        self.refresh()

    def set_effort(self, effort: str | None) -> None:
        if effort == self.effort:
            return
        self.effort = effort
        self.refresh()

    def set_context(self, used: int, window: int) -> None:
        """Amber past the compaction threshold, red near the limit."""
        self.context_used = used
        self.context_window = window
        self.refresh()

    def set_tokens(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.refresh()

    def set_cache(self, read_tokens: int, write_tokens: int, hit_rate: float) -> None:
        self.cache_read = read_tokens
        self.cache_write = write_tokens
        self.cache_hit_rate = hit_rate
        self.refresh()

    def set_cost(self, cost_usd: float) -> None:
        self.cost_usd = cost_usd
        self.refresh()

    def set_latency(self, ms: float) -> None:
        self.latency_ms = ms
        self.refresh()

    def set_mode(self, mode: str, sandbox_active: bool, backend: str = "") -> None:
        """Renders an explicit ``no sandbox`` marker when the backend degraded,
        so the user is never wrong about what is protecting them."""
        self.mode = mode
        self.sandbox_active = sandbox_active
        self.sandbox_backend = backend or self.sandbox_backend
        self.refresh()

    def set_location(self, cwd: str, branch: str | None) -> None:
        self.cwd = cwd
        self.branch = branch
        self.refresh()

    @property
    def context_fraction(self) -> float:
        if self.context_window <= 0:
            return 0.0
        return min(1.0, self.context_used / self.context_window)

    # -- rendering ---------------------------------------------------------- #

    def render(self) -> Text:
        width = self.size.width or 120
        line = Text(no_wrap=True, overflow="ellipsis", style=THEME.fg("dim"))
        line.append_text(self._justify(self._location_field(), self._mode_field(), width))
        line.append("\n")
        line.append_text(self._justify(self._stats_field(), self._model_field(), width))
        return line

    @staticmethod
    def _justify(left: Text, right: Text, width: int) -> Text:
        """Left half flush left, right half flush right; the left half yields first.

        The right half is short and fixed - the model and the permission mode -
        and it is what a user glances at without reading. A long project path
        must not be allowed to push either of them off the end, so the path is
        the half that gets the ellipsis.
        """
        keep_right = right.cell_len and width - right.cell_len - GAP >= MIN_LEFT
        room = width - right.cell_len - GAP if keep_right else width

        line = left.copy()
        if line.cell_len > room:
            line.truncate(max(0, room), overflow="ellipsis")
        if not keep_right:
            return line

        line.append(" " * max(0, width - line.cell_len - right.cell_len))
        line.append_text(right)
        return line

    def _location_field(self) -> Text:
        field = Text(self.cwd or "", style=THEME.fg("dim"))
        if self.branch:
            field.append(f" ({self.branch})", style=THEME.fg("dim"))
        if self.queued:
            field.append(f"  ⧗{self.queued} queued", style=THEME.fg("muted"))
        return field

    def _stats_field(self) -> Text:
        field = Text(style=THEME.fg("dim"))
        parts: list[Text] = []
        if self.input_tokens:
            parts.append(Text(f"↑{format_tokens(self.input_tokens)}", style=THEME.fg("dim")))
        if self.output_tokens:
            parts.append(Text(f"↓{format_tokens(self.output_tokens)}", style=THEME.fg("dim")))
        parts.append(self._cache_field())
        # A subscription turn has no per-token price, so "$0.00" would be a
        # claim about spend rather than the absence of one.
        parts.append(
            Text("sub", style=THEME.fg("muted"))
            if self.subscription
            else Text(format_cost(self.cost_usd), style=THEME.fg("muted"))
        )
        if self.latency_ms >= 100:
            parts.append(Text(f"{self.latency_ms / 1000:.1f}s", style=THEME.fg("dim")))
        parts.append(self._context_field())

        for index, part in enumerate(parts):
            if index:
                field.append(" ")
            field.append_text(part)
        return field

    def _model_field(self) -> Text:
        """``gpt-5.6-terra (sub)`` - the bare name, plus the route when it is not
        the default one.

        The namespace is stripped because ``anthropic/claude-sonnet-4.5`` is
        four wasted columns on the line that matters most. But stripping it from
        ``openai-codex/gpt-5.6-terra`` also erased the only on-screen trace of
        which account a turn was billed to, so the route comes back as a tag.
        """
        if not self.model:
            return Text("no model", style=THEME.fg("muted"))

        field = Text(self.model.split("/")[-1], style=THEME.fg("muted"))
        if self.subscription:
            field.append(" (sub)", style=THEME.fg("success"))
        if self.effort:
            field.append(f" · {self.effort}", style=THEME.fg("dim"))
        return field

    def _context_field(self) -> Text:
        """``12%/200k`` - percentage first, because that is the number that decides
        whether the next turn compacts."""
        fraction = self.context_fraction
        role = "success"
        if fraction >= DANGER_FRACTION:
            role = "error"
        elif fraction >= WARN_FRACTION:
            role = "warning"

        window = format_tokens(self.context_window) if self.context_window else "?"
        field = Text(f"{fraction * 100:.0f}%/{window}", style=THEME.fg(role))
        if self.auto_compact:
            field.append(" (auto)", style=THEME.fg("dim"))
        return field

    def _cache_field(self) -> Text:
        """Cache tokens read and written, plus the hit rate.

        A warm prefix shows a high read count; a rate that collapses after an
        edit is the visible symptom of a busted prefix.
        """
        if not self.cache_read and not self.cache_write:
            return Text("cache -", style=THEME.fg("dim"))
        role = "success" if self.cache_hit_rate >= 0.5 else "warning"
        field = Text(f"R{format_tokens(self.cache_read)}", style=THEME.fg(role))
        field.append(f" W{format_tokens(self.cache_write)}", style=THEME.fg("dim"))
        field.append(f" CH{self.cache_hit_rate * 100:.0f}%", style=THEME.fg(role))
        return field

    def _mode_field(self) -> Text:
        roles = {
            "plan": "border",
            "default": "dim",
            "acceptEdits": "warning",
            "bypass": "error",
        }
        field = Text(self.mode, style=THEME.fg(roles.get(self.mode, "dim")))
        if self.sandbox_active:
            if self.sandbox_backend and self.sandbox_backend != "none":
                field.append(f" · sandbox {self.sandbox_backend}", style=THEME.fg("dim"))
        else:
            field.append(" · no-sandbox", style=THEME.fg("warning"))
        return field
