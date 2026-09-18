"""The shipped example hook, ``examples/hooks/check.sh``.

An example that does not work is worse than no example - somebody copies it,
sees nothing happen on every edit, and concludes hooks are broken. These run
the real script through the real engine.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from hx.hooks.engine import HookEngine
from hx.hooks.spec import HookCommand, HookEvent

SCRIPT = Path(__file__).resolve().parents[2] / "examples" / "hooks" / "check.sh"

BROKEN = "def handle(payload):\n    return paylod\n"
CLEAN = "def handle(payload):\n    return payload\n"

pytestmark = pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not on PATH")


def _engine(cwd: Path) -> HookEngine:
    return HookEngine(
        hooks={
            HookEvent.POST_TOOL_USE: [HookCommand(str(SCRIPT), matcher="Edit|Write", timeout=60)]
        },
        cwd=cwd,
        session_id="test",
    )


def test_the_example_is_executable() -> None:
    """A copied-but-not-chmod'd hook fails as an exec error on every edit."""
    assert SCRIPT.is_file()
    assert SCRIPT.stat().st_mode & 0o111, f"{SCRIPT} is not executable"


async def test_reports_a_real_error_back_to_the_model(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text(BROKEN)
    outcome = await _engine(tmp_path).post_tool_use(
        "Edit", {"file_path": "broken.py"}, "Applied 1 edit(s)", False
    )
    assert not outcome.blocked
    assert not outcome.errors
    assert "F821" in outcome.context_text
    assert "paylod" in outcome.context_text


async def test_a_clean_file_adds_nothing(tmp_path: Path) -> None:
    """Silence on success. A hook that speaks every turn gets turned off."""
    (tmp_path / "clean.py").write_text(CLEAN)
    outcome = await _engine(tmp_path).post_tool_use(
        "Edit", {"file_path": "clean.py"}, "Applied 1 edit(s)", False
    )
    assert outcome.context_text == ""


async def test_never_blocks(tmp_path: Path) -> None:
    """The write already landed by PostToolUse; refusing it would be theatre."""
    (tmp_path / "broken.py").write_text(BROKEN)
    outcome = await _engine(tmp_path).post_tool_use(
        "Edit", {"file_path": "broken.py"}, "Applied 1 edit(s)", False
    )
    assert not outcome.blocked


async def test_unhandled_extension_is_silent(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("# nothing to check\n")
    outcome = await _engine(tmp_path).post_tool_use(
        "Write", {"file_path": "notes.md"}, "written", False
    )
    assert outcome.context_text == ""
    assert not outcome.errors


async def test_missing_file_is_silent(tmp_path: Path) -> None:
    outcome = await _engine(tmp_path).post_tool_use("Edit", {"file_path": "gone.py"}, "ok", False)
    assert outcome.context_text == ""
    assert not outcome.errors


async def test_malformed_event_is_silent(tmp_path: Path) -> None:
    """Defensive: the script must not fail loudly on input it cannot parse."""
    outcome = await _engine(tmp_path).post_tool_use("Edit", {}, "ok", False)
    assert outcome.context_text == ""
    assert not outcome.errors


async def test_the_matcher_excludes_other_tools(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text(BROKEN)
    outcome = await _engine(tmp_path).post_tool_use(
        "Read", {"file_path": "broken.py"}, "contents", False
    )
    assert outcome.context_text == ""
