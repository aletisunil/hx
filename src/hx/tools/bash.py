"""Bash tool: a persistent shell session.

State (cwd, exported vars, activated venvs) persists across calls within a
session, which is what makes multi-step work feel natural. Commands run through
the sandbox wrapper when one is available, and always through the permission
engine first.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hx.tools.base import StreamingTool, ToolContext, ToolResult

DESCRIPTION = """Run a shell command in a persistent session.

State persists between calls: cd, exported variables and activated environments
carry over. Prefer absolute paths. Use `timeout` for long commands, and
`run_in_background` for servers or watchers."""


@dataclass(slots=True)
class ShellResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    duration_ms: float


class PersistentShell:
    """A long-lived shell subprocess.

    Commands are delimited by a random sentinel echoed after each one, so
    output framing survives commands that print arbitrary text. Without the
    sentinel a command printing a fixed marker could spoof the boundary.
    """

    def __init__(self, cwd: Path, shell: str | None = None, env: dict[str, str] | None = None):
        raise NotImplementedError

    async def start(self) -> None:
        raise NotImplementedError

    async def run(self, command: str, timeout_seconds: float) -> ShellResult:
        raise NotImplementedError

    def stream(self, command: str, timeout_seconds: float) -> AsyncIterator[str]:
        raise NotImplementedError

    async def interrupt(self) -> None:
        """Send SIGINT to the foreground process group, leaving the shell alive."""
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    @property
    def cwd(self) -> Path:
        """Current shell cwd, re-read after each command."""
        raise NotImplementedError


class BackgroundJobs:
    """Tracks commands started with ``run_in_background``."""

    def __init__(self) -> None:
        raise NotImplementedError

    def start(self, command: str, cwd: Path) -> str:
        """Returns a job id the model can poll."""
        raise NotImplementedError

    def output(self, job_id: str, since_line: int = 0) -> str:
        raise NotImplementedError

    def kill(self, job_id: str) -> None:
        raise NotImplementedError

    def list(self) -> list[dict[str, Any]]:
        raise NotImplementedError


class BashTool(StreamingTool):
    name = "Bash"
    description = DESCRIPTION
    mutating = True

    def __init__(self, shell: PersistentShell, jobs: BackgroundJobs) -> None:
        raise NotImplementedError

    def schema(self) -> dict[str, Any]:
        raise NotImplementedError

    def permission_specifier(self, params: dict[str, Any]) -> str | None:
        """The raw command string. ``hx.permissions.parser`` splits it into
        segments before matching, so compound commands cannot smuggle a denied
        segment past an allowed prefix."""
        raise NotImplementedError

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise NotImplementedError

    def stream(self, params: dict[str, Any], ctx: ToolContext) -> AsyncIterator[str]:
        raise NotImplementedError
