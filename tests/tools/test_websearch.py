"""WebSearch and WebFetch, against a stubbed Tavily.

The transport is faked at the httpx layer rather than at ``_post``: the
translation of a status code into a sentence the model can act on is most of
what these tools do, and stubbing above it would test nothing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from hx import net
from hx.auth.resolve import AuthResolver
from hx.auth.store import TAVILY, ApiKeyCredential, AuthStore
from hx.config import load_settings
from hx.tools.base import ToolContext, ToolError
from hx.tools.registry import build_default_registry
from hx.tools.websearch import (
    CreditLedger,
    WebFetchTool,
    WebSearchTool,
)

SEARCH_BODY = {
    "query": "textual widgets",
    "results": [
        {
            "title": "Textual Widgets",
            "url": "https://textual.textualize.io/widgets/",
            "content": "A widget is a component that renders itself.",
            "score": 0.9,
        },
        {
            "title": "Custom widgets",
            "url": "https://textual.textualize.io/guide/widgets/",
            "content": "Build your own by subclassing Widget.",
            "score": 0.7,
        },
    ],
    "usage": {"credits": 1},
    "request_id": "req-search-1",
}

EXTRACT_BODY = {
    "results": [
        {
            "url": "https://example.com/a",
            "title": "Example A",
            "raw_content": "# Title\n\nBody text.",
        }
    ],
    "failed_results": [{"url": "https://example.com/b", "error": "timed out"}],
    "usage": {"credits": 2},
    "request_id": "req-extract-1",
}


@pytest.fixture()
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        cwd=tmp_path,
        session_id="test-session",
        tool_use_id="t1",
        settings=load_settings(tmp_path),
        emit_progress=lambda _chunk: None,
    )


@pytest.fixture()
def auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AuthResolver:
    """A resolver holding a Tavily key, with the environment cleared.

    A developer's real ``TAVILY_API_KEY`` would otherwise decide which branch
    of :meth:`AuthResolver.source` these assertions land on.
    """
    for name in ("HX_TAVILY_API_KEY", "TAVILY_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    store = AuthStore(tmp_path / "auth.json")
    store.save(TAVILY, ApiKeyCredential(key="tvly-test-key"))
    return AuthResolver(store)


def stub_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    *,
    seen: list[httpx.Request] | None = None,
) -> None:
    """Point every client built through :func:`hx.net.async_client` at ``handler``."""

    def record(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return handler(request)

    def fake_client(**kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("verify", None)
        return httpx.AsyncClient(transport=httpx.MockTransport(record), **kwargs)

    monkeypatch.setattr(net, "async_client", fake_client)


async def test_search_renders_ranked_results(
    ctx: ToolContext, auth: AuthResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[httpx.Request] = []
    stub_transport(monkeypatch, lambda _r: httpx.Response(200, json=SEARCH_BODY), seen=seen)

    result = await WebSearchTool(auth).run({"query": "textual widgets"}, ctx)

    assert not result.is_error
    assert "1. Textual Widgets" in result.content
    assert "https://textual.textualize.io/widgets/" in result.content
    assert result.summary == "2 results"
    assert seen[0].headers["authorization"] == "Bearer tvly-test-key"


async def test_search_sends_only_valid_parameters(
    ctx: ToolContext, auth: AuthResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invented enum value falls back to the default instead of failing the turn."""
    import json

    seen: list[httpx.Request] = []
    stub_transport(monkeypatch, lambda _r: httpx.Response(200, json=SEARCH_BODY), seen=seen)

    await WebSearchTool(auth).run(
        {
            "query": "q",
            "search_depth": "deep",
            "max_results": 99,
            "time_range": "fortnight",
            "include_domains": ["docs.python.org", "  ", 7],
        },
        ctx,
    )

    sent = json.loads(seen[0].content)
    assert sent["search_depth"] == "basic"
    assert sent["max_results"] == 20
    assert "time_range" not in sent
    assert sent["include_domains"] == ["docs.python.org"]


