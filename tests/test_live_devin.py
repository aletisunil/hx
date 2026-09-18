"""Live end-to-end against the real Devin (Cascade) backend, on a Devin subscription.

Deselected by default (``-m live``). Needs a stored ``devin`` login; sign in
first with::

    hx auth login devin
    uv run pytest -m live tests/test_live_devin.py -s

This is the only place the hand-rolled protobuf, the Connect framing, the
user-JWT exchange and the model catalogue are proven against the real service.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hx.auth.resolve import AuthResolver
from hx.auth.store import AuthStore
from hx.config import PermissionMode, load_settings
from hx.core.context import ContextBuilder, load_system_prompt
from hx.core.events import EventBus
from hx.core.lateinject import InjectionRegistry
from hx.core.loop import AgentLoop
from hx.core.session import new_session
from hx.permissions.engine import Decision, PermissionEngine, parse_rule
from hx.providers import devin_catalogue, registry
from hx.providers.base import Provider
from hx.providers.models import ModelRegistry
from hx.tools.read import FileTracker
from hx.tools.registry import build_default_registry
from tests.conftest import REAL_AUTH_FILE

pytestmark = pytest.mark.live

MODEL = os.environ.get("HX_LIVE_DEVIN_MODEL", "devin/swe-1-6")


def _resolver() -> AuthResolver:
    resolver = AuthResolver(AuthStore(REAL_AUTH_FILE))
    if not resolver.has_credential("devin"):
        pytest.skip("not signed in; run `hx auth login devin`")
    return resolver


async def _models(resolver: AuthResolver) -> ModelRegistry:
    models = ModelRegistry()
    models.load_cache()
    await models.refresh(resolver)
    return models


async def _build(tmp_path: Path) -> tuple[AgentLoop, Provider]:
    """Routed through the registry, so the model id picks the provider."""
    resolver = _resolver()
    models = await _models(resolver)
    session = new_session(tmp_path, MODEL)
    provider = registry.build_provider(
        MODEL, resolver, session_id=session.meta.session_id, models=models
    )
    assert provider.name == "devin", "the model id should have selected the Devin route"

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


async def test_the_account_lists_its_models(hx_home: Path) -> None:
    resolver = _resolver()
    auth = await resolver.resolve("devin")
    models = await devin_catalogue.fetch_models(auth)

    ids = {model.id for model in models}
    assert ids, "the catalogue came back empty"
    assert all(model.provider_id == "devin" and model.is_subscription for model in models)
    print("\n".join(sorted(ids)))


async def test_a_real_subscription_turn_streams(hx_home: Path, tmp_path: Path) -> None:
    loop, provider = await _build(tmp_path)
    try:
        result = await loop.run("Reply with exactly the word: pong")
    finally:
        await provider.aclose()

    assert result.error is None, result.error
    assert "pong" in loop.session.messages[-1].text().lower()
    assert loop.session.usage.total_output > 0


async def test_the_model_can_use_a_tool(hx_home: Path, tmp_path: Path) -> None:
    """Proves tool schemas encode, tool calls stream, and results thread back."""
    (tmp_path / "secret.txt").write_text("the answer is 42\n")

    loop, provider = await _build(tmp_path)
    try:
        result = await loop.run("Read secret.txt in the working directory and tell me the answer.")
    finally:
        await provider.aclose()

    assert result.error is None, result.error
    assert any(m.tool_uses() for m in loop.session.messages)
    assert "42" in loop.session.messages[-1].text()


async def test_a_second_turn_continues_the_conversation(hx_home: Path, tmp_path: Path) -> None:
    """History - including the assistant turn this model produced - replays cleanly."""
    loop, provider = await _build(tmp_path)
    try:
        await loop.run("Remember the codeword: marmalade. Reply with just: ok")
        result = await loop.run("What was the codeword? Reply with just the word.")
    finally:
        await provider.aclose()

    assert result.error is None, result.error
    assert "marmalade" in loop.session.messages[-1].text().lower()
