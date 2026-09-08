"""Token and cost accounting."""

from __future__ import annotations

from hx.core.usage import TurnUsage, UsageLedger, compute_cost, format_tokens
from hx.providers.models import ModelPricing


def test_cached_tokens_are_not_double_counted() -> None:
    """OpenRouter's prompt_tokens includes cached tokens; billing them at both
    the full and the cached rate silently inflates every cost display."""
    usage = TurnUsage(input_tokens=1000, cache_read_tokens=9000, output_tokens=100)
    pricing = ModelPricing(prompt=1e-6, completion=2e-6, cache_read=1e-7)
    assert compute_cost(usage, pricing) < 11 * 1e-6 * 1000


def test_provider_reported_cost_wins() -> None:
    ledger = UsageLedger()
    ledger.record(TurnUsage(input_tokens=100, output_tokens=10, cost_usd=0.5))
    assert ledger.total_cost_usd == 0.5


def test_format_tokens_is_compact() -> None:
    assert format_tokens(12_400) == "12.4k"
    assert format_tokens(1_200_000) == "1.2M"