async def test_credits_accumulate_across_both_tools(
    ctx: ToolContext, auth: AuthResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One key, one bill - a per-tool counter would report half of it."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = EXTRACT_BODY if request.url.path == "/extract" else SEARCH_BODY
        return httpx.Response(200, json=body)

    stub_transport(monkeypatch, handler)
    ledger = CreditLedger()
    search, fetch = WebSearchTool(auth, ledger), WebFetchTool(auth, ledger)

    await search.run({"query": "q"}, ctx)
    result = await fetch.run({"urls": ["https://example.com/a"]}, ctx)

    assert ledger.credits == 3
    assert ledger.calls == 2
    assert ledger.estimates == 0
    assert "3 this session" in result.content
    assert result.metadata["credits"] == 2


async def test_an_unreported_cost_is_priced_and_marked_as_an_estimate(
    ctx: ToolContext, auth: AuthResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tavily documents ``usage.credits`` but does not always send it. Reporting
    0 there would be a lie in the cheaper direction, which is the worse one."""
    body = {k: v for k, v in SEARCH_BODY.items() if k != "usage"}
    stub_transport(monkeypatch, lambda _r: httpx.Response(200, json=body))
    ledger = CreditLedger()

    result = await WebSearchTool(auth, ledger).run({"query": "q", "search_depth": "advanced"}, ctx)

    assert ledger.credits == 2  # advanced search, from the published price list
    assert ledger.estimates == 1
    assert "[Tavily: ~2 credits, ~2 this session]" in result.content
    assert result.metadata["credits"] == 2


async def test_a_reported_cost_wins_over_the_price_list(
    ctx: ToolContext, auth: AuthResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tavily is the authority on its own bill when it chooses to speak."""
    body = {**SEARCH_BODY, "usage": {"credits": 7}}
    stub_transport(monkeypatch, lambda _r: httpx.Response(200, json=body))
    ledger = CreditLedger()

    result = await WebSearchTool(auth, ledger).run({"query": "q"}, ctx)

    assert ledger.credits == 7
    assert ledger.estimates == 0
    assert "[Tavily: 7 credits, 7 this session]" in result.content


async def test_fetch_reports_partial_failure(
    ctx: ToolContext, auth: AuthResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_transport(monkeypatch, lambda _r: httpx.Response(200, json=EXTRACT_BODY))

    result = await WebFetchTool(auth).run(
        {"urls": ["https://example.com/a", "https://example.com/b"]}, ctx
    )

    assert "Body text." in result.content
    assert "## Example A" in result.content
    assert "Failed: timed out" in result.content
    assert result.summary == "1 page(s), 1 failed"
    # Kept out of the model's view, but recorded where you look afterwards.
    assert result.metadata["request_id"] == "req-extract-1"
    assert "req-extract-1" not in result.content


async def test_fetch_refuses_more_urls_than_a_credit_buys(
    ctx: ToolContext, auth: AuthResolver
) -> None:
    with pytest.raises(ToolError, match="limit is 5"):
        await WebFetchTool(auth).run({"urls": [f"https://e.com/{i}" for i in range(6)]}, ctx)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "hx auth set tavily"),
        (432, "monthly credits are spent"),
        (429, "rate limit"),
    ],
)
async def test_api_failures_name_the_fix(
    ctx: ToolContext,
    auth: AuthResolver,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    expected: str,
) -> None:
    stub_transport(monkeypatch, lambda _r: httpx.Response(status, json={"detail": "nope"}))

    with pytest.raises(ToolError, match=expected):
        await WebSearchTool(auth).run({"query": "q"}, ctx)


async def test_network_failure_is_one_line(
    ctx: ToolContext, auth: AuthResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    stub_transport(monkeypatch, explode)

    with pytest.raises(ToolError, match=re.escape("Tavily: Cannot reach api.tavily.com")):
        await WebSearchTool(auth).run({"query": "q"}, ctx)


async def test_missing_key_tells_the_user_how_to_add_one(
    ctx: ToolContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("HX_TAVILY_API_KEY", "TAVILY_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    empty = AuthResolver(AuthStore(tmp_path / "none.json"))

    with pytest.raises(ToolError, match="hx auth set tavily"):
        await WebSearchTool(empty).run({"query": "q"}, ctx)


def test_the_query_is_the_permission_specifier(auth: AuthResolver) -> None:
    """Non-mutating, so allowed by default - but a deny rule needs a target."""
    assert WebSearchTool(auth).permission_specifier({"query": "secrets"}) == "secrets"
    assert WebSearchTool(auth).mutating is False


def test_tools_are_absent_without_a_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An advertised tool that always fails costs cached-prefix tokens forever."""
    from hx.tools.read import FileTracker

    for name in ("HX_TAVILY_API_KEY", "TAVILY_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    without = build_default_registry(
        None, None, FileTracker(), auth=AuthResolver(AuthStore(tmp_path / "none.json"))
    )
    assert "WebSearch" not in without.names()


def test_tools_appear_once_a_key_exists(auth: AuthResolver) -> None:
    from hx.tools.read import FileTracker

    registry = build_default_registry(None, None, FileTracker(), auth=auth)
    assert {"WebSearch", "WebFetch"} <= set(registry.names())
