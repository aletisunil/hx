"""Layout snapshots.

This restyle touched every widget, and nothing else in the suite would notice a
frame that lost a row or a hints line that started wrapping. These snapshots are
the safety net for shape, not for behaviour - regenerate with
``pytest --snapshot-update`` and read the diff before accepting it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from textual.pilot import Pilot

from tests.tui.test_app import build_app

SIZE = (100, 32)


def _pin(app: Any) -> None:
    """Fix everything that would otherwise differ run to run.

    The status bar shows the working directory, which is a fresh tmp_path on
    every run; without this the snapshot would never match itself twice.
    """
    app._status.set_location("~/project", "main")


def test_the_idle_screen(hx_home: Path, tmp_path: Path, snap_compare: Any) -> None:
    app = build_app(tmp_path)

    async def run_before(pilot: Pilot) -> None:
        _pin(app)
        await pilot.pause()

    assert snap_compare(app, terminal_size=SIZE, run_before=run_before)


def test_a_running_turn(hx_home: Path, tmp_path: Path, snap_compare: Any) -> None:
    """The spinner rides the prompt's top rule; the layout must not shift."""
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
        transcript.add_tool_block("t1", "Read", {"file_path": "src/hx/core/loop.py"})
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
