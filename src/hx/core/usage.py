"""Token and cost ledger.

Feeds the status bar fields: context used, tokens used, cache-read/cache-write
tokens, and session cost. OpenRouter returns an authoritative ``usage.cost``
when ``usage: {include: true}`` is set on the request; we prefer that over our
own arithmetic and only compute locally as a fallback.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoids a providers -> core import cycle at runtime
    from hx.providers.models import ModelPricing


@dataclass(slots=True)
class TurnUsage:
    """Usage for a single provider call."""

    input_tokens: int = 0
    """Prompt tokens billed at the full rate. Excludes ``cache_read_tokens``."""
    output_tokens: int = 0
    cache_read_tokens: int = 0
    """Prompt tokens served from the provider's KV cache (billed at a discount)."""
    cache_write_tokens: int = 0
    """Prompt tokens written into the cache (billed at a premium on Anthropic)."""
    reasoning_tokens: int = 0
    cost_usd: float | None = None
    """Provider-reported cost. ``None`` means fall back to local pricing."""
    latency_ms: float = 0.0

    @property
    def prompt_tokens(self) -> int:
        """Every prompt token, cached or not."""
        return self.input_tokens + self.cache_read_tokens

    @property
    def cache_hit_rate(self) -> float:
        """Cached share of prompt tokens, 0.0-1.0."""
        total = self.prompt_tokens
        return self.cache_read_tokens / total if total else 0.0


@dataclass(slots=True)
class UsageLedger:
    """Running totals for a session."""

    turns: list[TurnUsage] = field(default_factory=list)
    context_tokens: int = 0
    """Tokens currently occupied by the assembled context."""
    context_window: int = 0

    def record(self, usage: TurnUsage) -> None:
        self.turns.append(usage)

    @property
    def total_input(self) -> int:
        return sum(t.input_tokens for t in self.turns)

    @property
    def total_output(self) -> int:
        return sum(t.output_tokens for t in self.turns)

    @property
    def total_cache_read(self) -> int:
        return sum(t.cache_read_tokens for t in self.turns)

    @property
    def total_cache_write(self) -> int:
        return sum(t.cache_write_tokens for t in self.turns)

    @property
    def total_cost_usd(self) -> float:
        return sum(t.cost_usd or 0.0 for t in self.turns)

    @property
    def cache_hit_rate(self) -> float:
        prompt = self.total_input + self.total_cache_read
        return self.total_cache_read / prompt if prompt else 0.0

    @property
    def last_latency_ms(self) -> float:
        return self.turns[-1].latency_ms if self.turns else 0.0

    @property
    def context_fraction(self) -> float:
        """``context_tokens / context_window``, clamped to 0.0-1.0. Drives the
        compaction trigger and the status bar gauge."""
        if self.context_window <= 0:
            return 0.0
        return min(1.0, max(0.0, self.context_tokens / self.context_window))


