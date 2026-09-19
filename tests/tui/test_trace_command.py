"""``/trace``: the whole session as one HTML page, from inside the session.

The point of the command over ``hx trace`` is that it traces the live object,
so a turn that has landed but whose usage has not been flushed is still in the
page. That is what these assert.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hx.config import load_settings
from hx.core.context import ContextBuilder
from hx.core.events import EventBus
from hx.core.lateinject import InjectionRegistry
from hx.core.loop import AgentLoop
from hx.core.messages import user_message
from hx.core.session import new_session
from hx.core.usage import TurnUsage
from hx.providers.fake import FakeProvider, text_turn
from hx.providers.models import ModelRegistry
from hx.term.terminal import FakeTerminal
from hx.term.width import strip_ansi
from hx.tools.registry import ToolRegistry
from hx.trace import TRACE_FILENAME, default_trace_path
from hx.tui.runtime import HXSession as HXApp
from hx.tui.views.blocks import Notice
from tests.tui.support import Driver


def build_app(tmp_path: Path) -> HXApp:
    bus = EventBus()
    models = ModelRegistry()
    models.load_cache()
    session = new_session(tmp_path, "anthropic/claude-sonnet-4.5")
    session.set_title("trace session")
    loop = AgentLoop(
        provider=FakeProvider([text_turn("hi")]),
        session=session,
        tools=ToolRegistry(),
        permissions=None,
        context=ContextBuilder("you are hx", tmp_path),
        compactor=None,
        injections=InjectionRegistry(),
        bus=bus,
        settings=load_settings(tmp_path),
        model_info=models.get_or_default("anthropic/claude-sonnet-4.5"),
        project_context="# AGENTS.md",
    )
    return HXApp(loop, bus, load_settings(tmp_path), terminal=FakeTerminal(80, 24), models=models)


def _notices(app: HXApp) -> str:
    return " ".join(
        strip_ansi(line)
        for block in app.view.transcript.blocks
        if isinstance(block, Notice)
        for line in block.render(200)
    )


def _payload(path: Path) -> dict:  # type: ignore[type-arg]
    page = path.read_text(encoding="utf-8")
    body = page.split('<script id="trace-data" type="application/json">')[1].split("</script>")[0]
    return json.loads(body)  # type: ignore[no-any-return]


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test must not open a browser window on whoever is running it.

    ``webbrowser.get`` rather than ``webbrowser.open``: the opener resolves a
    browser first so it can refuse a terminal one, and patching only ``open``
    would leave a real window to be launched on the developer's desktop.
    """
    import webbrowser

    def refuse(*_args: object) -> object:
        raise webbrowser.Error("no browser in tests")

    monkeypatch.setattr(webbrowser, "get", refuse)


async def test_trace_writes_beside_the_transcript_and_says_where(
    hx_home: Path, tmp_path: Path
) -> None:
    app = build_app(tmp_path)
    async with Driver(app) as driver:
        await app._run_command("/trace")
        await driver.settle()

    written = default_trace_path(app.loop.session)
    assert written.exists()
    assert str(written) in _notices(app)


async def test_trace_catches_state_that_has_not_been_flushed(hx_home: Path, tmp_path: Path) -> None:
    """The reason the command exists rather than only ``hx trace``.

    ``record_usage`` buffers until the next flush, so a trace read from the
    file alone would be missing the cost of the turn that just finished.
    """
    app = build_app(tmp_path)
    app.loop.session.append(user_message("what did that cost?"))
    app.loop.session.record_usage(TurnUsage(input_tokens=7, output_tokens=3, cost_usd=0.25))

    async with Driver(app) as driver:
        await app._run_command("/trace")
        await driver.settle()

    traced = _payload(default_trace_path(app.loop.session))
    assert traced["live"] is True
    assert traced["usage"]["turns"][0]["cost_usd"] == 0.25


async def test_trace_carries_the_prompt_before_the_first_provider_call(
    hx_home: Path, tmp_path: Path
) -> None:
    """Nothing is recorded to the transcript until the first call, so the
    command answers from the loop it is holding instead."""
    app = build_app(tmp_path)
    assert app.loop.session.environment is None

    async with Driver(app) as driver:
        await app._run_command("/trace")
        await driver.settle()

    traced = _payload(default_trace_path(app.loop.session))
    assert traced["system_prompt"] == "you are hx"
    assert traced["project_context"] == "# AGENTS.md"


