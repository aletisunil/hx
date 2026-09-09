"""Live end-to-end against the real Codex backend, on a ChatGPT subscription.

Deselected by default (``-m live``). Needs a stored ``openai-codex`` login;
sign in first with::

    hx auth login openai-codex
    uv run pytest -m live tests/test_live_codex.py -s

This is the only place the Responses wire format, its SSE events, the reasoning
replay and the implicit prompt cache are proven against the real service.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hx.auth.resolve import AuthResolver
from hx.config import PermissionMode, load_settings
from hx.core.context import ContextBuilder, load_system_prompt
from hx.core.events import EventBus
from hx.core.lateinject import InjectionRegistry
from hx.core.loop import AgentLoop
from hx.core.messages import ThinkingBlock
from hx.core.session import new_session
from hx.permissions.engine import Decision, PermissionEngine, parse_rule
from hx.providers import registry
from hx.providers.base import Provider
from hx.providers.models import ModelRegistry
from hx.tools.read import FileTracker
from hx.tools.registry import build_default_registry

pytestmark = pytest.mark.live

MODEL = os.environ.get("HX_LIVE_CODEX_MODEL", "openai-codex/gpt-5.3-codex")


def _resolver() -> AuthResolver:
    resolver = AuthResolver()
    if not resolver.has_credential("openai-codex"):
        pytest.skip("not signed in; run `hx auth login openai-codex`")
    return resolver


def _build(tmp_path: Path) -> tuple[AgentLoop, Provider]:
    """Deliberately routed through the registry, so the model id picks the provider."""
    models = ModelRegistry()
    session = new_session(tmp_path, MODEL)
    provider = registry.build_provider(MODEL, _resolver(), session_id=session.meta.session_id)
    assert provider.name == "openai-codex", "the model id should have selected the Codex route"

    loop = AgentLoop(
        provider=provider,
        session=session,
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


async def test_a_real_subscription_turn_streams(hx_home: Path, tmp_path: Path) -> None:
    loop, provider = _build(tmp_path)
    try:
        result = await loop.run("Reply with exactly the word: pong")
    finally:
        await provider.aclose()

    assert result.error is None
    assert "pong" in loop.session.messages[-1].text().lower()

    usage = loop.session.usage
    assert usage.total_input > 0
    assert usage.total_output > 0


async def test_the_model_can_use_a_tool_over_the_responses_api(
    hx_home: Path, tmp_path: Path
) -> None:
    """Proves the flat tool schema and the split argument fragments both decode."""
    (tmp_path / "secret.txt").write_text("the answer is 42\n")

    loop, provider = _build(tmp_path)
    try:
        await loop.run("Read secret.txt in the working directory and tell me the answer.")
    finally:
        await provider.aclose()

    assert any(m.tool_uses() for m in loop.session.messages)
    assert "42" in loop.session.messages[-1].text()


async def test_reasoning_is_captured_for_replay(hx_home: Path, tmp_path: Path) -> None:
    """Without the opaque payload the next turn re-reasons and misses the cache."""
    loop, provider = _build(tmp_path)
    try:
        await loop.run("Think briefly, then reply with just: ok")
    finally:
        await provider.aclose()

    thinking = [
        block
        for message in loop.session.messages
        for block in message.content
        if isinstance(block, ThinkingBlock)
    ]
    assert thinking, "no reasoning item came back"
    assert any(block.signature for block in thinking), "reasoning arrived with no opaque payload"


async def test_the_prompt_cache_actually_gets_hit(hx_home: Path, tmp_path: Path) -> None:
    """Turn one writes the cache; turn two must read from it."""
    loop, provider = _build(tmp_path)
    try:
        (tmp_path / "context.py").write_text("# padding\n" * 2000)
        await loop.run("Read context.py, then reply with just: one")
        first_read = loop.session.usage.total_cache_read

        await loop.run("Now reply with just: two")
        second_read = loop.session.usage.total_cache_read
    finally:
        await provider.aclose()

    assert second_read > first_read, (
        f"no cache hit on the second turn (read {first_read} then {second_read}); "
        "the request prefix or prompt_cache_key is not stable"
    )
