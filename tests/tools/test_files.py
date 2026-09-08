"""Read, Write, Edit, Glob and Grep."""

from __future__ import annotations

from pathlib import Path

import pytest

from hx.config import load_settings
from hx.tools.base import ToolContext, ToolError
from hx.tools.edit import EditOp, EditTool
from hx.tools.glob import GlobTool
from hx.tools.grep import GrepTool
from hx.tools.read import FileTracker, ReadTool
from hx.tools.write import WriteTool


@pytest.fixture()
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        cwd=tmp_path,
        session_id="test-session",
        tool_use_id="t1",
        settings=load_settings(tmp_path),
        emit_progress=lambda _chunk: None,
    )


@pytest.fixture()
def tracker() -> FileTracker:
    return FileTracker()


async def test_read_numbers_lines(ctx: ToolContext, tracker: FileTracker) -> None:
    (ctx.cwd / "a.py").write_text("alpha\nbeta\n")
    result = await ReadTool(tracker).run({"file_path": "a.py"}, ctx)
    assert "     1\talpha" in result.content
    assert "     2\tbeta" in result.content


async def test_read_windows_with_offset_and_limit(ctx: ToolContext, tracker: FileTracker) -> None:
    (ctx.cwd / "big.txt").write_text("\n".join(str(i) for i in range(100)))
    result = await ReadTool(tracker).run({"file_path": "big.txt", "offset": 50, "limit": 5}, ctx)
    assert "    50\t49" in result.content
    assert "more lines" in result.content


async def test_read_truncates_a_single_enormous_line(
    ctx: ToolContext, tracker: FileTracker
) -> None:
    """One minified bundle on one line would otherwise fill the window."""
    (ctx.cwd / "bundle.js").write_text("x" * 50_000)
    result = await ReadTool(tracker).run({"file_path": "bundle.js"}, ctx)
    assert "line truncated" in result.content
    assert len(result.content) < 10_000


