"""Routes in the TUI: what /model offers, what /login and /logout do."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Button

from hx.auth.store import ApiKeyCredential, AuthStore, OAuthCredential
from hx.config import load_settings
from hx.core.context import ContextBuilder
from hx.core.events import EventBus
from hx.core.lateinject import InjectionRegistry
from hx.core.loop import AgentLoop
from hx.core.session import new_session
from hx.providers.fake import FakeProvider, text_turn
from hx.providers.models import CODEX_MODELS, ModelRegistry
from hx.tools.registry import ToolRegistry
from hx.tui.app import HXApp
from hx.tui.widgets.login import LoginModal as LoginModalType

MODEL = "anthropic/claude-sonnet-4.5"
CODEX_MODEL = CODEX_MODELS[0].id
"""One model off the fallback list, whatever it currently is.

The list is what HX ships for an account it has not been able to ask, and it
moves whenever OpenAI does; naming an id here would make every one of those
moves a test failure with nothing wrong behind it."""


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HX_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def build_app(tmp_path: Path, model: str = MODEL) -> HXApp:
    bus = EventBus()
    models = ModelRegistry()
    models._models = {
        MODEL: models.get_or_default(MODEL),
        "openai/gpt-5": models.get_or_default("openai/gpt-5"),
        **{info.id: info for info in CODEX_MODELS},
    }
    session = new_session(tmp_path, model)
    session.set_title("route session")
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


async def settle(pilot: Any, until: Callable[[], bool], *, steps: int = 200) -> None:
    """Pump the app until ``until`` holds, instead of waiting a fixed delay.

    A login runs across several tasks - the flow, the modal's mount, the token
    exchange - and how many event-loop turns that takes is a property of the
    machine, not of the code under test. A tenth of a second was enough
    locally and not enough on a loaded CI runner.
    """
    for _ in range(steps):
        if until():
            return
        await pilot.pause()
    raise AssertionError("the app never reached the state the test was waiting for")


def signed_in_to_codex() -> None:
    AuthStore().save(
        "openai-codex",
        OAuthCredential(access="a", refresh="r", expires=0.0, extra={"account_id": "acct"}),
    )


async def picker_rows(app: HXApp) -> list[str]:
    from hx.tui.commands import _reachable

    return [model.id for model in _reachable(app._command_context(), app.models.all())]


async def test_the_picker_hides_routes_with_no_credential(hx_home: Path, tmp_path: Path) -> None:
    """Offering a model that cannot be called turns a choice into a failed turn."""
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert CODEX_MODEL not in await picker_rows(app)


async def test_signing_in_makes_the_codex_models_selectable(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)
    signed_in_to_codex()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert CODEX_MODEL in await picker_rows(app)


async def test_the_running_route_stays_listed_even_without_a_stored_credential(
    hx_home: Path, tmp_path: Path
) -> None:
    """The session is demonstrably working on it; hiding it helps nobody."""
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert MODEL in await picker_rows(app)


async def test_switching_to_a_subscription_model_swaps_the_provider(
    hx_home: Path, tmp_path: Path
) -> None:
    """Only the model name changing would send a Codex id to OpenRouter."""
    app = build_app(tmp_path)
    signed_in_to_codex()
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.submit(f"/model {CODEX_MODEL}")
        await pilot.pause()

        assert app.loop.model == CODEX_MODEL
        assert app.loop.provider.name == "openai-codex"
        assert app.loop.provider.session_id == app.loop.session.meta.session_id


async def test_switching_within_one_route_leaves_the_provider_alone(
    hx_home: Path, tmp_path: Path
) -> None:
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        original = app.loop.provider
        await app.submit("/model openai/gpt-5")
        await pilot.pause()

        assert app.loop.model == "openai/gpt-5"
        assert app.loop.provider is original


async def test_switching_to_a_route_without_a_credential_says_so(
    hx_home: Path, tmp_path: Path
) -> None:
    """The model must not change either - a half-applied switch is worse."""
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.submit(f"/model {CODEX_MODEL}")
        await pilot.pause()

        assert app.loop.model == MODEL
        notices = " ".join(str(n.render()) for n in app._transcript.query("Notice"))
        assert "hx auth login openai-codex" in notices


async def test_the_status_bar_says_subscription_instead_of_a_price(
    hx_home: Path, tmp_path: Path
) -> None:
    app = build_app(tmp_path)
    signed_in_to_codex()
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.submit(f"/model {CODEX_MODEL}")
        await pilot.pause()

        status = app._status
        assert status.subscription is True
        rendered = str(status.render())
        assert "sub" in rendered
        assert "$" not in rendered, "a per-token price is not a number the user can act on"


async def test_login_lists_every_route_with_its_state(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)
    AuthStore().save("openrouter", ApiKeyCredential(key="sk-or-test"))
    async with app.run_test() as pilot:
        worker = app.run_worker(app.commands.dispatch(app._command_context(), "/login"))
        await pilot.pause(0.1)

        labels = [str(button.label) for button in app.screen.query(Button)]
        assert any("OpenRouter" in label and "signed in" in label for label in labels)
        assert any("ChatGPT" in label and "not signed in" in label for label in labels)

        await pilot.press("escape")
        await worker.wait()


async def test_logout_forgets_the_credential(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)
    signed_in_to_codex()
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.submit("/logout openai-codex")
        await pilot.pause()

        assert AuthStore().read("openai-codex") is None
        assert CODEX_MODEL not in await picker_rows(app)


async def test_logout_without_a_provider_names_the_options(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.submit("/logout")
        await pilot.pause()

        notices = " ".join(str(n.render()) for n in app._transcript.query("Notice"))
        assert "openai-codex" in notices


async def test_the_login_modal_hands_a_pasted_url_to_the_flow(
    hx_home: Path, tmp_path: Path
) -> None:
    """Over SSH the browser callback never arrives, so this is the only path."""
    import asyncio

    from textual.widgets import Input

    from hx.tui.widgets.login import LoginModal

    app = build_app(tmp_path)
    modal = LoginModal("OpenAI (ChatGPT Plus/Pro)")
    async with app.run_test() as pilot:
        app.push_screen(modal)
        await pilot.pause()

        pasted = asyncio.ensure_future(modal.prompt_paste("paste:"))
        await pilot.pause()
        modal.query_one("#login-input", Input).value = "http://localhost:1455/auth/callback?code=c"
        await pilot.press("enter")
        assert await asyncio.wait_for(pasted, timeout=2) == (
            "http://localhost:1455/auth/callback?code=c"
        )


async def test_cancelling_the_login_modal_unblocks_the_flow(hx_home: Path, tmp_path: Path) -> None:
    import asyncio

    from hx.tui.widgets.login import LoginModal

    app = build_app(tmp_path)
    modal = LoginModal("OpenAI (ChatGPT Plus/Pro)")
    async with app.run_test() as pilot:
        app.push_screen(modal)
        await pilot.pause()

        pasted = asyncio.ensure_future(modal.prompt_paste("paste:"))
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        with pytest.raises(asyncio.CancelledError):
            await pasted


async def test_a_flow_that_fails_early_still_tears_the_modal_down(
    hx_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An orphaned modal is worse than a failed login: nothing can dismiss it."""
    import hx.auth.oauth.codex as codex

    async def exploding_login(interaction: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(codex, "login_browser", exploding_login)

    app = build_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.submit("/login openai-codex")

        def reported_the_failure() -> bool:
            return "boom" in " ".join(str(n.render()) for n in app._transcript.query("Notice"))

        await settle(pilot, reported_the_failure)

        assert not isinstance(app.screen, LoginModalType)


async def test_opening_the_browser_never_blocks_the_event_loop(
    hx_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``webbrowser.open`` shells out to ``osascript`` on macOS and waits for it.

    On the event loop that freezes every key the user presses - including the
    Escape that would cancel the login and the field it asks them to paste into.
    """
    import asyncio
    import contextlib
    import threading

    import hx.auth.oauth.codex as codex

    release = threading.Event()
    ran_on: list[str] = []

    def slow_open(url: str) -> bool:
        ran_on.append(threading.current_thread().name)
        release.wait(5)
        return True

    monkeypatch.setattr(codex.webbrowser, "open", slow_open)

    alive = asyncio.Event()

    async def heartbeat() -> None:
        for _ in range(3):
            await asyncio.sleep(0.01)
        alive.set()

    class Silent:
        """A LoginInteraction that says nothing and never finishes."""

        def show_url(self, url: str, instructions: str) -> None: ...
        def show_device_code(self, user_code: str, verification_uri: str) -> None: ...
        def progress(self, message: str) -> None: ...

        async def prompt_paste(self, message: str) -> str:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    beat = asyncio.create_task(heartbeat())
    flow = asyncio.create_task(codex.login_browser(Silent()))
    try:
        await asyncio.wait_for(alive.wait(), timeout=3)
    finally:
        release.set()
        flow.cancel()
        beat.cancel()
        for task in (flow, beat):
            with contextlib.suppress(asyncio.CancelledError, OSError):
                await task

    assert ran_on, "the browser was never opened"
    assert ran_on[0] != threading.main_thread().name, "the open ran on the event loop"
