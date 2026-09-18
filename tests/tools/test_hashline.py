"""Content anchors and the ``hashline`` edit shape."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hx.config import load_settings
from hx.tools import anchors
from hx.tools.base import ToolContext, ToolError
from hx.tools.edit import EditTool
from hx.tools.read import FileTracker, ReadTool

SAMPLE = "def one():\n    return 1\n\n\ndef two():\n    return 2\n"


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


def test_neighbouring_lines_of_a_two_line_file_differ() -> None:
    """Both windows cover the whole file; only the separator tells them apart."""
    labels, _ = anchors.compute(["alpha", "beta"])
    assert labels[0] != labels[1]


def test_identical_lines_in_different_context_differ() -> None:
    labels, _ = anchors.compute(["a", "x", "b", "c", "x", "d"])
    assert labels[1] != labels[4]


def test_width_widens_until_collision_free() -> None:
    """A file engineered to collide at four hex chars renders wider."""
    lines = [f"line {i}" for i in range(4000)]
    labels, width = anchors.compute(lines)
    assert width in anchors.WIDTHS
    assert len(set(labels)) == len(set(anchors.digests(lines)))


def test_resolve_finds_the_line() -> None:
    lines = SAMPLE.splitlines()
    labels, _ = anchors.compute(lines)
    assert anchors.resolve(lines, labels[4]) == 4


def test_resolve_accepts_a_hash_prefixed_anchor() -> None:
    lines = SAMPLE.splitlines()
    labels, _ = anchors.compute(lines)
    assert anchors.resolve(lines, "#" + labels[1]) == 1


def test_resolve_rejects_an_unknown_anchor() -> None:
    with pytest.raises(anchors.AnchorError, match="does not match"):
        anchors.resolve(SAMPLE.splitlines(), "dead")


def test_resolve_rejects_an_ambiguous_anchor() -> None:
    """Inside a run of blank lines the windows really are identical."""
    lines = ["a", "", "", "", "", "b"]
    labels, _ = anchors.compute(lines)
    with pytest.raises(anchors.AnchorError, match="ambiguous"):
        anchors.resolve(lines, labels[2])


async def test_hashline_replaces_a_span(ctx: ToolContext, tracker: FileTracker) -> None:
    path = ctx.cwd / "mod.py"
    path.write_text(SAMPLE)
    await ReadTool(tracker).run({"file_path": "mod.py"}, ctx)

    labels, _ = anchors.compute(SAMPLE.splitlines())
    await EditTool(tracker).run(
        {
            "file_path": "mod.py",
            "hashline": [
                {"start": labels[4], "end": labels[5], "new_string": "def two():\n    return 22"}
            ],
        },
        ctx,
    )
    assert path.read_text() == "def one():\n    return 1\n\n\ndef two():\n    return 22\n"


async def test_hashline_replaces_one_line_without_end(
    ctx: ToolContext, tracker: FileTracker
) -> None:
    path = ctx.cwd / "mod.py"
    path.write_text(SAMPLE)
    await ReadTool(tracker).run({"file_path": "mod.py"}, ctx)

    labels, _ = anchors.compute(SAMPLE.splitlines())
    await EditTool(tracker).run(
        {"file_path": "mod.py", "hashline": [{"start": labels[1], "new_string": "    return 11"}]},
        ctx,
    )
    assert "    return 11\n" in path.read_text()
    assert "    return 1\n" not in path.read_text()


async def test_hashline_deletes_on_empty_replacement(
    ctx: ToolContext, tracker: FileTracker
) -> None:
    path = ctx.cwd / "mod.py"
    path.write_text(SAMPLE)
    await ReadTool(tracker).run({"file_path": "mod.py"}, ctx)

    labels, _ = anchors.compute(SAMPLE.splitlines())
    await EditTool(tracker).run(
        {"file_path": "mod.py", "hashline": [{"start": labels[1], "new_string": ""}]},
        ctx,
    )
    assert path.read_text() == "def one():\n\n\ndef two():\n    return 2\n"


async def test_hashline_rejects_a_stale_anchor(ctx: ToolContext, tracker: FileTracker) -> None:
    """The anchor is taken from one version of the file and applied to another."""
    path = ctx.cwd / "mod.py"
    path.write_text(SAMPLE)
    await ReadTool(tracker).run({"file_path": "mod.py"}, ctx)
    stale = anchors.compute(["totally", "different", "content"])[0][1]

    with pytest.raises(ToolError, match="does not match"):
        await EditTool(tracker).run(
            {"file_path": "mod.py", "hashline": [{"start": stale, "new_string": "x"}]},
            ctx,
        )
    assert path.read_text() == SAMPLE


async def test_hashline_rejects_a_reversed_span(ctx: ToolContext, tracker: FileTracker) -> None:
    path = ctx.cwd / "mod.py"
    path.write_text(SAMPLE)
    await ReadTool(tracker).run({"file_path": "mod.py"}, ctx)

    labels, _ = anchors.compute(SAMPLE.splitlines())
    with pytest.raises(ToolError, match="before start anchor"):
        await EditTool(tracker).run(
            {
                "file_path": "mod.py",
                "hashline": [{"start": labels[5], "end": labels[0], "new_string": "x"}],
            },
            ctx,
        )
    assert path.read_text() == SAMPLE


async def test_hashline_anchors_round_trip_from_read(
    ctx: ToolContext, tracker: FileTracker
) -> None:
    """The anchor the model sees in the read output is the one Edit resolves."""
    path = ctx.cwd / "mod.py"
    path.write_text(SAMPLE)
    rendered = await ReadTool(tracker).run({"file_path": "mod.py"}, ctx)

    match = re.search(r"^     5 ([0-9a-f]+)\t", rendered.content, re.M)
    assert match is not None
    await EditTool(tracker).run(
        {
            "file_path": "mod.py",
            "hashline": [{"start": match.group(1), "new_string": "def TWO():"}],
        },
        ctx,
    )
    assert "def TWO():" in path.read_text()


async def test_hashline_refused_when_disabled(tmp_path: Path, tracker: FileTracker) -> None:
    settings = load_settings(tmp_path, overrides={"tools": {"hashline": False}})
    ctx = ToolContext(
        cwd=tmp_path,
        session_id="test-session",
        tool_use_id="t1",
        settings=settings,
        emit_progress=lambda _chunk: None,
    )
    path = tmp_path / "mod.py"
    path.write_text(SAMPLE)
    await ReadTool(tracker).run({"file_path": "mod.py"}, ctx)

    with pytest.raises(ToolError, match="disabled"):
        await EditTool(tracker).run(
            {"file_path": "mod.py", "hashline": [{"start": "abcd", "new_string": "x"}]},
            ctx,
        )
