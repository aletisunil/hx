"""Write tool: create or fully replace a file."""

from __future__ import annotations

import asyncio
import os
import secrets
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hx.text import count
from hx.tools import anchors
from hx.tools.base import Tool, ToolContext, ToolError, ToolResult
from hx.tools.read import FileTracker, resolve_path

if TYPE_CHECKING:
    from hx.core.checkpoints import CheckpointStore

DESCRIPTION = """Write a file, overwriting it if it exists.

Overwriting a file that has not been read in this session is refused - use Read
first, or Edit for partial changes."""


class WriteTool(Tool):
    name = "Write"
    description = DESCRIPTION
    mutating = True

    def __init__(self, tracker: FileTracker, checkpoints: CheckpointStore | None = None) -> None:
        """Args:
        tracker: ``hx.tools.read.FileTracker``, used to enforce read-before-write.
        checkpoints: ``hx.core.checkpoints.CheckpointStore``, given the file's
            previous bytes so ``/rewind`` can put them back. Optional: a
            headless caller with no session has nothing to rewind to.
        """
        self.tracker = tracker
        self.checkpoints = checkpoints

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["file_path", "content"],
        }

    def permission_specifier(self, params: dict[str, Any]) -> str | None:
        return str(params.get("file_path", ""))

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Writes atomically (temp file + rename) so a crash cannot truncate the target."""
        # Filesystem work runs off the event loop: a slow disk or a huge
        # tree would otherwise freeze the TUI mid-render.
        return await asyncio.to_thread(self._run, params, ctx)

    def _run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = resolve_path(params["file_path"], ctx.cwd)
        content = str(params["content"])

        if path.exists():
            if path.is_dir():
                raise ToolError(f"{path} is a directory")
            if not self.tracker.was_read(path):
                raise ToolError(
                    f"{path} exists but has not been read in this session. "
                    "Read it first so the overwrite is deliberate."
                )
            if self.tracker.changed_since_read(path):
                raise ToolError(
                    f"{path} changed on disk since it was read. Read it again before writing."
                )

        existed = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.checkpoints is not None:
            self.checkpoints.capture(path)
        write_atomic(path, content)
        self.tracker.mark_read(path)
        if self.checkpoints is not None:
            self.checkpoints.settle(path)

        verb = "updated" if existed else "created"
        # Lines as Read numbers them: a final newline ends the last line rather
        # than starting another, so "done\n" is one line, not two.
        lines = len(anchors.split(content)[0])
        return ToolResult(
            content=f"{verb} {path}{through_link(path)} ({count(lines, 'line')})",
            summary=f"{verb} {path.name}",
            metadata={"path": str(path), "created": not existed},
        )


def link_target(path: Path) -> Path | None:
    """The file a write to ``path`` really changes, when ``path`` is a symlink.

    None for anything else. Callers name it to the user and the model, because
    the link can point anywhere - out of the project included - and a write
    reported against the link's own name hides where it landed.
    """
    return path.resolve() if path.is_symlink() else None


def through_link(path: Path) -> str:
    """`` through the link to <target>`` for a symlink, else nothing."""
    target = link_target(path)
    return f" through the link to {target}" if target is not None else ""


def write_atomic(path: Path, content: str) -> None:
    """Write via a temp file in the same directory, then rename.

    Same directory matters: a rename across filesystems is not atomic, and the
    point of this is that a crash never leaves a half-written file.

    The rename replaces whatever sits at the name it lands on, so two things
    are carried over by hand. A symlink is written through - the rename lands
    on the file it points at, and the link stays a link - rather than replaced
    by a regular file while its target goes unchanged. A link to a file that
    does not exist yet creates it, the way a shell redirect would, provided
    its directory exists. And the file's mode is copied onto the temp file,
    which would otherwise turn an executable script into one that no longer
    runs. A new file gets the mode the umask gives it.

    The temp file is created with that mode, not opened wider and narrowed
    after the write: in between, a private file's new content - an ``.env``
    being edited - was readable by every user on the machine, and a crash in
    that window left the readable copy on disk.

    Text is written as given, with no newline translation: the line endings
    in ``content`` are the ones the caller chose.
    """
    target = link_target(path) or path
    if target is not path and not target.parent.is_dir():
        raise ToolError(f"{path} is a link to {target}, and {target.parent} does not exist")
    try:
        mode: int | None = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        mode = None

    tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.hx-tmp")
    # Through os.open, so the kernel applies the umask on top: the temp file
    # never allows more than the file it replaces, and a new file gets 0o666
    # less the umask.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666 if mode is None else mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        if mode is not None:
            # Again, exactly: the umask may have taken bits the file had.
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
