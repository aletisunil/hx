"""Read tool."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from hx.core.images import ImageError, describe, is_image_path, load_image_file, sniff_image
from hx.text import count
from hx.tools import anchors
from hx.tools.base import Tool, ToolContext, ToolError, ToolResult

DESCRIPTION = """Read a file from the local filesystem.

Returns numbered lines, each labelled with a short content anchor:
`   12 a3f9\tcontent`. Pass those anchors to Edit's `hashline` argument to
replace a span without retyping it. Use `offset`/`limit` for large files.
Prefer reading the part you need over reading a whole large file.

An image file (PNG, JPEG, GIF, WebP, BMP, TIFF) is returned as the image
itself, for you to look at - screenshots, diagrams, mockups."""

DEFAULT_LINE_LIMIT = 2000
MAX_LINE_LENGTH = 2000
"""Individual lines longer than this are truncated - a minified bundle on one
line would otherwise blow the window in a single read."""

BINARY_SNIFF_BYTES = 8192


class ReadTool(Tool):
    name = "Read"
    description = DESCRIPTION
    mutating = False

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute or cwd-relative path"},
                "offset": {"type": "integer", "description": "First line to read (1-based)"},
                "limit": {"type": "integer", "description": "How many lines to read"},
            },
            "required": ["file_path"],
        }

    def permission_specifier(self, params: dict[str, Any]) -> str | None:
        return str(params.get("file_path", ""))

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # Filesystem work runs off the event loop: a slow disk or a huge
        # tree would otherwise freeze the TUI mid-render.
        return await asyncio.to_thread(self._run, params, ctx)

    def _run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = resolve_path(params["file_path"], ctx.cwd)
        if not path.exists():
            raise ToolError(f"{path}: no such file")
        if path.is_dir():
            raise ToolError(f"{path} is a directory. Use Glob to list its contents.")
        # Sniffed only when the file is binary anyway, so a text file that
        # happens to open with an image signature is still read as text.
        if is_image_path(path) or (is_binary(path) and _looks_like_image(path)):
            return _read_image(path)
        if is_binary(path):
            size = path.stat().st_size
            raise ToolError(f"{path}: binary file ({count(size, 'byte')}), not read as text")

        offset = max(int(params.get("offset") or 1), 1)
        limit = int(params.get("limit") or DEFAULT_LINE_LIMIT)

        try:
            content = read_exact(path, errors="replace")
        except OSError as exc:
            raise ToolError(f"{path}: {exc}") from exc

        # Split on newlines alone, so the lines an anchor indexes are the ones
        # Edit will reassemble. A line keeps its `\r` for hashing and loses it
        # only on the way to the screen.
        lines, _ = anchors.split(content)
        window = lines[offset - 1 : offset - 1 + limit]

        if hashline_enabled(ctx):
            # Anchors are computed over the whole file, not the window: the hash
            # covers each line's neighbours, and Edit resolves against the whole
            # file too. A window-local anchor would not survive the trip.
            labels, _ = anchors.compute(lines)
            rendered = "\n".join(
                f"{number:>6} {labels[number - 1]}\t{_clip(_shown(line))}"
                for number, line in enumerate(window, start=offset)
            )
        else:
            rendered = "\n".join(
                f"{number:>6}\t{_clip(_shown(line))}"
                for number, line in enumerate(window, start=offset)
            )

        ctx.reads.mark_read(path)

        remaining = len(lines) - (offset - 1 + len(window))
        if remaining > 0:
            rendered += (
                f"\n\n... {count(remaining, 'more line')}. "
                f"Re-read with offset={offset + len(window)}."
            )

        if not window:
            rendered = "(empty file)" if not lines else f"(no lines at offset {offset})"

        return ToolResult(content=rendered, summary=f"read {count(len(window), 'line')}")


def _read_image(path: Path) -> ToolResult:
    """An image, attached to the result for the model to look at.

    The text names it too. That line is all a text-only model gets, once the
    loop has withheld the image (see :func:`hx.core.images.without_images`),
    and it is what the transcript shows in place of the picture.
    """
    try:
        image = load_image_file(path)
    except ImageError as exc:
        raise ToolError(f"{path}: {exc}") from exc
    return ToolResult(
        content=f"Image {path} ({describe(image)}), attached.",
        summary=f"read image {image.width}x{image.height}",
        images=[image],
    )


def _looks_like_image(path: Path) -> bool:
    """An image saved without an extension - ``screenshot``, a temp file."""
    try:
        with path.open("rb") as handle:
            return sniff_image(handle.read(16))
    except OSError:
        return False


def _shown(line: str) -> str:
    """A CRLF file's `\r` is part of the line but not part of what to print."""
    return line.removesuffix("\r")


def hashline_enabled(ctx: ToolContext) -> bool:
    """Whether anchors are rendered and accepted.

    Read defensively: ``ToolContext.settings`` is deliberately untyped, and a
    headless caller may pass a stub that predates the setting.
    """
    tools = getattr(ctx.settings, "tools", None)
    return bool(getattr(tools, "hashline", True))


def read_exact(path: Path, errors: str = "strict") -> str:
    """A file's text with its line endings exactly as they are on disk.

    ``Path.read_text`` reads with universal newlines, which turns every ``\r\n``
    into ``\n``. Edit read that way and wrote the result back, so one edit to a
    CRLF file rewrote every line ending in it - and the diff, computed from the
    same normalised text, showed only the line that was asked for. Read and
    Edit both read here, so an anchor hashes the same bytes on both sides.
    """
    return path.read_bytes().decode("utf-8", errors=errors)


def resolve_path(raw: str, cwd: Path) -> Path:
    """Resolve a model-supplied path against the session cwd.

    Relative paths are common in model output even when the tool asks for
    absolute ones, so they are accepted rather than rejected.
    """
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (cwd / path)


def is_binary(path: Path) -> bool:
    try:
        chunk = path.open("rb").read(BINARY_SNIFF_BYTES)
    except OSError:
        return False
    return b"\x00" in chunk


def _clip(line: str) -> str:
    return line if len(line) <= MAX_LINE_LENGTH else line[:MAX_LINE_LENGTH] + " … [line truncated]"