@dataclass(frozen=True, slots=True)
class PlanWindow:
    """One of a subscription's usage windows, as its route last reported it.

    A subscription has no per-token price; what it has instead is a share of a
    window - five hours, seven days - and a time when that share comes back.
    The route says how much is spent when it chooses to: ChatGPT on every
    response, Claude only once a window nears its limit.
    """

    minutes: int
    """How long the window is: 300 for five hours, 10080 for seven days."""
    used_percent: float | None = None
    """How much of it is spent, 0-100. ``None`` when the route did not say,
    which is not the same as nothing spent."""
    resets_at: float | None = None
    """When it is empty again, in epoch seconds."""
    scope: str = ""
    """What the window is limited to, when not every model: Claude keeps a
    separate weekly window for ``opus``."""

    def __post_init__(self) -> None:
        # Held to 0-100 here rather than by each route: the colours and
        # format_percent assume it, whoever reported the figure.
        if self.used_percent is not None:
            object.__setattr__(self, "used_percent", min(max(self.used_percent, 0.0), 100.0))

    @property
    def key(self) -> tuple[int, str]:
        """Which window this is. A newer report of the same key replaces it."""
        return self.minutes, self.scope

    @property
    def label(self) -> str:
        """``5h``, ``7d``, ``7d opus`` - short, for the status bar."""
        amount, unit = self._length()
        length = f"{amount}{unit[0]}"
        return f"{length} {self.scope}" if self.scope else length

    @property
    def title(self) -> str:
        """``5-hour``, ``7-day opus`` - spelled out, for /cost."""
        amount, unit = self._length()
        length = f"{amount}-{unit}"
        return f"{length} {self.scope}" if self.scope else length

    def _length(self) -> tuple[int, str]:
        """The window's length in the largest whole unit: ``(5, "hour")``."""
        if self.minutes % 1440 == 0:
            return self.minutes // 1440, "day"
        if self.minutes % 60 == 0:
            return self.minutes // 60, "hour"
        return self.minutes, "minute"

    def at(self, now: float | None = None) -> PlanWindow | None:
        """The window as it stands at ``now``.

        Past its reset a window is empty again, which is known whether or not
        a figure was: one that had a figure is at 0%. One that had none had
        only its reset to show, and that is gone - so there is nothing left
        to show, rather than a guess. Nor is there for one with neither.
        """
        now = time.time() if now is None else now
        if self.used_percent is None and self.resets_at is None:
            return None
        if self.resets_at is None or now < self.resets_at:
            return self
        if self.used_percent is None:
            return None
        return replace(self, used_percent=0.0, resets_at=None)


def format_percent(used_percent: float) -> str:
    """``95%``. Never ``100%`` short of the limit: a window rounded up to full
    reads as one that refuses the next turn."""
    shown = 100 if used_percent >= 100 else min(round(used_percent), 99)
    return f"{max(shown, 0)}%"


RENEWAL_CLOCK_SECONDS = 86_400
"""Closer than this, a renewal is given as a clock time alone."""


def format_renewal(resets_at: float, *, short: bool = False, now: float | None = None) -> str:
    """When a window renews, in local time: ``06:30 am`` within a day, and
    ``Mon Oct 26 04:00 am`` beyond it. ``short`` drops the clock from a date -
    ``Mon Oct 26`` - where room is scarce: days away, the day is what counts.
    """
    now = time.time() if now is None else now
    when = time.localtime(resets_at)
    clock = time.strftime("%I:%M %p", when).lower()
    if resets_at - now < RENEWAL_CLOCK_SECONDS:
        return clock
    day = f"{time.strftime('%a %b', when)} {when.tm_mday}"
    return day if short else f"{day} {clock}"


def compute_cost(usage: TurnUsage, pricing: ModelPricing) -> float:
    """Local cost fallback when the provider reports none.

    Cache reads and writes are priced separately from ordinary input tokens, and
    ``input_tokens`` deliberately *excludes* cached tokens - double-counting here
    silently inflates every cost display.
    """
    cache_read_rate = pricing.cache_read or pricing.prompt
    cache_write_rate = pricing.cache_write or pricing.prompt
    return (
        usage.input_tokens * pricing.prompt
        + usage.output_tokens * pricing.completion
        + usage.cache_read_tokens * cache_read_rate
        + usage.cache_write_tokens * cache_write_rate
        + usage.reasoning_tokens * pricing.reasoning
    )


def format_cost(cost_usd: float) -> str:
    """Human-readable cost for the status bar (e.g. ``$0.0431``)."""
    if cost_usd >= 100:
        return f"${cost_usd:,.0f}"
    if cost_usd >= 1:
        return f"${cost_usd:.2f}"
    return f"${cost_usd:.4f}"


def format_tokens(count: int) -> str:
    """Compact token count (e.g. ``12.4k``, ``1.2M``)."""
    if count < 1_000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1_000:.1f}k".replace(".0k", "k")
    return f"{count / 1_000_000:.1f}M".replace(".0M", "M")