async def test_read_refuses_binaries(ctx: ToolContext, tracker: FileTracker) -> None:
    (ctx.cwd / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    with pytest.raises(ToolError, match="binary"):
        await ReadTool(tracker).run({"file_path": "blob.bin"}, ctx)


async def test_read_points_at_glob_for_directories(ctx: ToolContext, tracker: FileTracker) -> None:
    (ctx.cwd / "sub").mkdir()
    with pytest.raises(ToolError, match="Glob"):
        await ReadTool(tracker).run({"file_path": "sub"}, ctx)


async def test_write_creates_new_files(ctx: ToolContext, tracker: FileTracker) -> None:
    await WriteTool(tracker).run({"file_path": "new/deep.txt", "content": "hi"}, ctx)
    assert (ctx.cwd / "new" / "deep.txt").read_text() == "hi"


async def test_write_refuses_to_clobber_an_unread_file(
    ctx: ToolContext, tracker: FileTracker
) -> None:
    """Overwriting a file the model never looked at destroys work silently."""
    (ctx.cwd / "existing.txt").write_text("precious")
    with pytest.raises(ToolError, match="has not been read"):
        await WriteTool(tracker).run({"file_path": "existing.txt", "content": "gone"}, ctx)
    assert (ctx.cwd / "existing.txt").read_text() == "precious"


async def test_write_detects_a_file_changed_underneath(
    ctx: ToolContext, tracker: FileTracker
) -> None:
    path = ctx.cwd / "shared.txt"
    path.write_text("v1")
    await ReadTool(tracker).run({"file_path": "shared.txt"}, ctx)
    path.write_text("v2 from another process")

    with pytest.raises(ToolError, match="changed on disk"):
        await WriteTool(tracker).run({"file_path": "shared.txt", "content": "v3"}, ctx)


async def test_write_leaves_no_partial_file_behind(ctx: ToolContext, tracker: FileTracker) -> None:
    await WriteTool(tracker).run({"file_path": "atomic.txt", "content": "body"}, ctx)
    assert [p.name for p in ctx.cwd.iterdir()] == ["atomic.txt"]


async def test_edit_replaces_a_unique_string(ctx: ToolContext, tracker: FileTracker) -> None:
    path = ctx.cwd / "code.py"
    path.write_text("def old():\n    pass\n")
    await ReadTool(tracker).run({"file_path": "code.py"}, ctx)

    result = await EditTool(tracker).run(
        {"file_path": "code.py", "old_string": "old", "new_string": "new"}, ctx
    )
    assert path.read_text() == "def new():\n    pass\n"
    assert "-def old():" in result.content
    assert "+def new():" in result.content


def test_edit_refuses_an_ambiguous_match(tracker: FileTracker) -> None:
    """Two matches means the model did not say what it meant. Guessing is worse
    than failing."""
    with pytest.raises(ToolError, match="appears 2 times"):
        EditTool(tracker).apply("x = 1\nx = 1\n", [EditOp("x = 1", "x = 2")])


def test_edit_replace_all_is_explicit(tracker: FileTracker) -> None:
    out = EditTool(tracker).apply("a\na\n", [EditOp("a", "b", replace_all=True)])
    assert out == "b\nb\n"


def test_edit_reports_a_missing_match(tracker: FileTracker) -> None:
    with pytest.raises(ToolError, match="not found"):
        EditTool(tracker).apply("hello", [EditOp("goodbye", "hi")])


async def test_multiple_edits_apply_all_or_nothing(ctx: ToolContext, tracker: FileTracker) -> None:
    """A half-applied edit leaves a state nobody asked for."""
    path = ctx.cwd / "multi.py"
    original = "one\ntwo\n"
    path.write_text(original)
    await ReadTool(tracker).run({"file_path": "multi.py"}, ctx)

    with pytest.raises(ToolError):
        await EditTool(tracker).run(
            {
                "file_path": "multi.py",
                "edits": [
                    {"old_string": "one", "new_string": "1"},
                    {"old_string": "MISSING", "new_string": "x"},
                ],
            },
            ctx,
        )
    assert path.read_text() == original


async def test_edit_requires_a_prior_read(ctx: ToolContext, tracker: FileTracker) -> None:
    (ctx.cwd / "unseen.py").write_text("body")
    with pytest.raises(ToolError, match="has not been read"):
        await EditTool(tracker).run(
            {"file_path": "unseen.py", "old_string": "body", "new_string": "x"}, ctx
        )


async def test_glob_sorts_newest_first_and_skips_noise(ctx: ToolContext) -> None:
    import os
    import time

    (ctx.cwd / "old.py").write_text("")
    (ctx.cwd / "new.py").write_text("")
    (ctx.cwd / "node_modules").mkdir()
    (ctx.cwd / "node_modules" / "dep.py").write_text("")

    past = time.time() - 10_000
    os.utime(ctx.cwd / "old.py", (past, past))

    result = await GlobTool().run({"pattern": "**/*.py"}, ctx)
    lines = result.content.splitlines()
    assert lines[0].endswith("new.py")
    assert not any("node_modules" in line for line in lines)


async def test_grep_finds_matches(ctx: ToolContext) -> None:
    (ctx.cwd / "a.py").write_text("import os\nvalue = 1\n")
    (ctx.cwd / "b.py").write_text("nothing here\n")

    result = await GrepTool().run({"pattern": r"^import\s", "output_mode": "content"}, ctx)
    assert "import os" in result.content
    assert "b.py" not in result.content


async def test_grep_rejects_a_bad_regex(ctx: ToolContext) -> None:
    with pytest.raises(ToolError, match="invalid regular expression"):
        await GrepTool().run({"pattern": "("}, ctx)


async def test_grep_python_fallback_matches_ripgrep_on_basics(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback exists so a machine without rg still works; it must agree."""
    (ctx.cwd / "a.py").write_text("needle here\n")
    tool = GrepTool()

    with_rg = await tool.run({"pattern": "needle"}, ctx)
    monkeypatch.setattr("hx.tools.grep.has_ripgrep", lambda: False)
    without_rg = await tool.run({"pattern": "needle"}, ctx)

    assert sorted(with_rg.content.splitlines()) == sorted(without_rg.content.splitlines())


async def test_tools_do_not_block_the_event_loop(ctx: ToolContext, tracker: FileTracker) -> None:
    """File work runs on a thread; a blocked loop freezes the TUI mid-render."""
    import asyncio

    (ctx.cwd / "a.py").write_text("x\n")
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.001)
            ticks += 1

    task = asyncio.create_task(ticker())
    for _ in range(20):
        await ReadTool(tracker).run({"file_path": "a.py"}, ctx)
    task.cancel()
    assert ticks > 0


def test_file_tracker_notices_content_changes(tmp_path: Path, tracker: FileTracker) -> None:
    path = tmp_path / "f.txt"
    path.write_text("v1")
    tracker.mark_read(path)
    assert not tracker.changed_since_read(path)

    path.write_text("v2")
    assert tracker.changed_since_read(path)
    assert tracker.stale_files() == [path.resolve()]
