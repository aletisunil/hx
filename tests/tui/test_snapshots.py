"""Layout snapshots.

This restyle touched every widget, and nothing else in the suite would notice a
frame that lost a row or a hints line that started wrapping. These snapshots are
the safety net for shape, not for behaviour - regenerate with
``pytest --snapshot-update`` and read the diff before accepting it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot

from hx.tui.widgets import working
from hx.tui.widgets.working import WorkingIndicator
from tests.tui.test_app import build_app

SIZE = (100, 32)


@pytest.fixture(autouse=True)
def _force_snapshot_colors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep SVG colours independent of the shell running pytest.

    Textual honours ``NO_COLOR`` when the app is constructed and filters its
    rendered output to monochrome. That is useful for the real TUI, but it made
    snapshots recorded from a non-colour terminal disagree with CI's true-colour
    rendering even though the widgets and theme were identical.
    """
    monkeypatch.delenv("NO_COLOR", raising=False)


def _pin(app: Any) -> None:
    """Fix everything that would otherwise differ run to run.

    The status bar shows the working directory, which is a fresh tmp_path on
    every run. The startup header shows the package version, which changes on
    every release but has its own behavioural tests. Neither belongs in a
    layout snapshot.
    """
    app._status.set_location("~/project", "main")
    app._header.version = "0.0.0"
    app._header.refresh()


def _pin_clock(monkeypatch: Any) -> None:
    """Freeze the working indicator's animation and its stopwatch.

    The spinner advances on a timer and the elapsed count comes off the wall
    clock, so a snapshot of a running turn records whichever frame and second
    the machine happened to reach. That passes on the machine that recorded it
    and fails on any slower one. A single-frame FRAMES makes the index moot.
    """
    monkeypatch.setattr(working, "FRAMES", "⠋")
    monkeypatch.setattr(WorkingIndicator, "elapsed", property(lambda self: 12.0))


def test_the_idle_screen(hx_home: Path, tmp_path: Path, snap_compare: Any) -> None:
    app = build_app(tmp_path)

    async def run_before(pilot: Pilot) -> None:
        _pin(app)
        await pilot.pause()

    assert snap_compare(app, terminal_size=SIZE, run_before=run_before)


def test_a_running_turn(hx_home: Path, tmp_path: Path, snap_compare: Any, monkeypatch: Any) -> None:
    """The spinner rides the prompt's top rule; the layout must not shift."""
    _pin_clock(monkeypatch)
    app = build_app(tmp_path)

    async def run_before(pilot: Pilot) -> None:
        _pin(app)
        app._transcript.add_user_message("why is the retry loop giving up early?")
        app._working.start("thinking")
        await pilot.pause()

    assert snap_compare(app, terminal_size=SIZE, run_before=run_before)


def test_a_turn_with_tool_blocks(hx_home: Path, tmp_path: Path, snap_compare: Any) -> None:
    app = build_app(tmp_path)

    async def run_before(pilot: Pilot) -> None:
        _pin(app)
        transcript = app._transcript
        transcript.add_user_message("run the core tests")
        transcript.start_assistant_message()
        transcript.append_delta("Running them now.")
        # Absolute, and under the app's cwd, so display_path renders it
        # relative to the project. A bare relative path resolves against the
        # *process* cwd instead, which is not tmp_path, so it came out as an
        # absolute ~/... path and baked the recording machine into the snapshot.
        read_path = tmp_path / "src" / "hx" / "core" / "loop.py"
        transcript.add_tool_block("t1", "Read", {"file_path": str(read_path)})
        transcript.finish_tool_block("t1", "20 lines", False, {"content": "x = 1"}, 12.0)
        transcript.add_tool_block("t2", "Bash", {"command": "pytest -q tests/core"})
        transcript.update_tool_block("t2", "\n".join(f"line {i}" for i in range(30)))
        transcript.finish_tool_block("t2", "1 failed", True, {}, 4200.0)
        await pilot.pause()

    assert snap_compare(app, terminal_size=SIZE, run_before=run_before)


def test_the_completion_popup(hx_home: Path, tmp_path: Path, snap_compare: Any) -> None:
    app = build_app(tmp_path)

    async def run_before(pilot: Pilot) -> None:
        _pin(app)
        await pilot.press("slash", "c", "o")
        await pilot.pause()

    assert snap_compare(app, terminal_size=SIZE, run_before=run_before)


def test_a_pending_permission_prompt(
    hx_home: Path, tmp_path: Path, snap_compare: Any, monkeypatch: Any
) -> None:
    """The approval sits in the transcript, under the sentence that explains it.

    That adjacency is the whole argument for asking here rather than in a modal,
    and it is exactly the kind of thing only a layout snapshot notices losing.
    """
    from hx.permissions.engine import PermissionRequest
    from hx.tui.widgets.permission import PermissionPrompt

    _pin_clock(monkeypatch)
    app = build_app(tmp_path)

    async def run_before(pilot: Pilot) -> None:
        _pin(app)
        transcript = app._transcript
        transcript.add_user_message("clean up the settings loader")
        transcript.start_assistant_message()
        transcript.append_delta("Rewriting it to go through the settings reader.")
        request = PermissionRequest(
            tool_name="Edit",
            specifier="src/hx/config.py",
            params={},
            mutating=True,
            description="Edit(src/hx/config.py)",
            detail=(
                "--- src/hx/config.py\n"
                "+++ src/hx/config.py\n"
                "@@ -12,3 +12,3 @@\n"
                " def load():\n"
                "-    return {}\n"
                "+    return read_settings_file(path)\n"
            ),
        )
        transcript.add_permission_prompt(PermissionPrompt(request))
        app._working.start("awaiting approval")
        await pilot.pause()

    assert snap_compare(app, terminal_size=SIZE, run_before=run_before)
