"""Live end-to-end against the real Tavily API.

Deselected by default (``addopts`` carries ``-m 'not live'``), and skipped even
when selected without a key: it spends real credits and talks to the network.
This is the only place the actual response shape is proven - every other web
search test replays a canned body, which cannot catch a renamed field.

Run with::

    TAVILY_API_KEY=... uv run pytest -m live -k tavily -s
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hx.auth.resolve import AuthResolver
from hx.auth.store import TAVILY, ApiKeyCredential, AuthStore
from hx.config import load_settings
from hx.tools.base import ToolContext
from hx.tools.websearch import CreditLedger, WebFetchTool, WebSearchTool

pytestmark = pytest.mark.live


@pytest.fixture()
def tavily_key() -> str:
    """Gate on a real key, for every test here - including the ones that send a
    deliberately bad one. Without this a test that needs only the network and
    not the key still runs whenever the marker is not filtered, and then fails
    offline for a reason that has nothing to do with what it asserts."""
    key = os.environ.get("HX_TAVILY_API_KEY") or os.environ.get("TAVILY_API_KEY")
    if not key:
        pytest.skip("no Tavily API key in the environment")
    return key


@pytest.fixture()
def auth(tmp_path: Path, tavily_key: str) -> AuthResolver:
    store = AuthStore(tmp_path / "auth.json")
    store.save(TAVILY, ApiKeyCredential(key=tavily_key))
    return AuthResolver(store)


@pytest.fixture()
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        cwd=tmp_path,
        session_id="live-tavily",
        tool_use_id="t1",
        settings=load_settings(tmp_path),
        emit_progress=lambda chunk: print(chunk, end=""),
    )


async def test_search_returns_usable_results(ctx: ToolContext, auth: AuthResolver) -> None:
    ledger = CreditLedger()
    result = await WebSearchTool(auth, ledger).run(
        {"query": "textual python tui library", "max_results": 3}, ctx
    )
    print(result.content)

    assert not result.is_error
    assert "https://" in result.content
    # A basic search is 1 credit whether Tavily reports it or HX prices it.
    assert ledger.credits == 1
    assert result.metadata["credits"] == 1
    print(f"cost reported by Tavily: {ledger.estimates == 0}")


async def test_fetch_returns_page_markdown(ctx: ToolContext, auth: AuthResolver) -> None:
    result = await WebFetchTool(auth).run({"urls": ["https://example.com"]}, ctx)
    print(result.content)

    assert not result.is_error
    assert "Example Domain" in result.content
    assert result.metadata["credits"] == 1


async def test_a_bad_key_is_reported_as_something_actionable(
    ctx: ToolContext, tmp_path: Path, tavily_key: str
) -> None:
    """A 401 must name the fix; the raw status sends the model guessing.

    The mapping itself is proven offline in ``tests/tools/test_websearch.py``.
    What is live here is that Tavily really answers a bad key with 401 rather
    than 403 or a 200 carrying an error body - which no canned response can
    tell us.
    """
    from hx.tools.base import ToolError

    store = AuthStore(tmp_path / "bad.json")
    store.save(TAVILY, ApiKeyCredential(key="tvly-dev-definitely-not-a-real-key"))

    with pytest.raises(ToolError, match="hx auth set tavily"):
        await WebSearchTool(AuthResolver(store)).run({"query": "hello"}, ctx)
