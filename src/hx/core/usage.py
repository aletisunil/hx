"""Token and cost ledger.

Feeds the status bar fields: context used, tokens used, cache-read/cache-write
tokens, and session cost. OpenRouter returns an authoritative ``usage.cost``
when ``usage: {include: true}`` is set on the request; we prefer that over our
own arithmetic and only compute locally as a fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
