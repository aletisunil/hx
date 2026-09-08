"""Status bar.

Fields, left to right::

    model | ctx used/window (%) [bar] | in/out tokens | cache R/W + hit% |
    $cost | last turn latency | cwd @ branch | permission mode

The cache and cost fields are the point of the bar - they are what tell the
user whether the prefix is staying warm and what the session is costing.
"""

from __future__ import annotations

from rich.console import RenderableType
from rich.text import Text
from textual.widgets import Static

from hx.core.usage import format_cost, format_tokens

GAUGE_WIDTH = 10
SEPARATOR = "  "
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
        self.sandbox_active = True
        self.cwd = ""
        self.branch: str | None = None
        self.busy = False
        self.busy_label = ""

    def set_model(self, model_id: str) -> None:
        self.model = model_id
        self.refresh()

    def set_context(self, used: int, window: int) -> None:
        """Gauge turns amber past the compaction threshold and red near the limit."""
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

    def set_mode(self, mode: str, sandbox_active: bool) -> None:
        """Renders an explicit ``no sandbox`` marker when the backend degraded,
        so the user is never wrong about what is protecting them."""
        self.mode = mode
        self.sandbox_active = sandbox_active
        self.refresh()

    def set_location(self, cwd: str, branch: str | None) -> None:
        self.cwd = cwd
        self.branch = branch
        self.refresh()

    def set_busy(self, busy: bool, label: str = "") -> None:
        self.busy = busy
        self.busy_label = label
        self.refresh()

    @property
    def context_fraction(self) -> float:
        if self.context_window <= 0:
            return 0.0
        return min(1.0, self.context_used / self.context_window)

    def render(self) -> RenderableType:
        """Assemble the fields, dropping the least important until they fit.

        Plain ellipsis truncation would eat the right-hand end of the bar, which
        is where cost and permission mode live - exactly the fields the user
        most needs to see. Fields are dropped by priority instead.
        """
        fields: list[tuple[int, Text]] = [
            (0, Text(self.model.split("/")[-1] or "no model", style="bold cyan")),
            (1, self._context_field()),
            (2, self._mode_field()),
            (3, Text(format_cost(self.cost_usd), style="bold")),
            (4, self._cache_field()),
            (
                5,
                Text(
                    f"↑{format_tokens(self.input_tokens)} ↓{format_tokens(self.output_tokens)}",
                    style="dim",
                ),
            ),
        ]
        if self.latency_ms:
            fields.append((6, Text(f"{self.latency_ms / 1000:.1f}s", style="dim")))
        if self.cwd:
            location = self.cwd if not self.branch else f"{self.cwd}@{self.branch}"
            fields.append((7, Text(location, style="dim")))

        prefix = Text()
        if self.busy:
            prefix = Text(f"⠿ {self.busy_label or 'working'} ", style="bold yellow")

        available = (self.size.width or 120) - prefix.cell_len
        keep = self._fit(fields, available)

        line = Text(no_wrap=True, overflow="ellipsis")
        line.append_text(prefix)
        for index, field in enumerate(keep):
            if index:
                line.append(SEPARATOR, style="dim")
            line.append_text(field)
        return line

    @staticmethod
    def _fit(fields: list[tuple[int, Text]], available: int) -> list[Text]:
        """Drop the lowest-priority fields until the line fits."""
        chosen = list(fields)
        while chosen:
            width = sum(f.cell_len for _, f in chosen) + len(SEPARATOR) * (len(chosen) - 1)
            if width <= available or len(chosen) == 1:
                break
            worst = max(range(len(chosen)), key=lambda i: chosen[i][0])
            chosen.pop(worst)
        return [field for _, field in chosen]

    def _context_field(self) -> Text:
        fraction = self.context_fraction
        style = "green"
        if fraction >= DANGER_FRACTION:
            style = "bold red"
        elif fraction >= WARN_FRACTION:
            style = "yellow"

        filled = int(fraction * GAUGE_WIDTH)
        gauge = "█" * filled + "░" * (GAUGE_WIDTH - filled)
        window = format_tokens(self.context_window) if self.context_window else "?"
        return Text.assemble(
            (gauge, style),
            (f" {format_tokens(self.context_used)}/{window} ", "dim"),
            (f"({fraction * 100:.0f}%)", style),
        )

    def _cache_field(self) -> Text:
        """Cache tokens read and written, plus the hit rate.

        A session with a warm prefix shows a high read count; a rate that
        collapses after an edit is the visible symptom of a busted prefix.
        """
        if not self.cache_read and not self.cache_write:
            return Text("cache -", style="dim")
        style = "green" if self.cache_hit_rate >= 0.5 else "yellow"
        return Text.assemble(
            ("cache ", "dim"),
            (f"R{format_tokens(self.cache_read)}", style),
            ("/", "dim"),
            (f"W{format_tokens(self.cache_write)}", "dim"),
            (f" {self.cache_hit_rate * 100:.0f}%", style),
        )

    def _mode_field(self) -> Text:
        colors = {
            "plan": "blue",
            "default": "dim",
            "acceptEdits": "yellow",
            "bypass": "bold red",
        }
        field = Text(self.mode, style=colors.get(self.mode, "dim"))
        if not self.sandbox_active:
            field.append(" no-sandbox", style="bold yellow")
        return field