async def test_trace_takes_a_path(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)
    async with Driver(app) as driver:
        await app._run_command(f"/trace {tmp_path / 'out' / 'mine.html'}")
        await driver.settle()

    assert (tmp_path / "out" / "mine.html").exists()


async def test_a_relative_path_lands_in_the_project(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)
    async with Driver(app) as driver:
        await app._run_command("/trace here.html")
        await driver.settle()

    assert (Path(app.settings.cwd) / "here.html").exists()


async def test_a_directory_target_gets_the_default_name(hx_home: Path, tmp_path: Path) -> None:
    app = build_app(tmp_path)
    (tmp_path / "into").mkdir()

    async with Driver(app) as driver:
        await app._run_command(f"/trace {tmp_path / 'into'}")
        await driver.settle()

    assert (tmp_path / "into" / TRACE_FILENAME).exists()


async def test_an_unwritable_target_is_reported_not_raised(hx_home: Path, tmp_path: Path) -> None:
    """A trace that cannot be written must not take the session with it."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")

    app = build_app(tmp_path)
    async with Driver(app) as driver:
        await app._run_command(f"/trace {blocked / 'trace.html'}")
        await driver.settle()

    assert "Could not write the trace" in _notices(app)


async def test_trace_follows_a_resume_rather_than_the_session_hx_opened_with(
    hx_home: Path, tmp_path: Path
) -> None:
    """``/resume`` rebinds ``loop.session``, and ``/trace`` has to follow it.

    Reported from a real run: resume a session with 74 messages, type
    ``/trace``, get a page of zeros written into the empty startup session's
    directory. The command context had captured the session object at startup,
    so it was tracing the session HX opened with rather than the one on screen.
    """
    app = build_app(tmp_path)
    opened = app.loop.session

    earlier = new_session(tmp_path, "anthropic/claude-sonnet-4.5")
    earlier.set_title("the one with the work in it")
    earlier.append(user_message("the prompt that started it"))
    earlier.record_usage(TurnUsage(input_tokens=11, output_tokens=5, cost_usd=0.5))
    earlier.flush()  # resume reads the file, so the usage has to be in it

    async with Driver(app) as driver:
        app.resume_session(earlier.meta.session_id)
        await app._run_command("/trace")
        await driver.settle()

    assert not default_trace_path(opened).exists(), "traced the session HX opened with"

    written = default_trace_path(earlier)
    assert written.exists(), "the trace did not land beside the resumed transcript"
    traced = _payload(written)
    assert traced["session"]["session_id"] == earlier.meta.session_id
    assert [m["content"][0]["text"] for m in traced["messages"]] == ["the prompt that started it"]
    assert traced["usage"]["turns"][0]["cost_usd"] == 0.5


async def test_cost_reads_the_resumed_session_too(hx_home: Path, tmp_path: Path) -> None:
    """The same stale reference: ``/cost`` shared it with ``/trace``."""
    app = build_app(tmp_path)

    earlier = new_session(tmp_path, "anthropic/claude-sonnet-4.5")
    earlier.append(user_message("spend something"))
    earlier.record_usage(TurnUsage(input_tokens=1234, output_tokens=7, cost_usd=0.25))
    earlier.flush()

    async with Driver(app) as driver:
        app.resume_session(earlier.meta.session_id)
        await app._run_command("/cost")
        await driver.settle()

    assert "1 API request" in _notices(app), "reported the empty startup session"


async def test_no_open_writes_the_file_and_stops_there(hx_home: Path, tmp_path: Path) -> None:
    """``/trace --no-open`` is for anyone who wants the file and not the window."""
    import hx.trace

    app = build_app(tmp_path)
    attempts: list[Path] = []

    async with Driver(app) as driver:
        app.loop.session.append(user_message("something to trace"))
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(hx.trace, "open_in_browser", lambda path: attempts.append(path) or True)
            await app._run_command("/trace --no-open")
            await driver.settle()

    assert attempts == [], "opened a browser after --no-open"
    assert default_trace_path(app.loop.session).exists()


async def test_the_flag_is_not_mistaken_for_a_destination(hx_home: Path, tmp_path: Path) -> None:
    """``--no-open`` is a flag, not a filename to write ``--no-open`` into."""
    app = build_app(tmp_path)

    async with Driver(app) as driver:
        app.loop.session.append(user_message("something to trace"))
        await app._run_command("/trace --no-open")
        await driver.settle()

    assert not (tmp_path / "--no-open").exists()
    assert default_trace_path(app.loop.session).exists()
