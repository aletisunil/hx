"""Live end-to-end against the real OpenRouter API.

Deselected by default (``-m live``); it costs real money and needs a key. This
is the only place the actual wire format, streaming, caching and billing are
proven - everything else runs against the scripted provider.

Run with::

    OPENROUTER_API_KEY=... uv run pytest -m live -s
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hx.config import PermissionMode, load_settings
from hx.core.context import ContextBuilder, load_system_prompt
from hx.core.events import EventBus
from hx.core.lateinject import InjectionRegistry
from hx.core.loop import AgentLoop
from hx.core.session import new_session
from hx.permissions.engine import Decision, PermissionEngine, parse_rule
from hx.providers.models import ModelRegistry
from hx.providers.openrouter import OpenRouterProvider
from hx.tools.read import FileTracker
from hx.tools.registry import build_default_registry

pytestmark = pytest.mark.live

MODEL = os.environ.get("HX_LIVE_MODEL", "anthropic/claude-haiku-4.5")


def _key() -> str:
    key = os.environ.get("HX_OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        pytest.skip("no OpenRouter API key in the environment")
    return key


async def _build(tmp_path: Path) -> tuple[AgentLoop, OpenRouterProvider]:
    provider = OpenRouterProvider(_key())
    models = ModelRegistry()
    await models.refresh(_key())

    loop = AgentLoop(
        provider=provider,
        session=new_session(tmp_path, MODEL),
        tools=build_default_registry(None, None, FileTracker()),
        permissions=PermissionEngine(
            PermissionMode.DEFAULT, [parse_rule("Read(**)", "live", Decision.ALLOW)], tmp_path
        ),
        context=ContextBuilder(load_system_prompt(tmp_path), tmp_path),
        compactor=None,
        injections=InjectionRegistry(),
        bus=EventBus(),
        settings=load_settings(tmp_path),
        model_info=models.get_or_default(MODEL),
    )
    return loop, provider


async def test_a_real_turn_streams_and_bills(hx_home: Path, tmp_path: Path) -> None:
    loop, provider = await _build(tmp_path)
    try:
        result = await loop.run("Reply with exactly the word: pong")
    finally:
        await provider.aclose()

    assert result.error is None
    assert "pong" in loop.session.messages[-1].text().lower()

    usage = loop.session.usage
    assert usage.total_input > 0
    assert usage.total_output > 0
    assert usage.total_cost_usd > 0, "OpenRouter should report a cost with usage.include set"


async def test_the_model_can_actually_use_a_tool(hx_home: Path, tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("the answer is 42\n")

    loop, provider = await _build(tmp_path)
    try:
        await loop.run("Read secret.txt in the working directory and tell me the answer.")
    finally:
        await provider.aclose()

    assert any(m.tool_uses() for m in loop.session.messages)
    assert "42" in loop.session.messages[-1].text()


async def test_the_prefix_cache_actually_gets_hit(hx_home: Path, tmp_path: Path) -> None:
    """The design's central claim, proven against a real provider.

    Turn one writes the cache; turn two must read from it. If this fails, every
    request in the harness is paying full price for its prefix.
    """
    loop, provider = await _build(tmp_path)
    try:
        # A prefix large enough to clear the provider's minimum cacheable size.
        (tmp_path / "context.py").write_text("# padding\n" * 2000)
        await loop.run("Read context.py, then reply with just: one")
        first_read = loop.session.usage.total_cache_read

        await loop.run("Now reply with just: two")
        second_read = loop.session.usage.total_cache_read
    finally:
        await provider.aclose()

    assert second_read > first_read, (
        f"no cache hit on the second turn (read {first_read} then {second_read}); "
        "the request prefix is not stable"
    )
