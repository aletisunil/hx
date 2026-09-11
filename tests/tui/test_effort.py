"""``/effort``: how hard the model thinks, from the levels that model offers."""

from __future__ import annotations

import json
from pathlib import Path

from hx.config import load_settings
from hx.core.context import ContextBuilder
from hx.core.events import EventBus
from hx.core.lateinject import InjectionRegistry
from hx.core.loop import AgentLoop
from hx.core.session import new_session
from hx.paths import user_settings_file
from hx.providers.fake import FakeProvider, text_turn
from hx.providers.models import CODEX_MODELS, ModelRegistry
from hx.tools.registry import ToolRegistry
from hx.tui.app import HXApp

DEEPEST = max(CODEX_MODELS, key=lambda m: len(m.reasoning_levels))
SHALLOWEST = min(CODEX_MODELS, key=lambda m: len(m.reasoning_levels))


def build_app(tmp_path: Path, model: str) -> HXApp:
    bus = EventBus()
    models = ModelRegistry()
    models.load_cache()
    session = new_session(tmp_path, model)
    session.set_title("effort session")
    loop = AgentLoop(
        provider=FakeProvider([text_turn("hi")]),
        session=session,
        tools=ToolRegistry(),
        permissions=None,
        context=ContextBuilder("sys", tmp_path),
        compactor=None,
        injections=InjectionRegistry(),
        bus=bus,
        settings=load_settings(tmp_path),
        model_info=models.get_or_default(model),
    )
    return HXApp(loop, bus, load_settings(tmp_path), models=models)


def _notices(app: HXApp) -> str:
    return " ".join(str(n.render()) for n in app._transcript.query("Notice"))


async def test_an_effort_applies_to_the_next_turn_and_is_saved(
    hx_home: Path, tmp_path: Path
) -> None:
    """The provider asks per request, so nothing has to be rebuilt - and the
    choice is worth keeping for the sessions after this one."""
    app = build_app(tmp_path, DEEPEST.id)
    async with app.run_test() as pilot:
        await app.submit("/effort high")
        await pilot.pause()

        assert app.models.requested_effort == "high"
        assert app.models.reasoning_effort(DEEPEST.id) == "high"
        assert app._status.effort == "high"
        saved = json.loads(user_settings_file().read_text())
        assert saved["models"]["reasoning_effort"] == "high"


async def test_an_effort_the_model_cannot_reach_says_where_it_lands(
    hx_home: Path, tmp_path: Path
) -> None:
    """Clamped rather than refused, and silence would read as the deeper
    setting having taken."""
    ceiling = SHALLOWEST.reasoning_levels[-1]
    beyond = next(
        level
        for level in reversed(DEEPEST.reasoning_levels)
        if level not in SHALLOWEST.reasoning_levels
    )

    app = build_app(tmp_path, SHALLOWEST.id)
    async with app.run_test() as pilot:
        await app.submit(f"/effort {beyond}")
        await pilot.pause()

        assert app.models.reasoning_effort(SHALLOWEST.id) == ceiling
        assert f"tops out at {ceiling}" in _notices(app)


async def test_default_hands_each_model_back_its_own_depth(hx_home: Path, tmp_path: Path) -> None:
    """One fixed depth for every model is a decision nobody asked for: the
    catalogue sets it per model."""
    app = build_app(tmp_path, DEEPEST.id)
    async with app.run_test() as pilot:
        await app.submit("/effort high")
        await pilot.pause()
        await app.submit("/effort default")
        await pilot.pause()

        assert app.models.requested_effort is None
        assert app.models.reasoning_effort(DEEPEST.id) == DEEPEST.default_reasoning_level
        assert "reasoning_effort" not in json.loads(user_settings_file().read_text())["models"]


async def test_an_unknown_level_lists_the_ones_that_exist(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path, DEEPEST.id)
    async with app.run_test() as pilot:
        await app.submit("/effort telepathic")
        await pilot.pause()

        assert "Unknown effort 'telepathic'" in _notices(app)
        assert app.models.requested_effort is None


async def test_a_model_with_no_levels_has_nothing_to_pick_from(
    hx_home: Path, tmp_path: Path
) -> None:
    """Only the Codex catalogue publishes them; a picker of guesses is worse
    than a sentence saying where they come from."""
    app = build_app(tmp_path, "anthropic/claude-sonnet-4.5")
    async with app.run_test() as pilot:
        await app.submit("/effort")
        await pilot.pause()

        assert "publishes no reasoning levels" in _notices(app)


async def test_switching_models_re_resolves_the_depth(hx_home: Path, tmp_path: Path) -> None:
    """The bar has to say what will really run, at the moment it changes."""
    app = build_app(tmp_path, DEEPEST.id)
    async with app.run_test() as pilot:
        await app.submit(f"/effort {DEEPEST.reasoning_levels[-1]}")
        await pilot.pause()
        assert app._status.effort == DEEPEST.reasoning_levels[-1]

        app.models.set_reasoning_effort(DEEPEST.reasoning_levels[-1])
        app._status.set_effort(app.models.displayed_effort(SHALLOWEST.id))

        assert app._status.effort == SHALLOWEST.reasoning_levels[-1]


def test_a_resume_row_leads_with_the_number_the_user_recognises() -> None:
    """A prompt is something they typed; a message is a wire-format record, and
    a one-prompt session reading "31 msgs" describes the protocol."""
    from dataclasses import dataclass

    from hx.tui.widgets.palette import _size

    @dataclass
    class _Meta:
        prompt_count: int
        message_count: int

    assert _size(_Meta(1, 31)) == "1 prompt · 31 msgs"
    assert _size(_Meta(4, 57)) == "4 prompts · 57 msgs"
    # Recorded before prompts were counted: nothing to lead with, so it keeps
    # the old shape rather than claiming zero prompts.
    assert _size(_Meta(0, 12)) == "12 msgs"
