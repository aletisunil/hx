"""Bash tool: a persistent shell session.

State (cwd, exported vars, activated venvs) persists across calls within a
session, which is what makes multi-step work feel natural. The shell is spawned
once inside the sandbox, so every command inherits it; changing the sandbox
policy requires :meth:`PersistentShell.restart`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import signal
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hx.tools.base import StreamingTool, Tool, ToolContext, ToolError, ToolResult
from hx.tools.output import cap_output, summarize_for_ui

DESCRIPTION = """Run a shell command in a persistent session.

State persists between calls: cd, exported variables and activated environments
carry over. Prefer absolute paths. Use `timeout` for long commands, and
`run_in_background` for servers or watchers."""

DEFAULT_TIMEOUT = 120.0
MAX_TIMEOUT = 600.0
READ_CHUNK = 4096


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
    per-session random suffix a command printing a fixed marker could spoof the
    boundary and hide its own output.
    """

    def __init__(
        self,
        cwd: Path,
        shell: str | None = None,
        env: dict[str, str] | None = None,
        sandbox: Any | None = None,
    ) -> None:
        self._cwd = cwd
        self.shell = shell or os.environ.get("SHELL") or "/bin/bash"
        self.env = {**os.environ, **(env or {}), "HX": "1", "TERM": "dumb"}
        self.sandbox = sandbox
        self._process: asyncio.subprocess.Process | None = None
        self._sentinel = f"__HX_DONE_{secrets.token_hex(8)}__"
        self._lock = asyncio.Lock()
        self._exit_code = 0
        self._timed_out = False
        self._duration_ms = 0.0

    @property
    def last_exit_code(self) -> int:
        """Exit status of the most recently completed command."""
        return self._exit_code

    async def start(self) -> None:
        argv = [self.shell, "-s"]
        if self.sandbox is not None:
            argv = self.sandbox.wrap(argv)
        self._process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self._cwd,
            env=self.env,
            start_new_session=True,
        )

    async def restart(self) -> None:
        await self.close()
        await self.start()

    async def run(self, command: str, timeout_seconds: float = DEFAULT_TIMEOUT) -> ShellResult:
        chunks: list[str] = []
        async for chunk in self.stream(command, timeout_seconds):
            chunks.append(chunk)
        output = "".join(chunks)
        return ShellResult(
            stdout=output,
            stderr="",
            exit_code=self._exit_code,
            timed_out=self._timed_out,
            duration_ms=self._duration_ms,
        )

    async def stream(
        self, command: str, timeout_seconds: float = DEFAULT_TIMEOUT
    ) -> AsyncIterator[str]:
        """Run a command, yielding output as it arrives."""
        async with self._lock:
            if self._process is None or self._process.returncode is not None:
                await self.start()
            assert self._process is not None and self._process.stdin is not None

            started = time.monotonic()
            self._timed_out = False
            self._exit_code = 0

            # stderr is merged into stdout so the model sees what a user would.
            payload = f"{command}\nprintf '\\n%s %s\\n' {self._sentinel} \"$?\"\n"
            self._process.stdin.write(payload.encode())
            await self._process.stdin.drain()

            try:
                async for chunk in self._read_until_sentinel(timeout_seconds):
                    yield chunk
            except TimeoutError:
                self._timed_out = True
                self._exit_code = 124
                interrupted = await self.interrupt()
                yield f"\n[hx] command exceeded {timeout_seconds:.0f}s and was interrupted\n"
                # The interrupted command still owes us a sentinel. Leaving it in
                # the pipe would make the *next* command return that stale marker
                # immediately and appear to produce no output at all.
                if not interrupted or not await self._drain_stale_sentinel():
                    await self.restart()
                    yield "[hx] shell did not recover from the interrupt; restarted\n"

            self._duration_ms = (time.monotonic() - started) * 1000

    async def _read_until_sentinel(self, timeout_seconds: float) -> AsyncIterator[str]:
        assert self._process is not None and self._process.stdout is not None
        stdout = self._process.stdout
        buffer = ""
        deadline = time.monotonic() + timeout_seconds

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            data = await asyncio.wait_for(stdout.read(READ_CHUNK), timeout=remaining)
            if not data:
                # The shell died - `exit 3`, a crash, a kill. Wait for it to be
                # reaped before reading the status: returncode is None until
                # then, and falling back to 1 would report the wrong code.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._process.wait(), timeout=2)
                self._exit_code = (
                    self._process.returncode if self._process.returncode is not None else 1
                )
                if buffer:
                    yield buffer
                return

            buffer += data.decode(errors="replace")
            if self._sentinel in buffer:
                head, _, tail = buffer.partition(self._sentinel)
                self._exit_code = _parse_exit_code(tail)
                if head:
                    yield head
                return

            # Hold back a partial sentinel so it is never emitted as output.
            safe = len(buffer) - len(self._sentinel)
            if safe > 0:
                yield buffer[:safe]
                buffer = buffer[safe:]

    async def _drain_stale_sentinel(self, timeout_seconds: float = 5.0) -> bool:
        """Consume output up to the sentinel owed by an interrupted command."""
        assert self._process is not None and self._process.stdout is not None
        stdout = self._process.stdout
        buffer = ""
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                data = await asyncio.wait_for(stdout.read(READ_CHUNK), timeout=remaining)
            except TimeoutError:
                return False
            if not data:
                return False
            buffer += data.decode(errors="replace")
            if self._sentinel in buffer:
                return True

    async def interrupt(self) -> bool:
        """Interrupt the running command while leaving the shell alive.

        Signalling the whole process group would kill the shell too - a
        non-interactive shell dies on SIGINT - and one timed-out command would
        silently wipe the session's cd and exported variables. So only the
        shell's own children are signalled.

        Returns False when no child could be found, meaning the caller should
        restart the shell rather than assume the command stopped.
        """
        if self._process is None or self._process.returncode is not None:
            return False

        children = await _child_pids(self._process.pid)
        if not children:
            return False

        for pid in children:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGINT)

        await asyncio.sleep(0.2)
        for pid in children:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        return True

    async def close(self) -> None:
        if self._process is None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            self._process.terminate()
        with contextlib.suppress(TimeoutError, ProcessLookupError):
            await asyncio.wait_for(self._process.wait(), timeout=5)
        self._process = None

    @property
    def cwd(self) -> Path:
        """Shell cwd as HX last knew it. Refreshed by :meth:`sync_cwd`."""
        return self._cwd


@dataclass(slots=True)
class BackgroundJob:
    job_id: str
    command: str
    process: Any
    log_path: Path
    started: float = field(default_factory=time.time)
    cursor: int = 0
    """Lines already returned to the model, so polling only yields what is new."""


class BackgroundJobs:
    """Tracks commands started with ``run_in_background``.

    Background work runs in its own process rather than the persistent shell:
    a long-running server would otherwise hold the shell's sentinel forever and
    wedge every later command.
    """

    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self._jobs: dict[str, BackgroundJob] = {}

    async def start(self, command: str, cwd: Path, sandbox: Any | None = None) -> str:
        """Returns a job id the model can poll."""
        job_id = f"bg_{uuid.uuid4().hex[:8]}"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"{job_id}.log"

        argv = ["/bin/sh", "-c", command]
        if sandbox is not None:
            argv = sandbox.wrap(argv)

        handle = log_path.open("wb")
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=handle,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            start_new_session=True,
        )
        self._jobs[job_id] = BackgroundJob(job_id, command, process, log_path)
        return job_id

    def output(self, job_id: str, since_line: int = 0) -> str:
        return self.read(job_id, since_line)[0]

    def read(self, job_id: str, since_line: int | None = None) -> tuple[str, int]:
        """Return output and the next line cursor.

        The cursor is remembered per job, so repeated polling returns only what
        is new rather than re-sending the whole log on every call.
        """
        job = self._get(job_id)
        try:
            lines = job.log_path.read_text(errors="replace").splitlines()
        except OSError:
            return "(no output yet)", job.cursor

        start = job.cursor if since_line is None else max(0, since_line)
        chunk = lines[start:]
        job.cursor = len(lines)
        return "\n".join(chunk) or "(no new output)", job.cursor

    def state(self, job_id: str) -> dict[str, Any]:
        job = self._get(job_id)
        return {
            "job_id": job.job_id,
            "running": job.process.returncode is None,
            "exit_code": job.process.returncode,
        }

    def kill(self, job_id: str) -> None:
        job = self._get(job_id)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(job.process.pid), signal.SIGTERM)

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "job_id": job.job_id,
                "command": job.command,
                "running": job.process.returncode is None,
                "exit_code": job.process.returncode,
                "elapsed_s": round(time.time() - job.started, 1),
            }
            for job in self._jobs.values()
        ]

    def _get(self, job_id: str) -> BackgroundJob:
        try:
            return self._jobs[job_id]
        except KeyError:
            raise ToolError(f"unknown background job {job_id!r}") from None

    async def close_all(self) -> None:
        for job_id in list(self._jobs):
            self.kill(job_id)


class BashTool(StreamingTool):
    name = "Bash"
    description = DESCRIPTION
    mutating = True

    def __init__(self, shell: PersistentShell, jobs: BackgroundJobs) -> None:
        self.shell = shell
        self.jobs = jobs

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {
                    "type": "number",
                    "description": "Seconds. Capped by the project's configured maximum.",
                },
                "run_in_background": {"type": "boolean", "default": False},
                "description": {
                    "type": "string",
                    "description": "5-10 word description of what this runs",
                },
            },
            "required": ["command"],
        }

    def permission_specifier(self, params: dict[str, Any]) -> str | None:
        """The raw command string. ``hx.permissions.parser`` splits it into
        segments before matching, so compound commands cannot smuggle a denied
        segment past an allowed prefix."""
        command = params.get("command")
        return str(command) if command else None

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = str(params["command"]).strip()
        if not command:
            raise ToolError("command is empty")

        timeout = self._timeout(params, ctx)

        if params.get("run_in_background"):
            job_id = await self.jobs.start(command, ctx.cwd, self.shell.sandbox)
            return ToolResult(
                content=(
                    f"Started background job {job_id}.\n"
                    f"Read its output with BashOutput(job_id={job_id!r}), "
                    f"stop it with KillShell(job_id={job_id!r})."
                ),
                summary=f"background {job_id}",
                metadata={"job_id": job_id},
            )

        chunks: list[str] = []
        async for chunk in self.shell.stream(command, timeout):
            chunks.append(chunk)
            ctx.emit_progress(chunk)

        raw = "".join(chunks)
        capped = cap_output(
            raw,
            session_id=ctx.session_id,
            tool_use_id=ctx.tool_use_id,
            char_cap=ctx.settings.context.tool_output_char_cap,
            line_cap=ctx.settings.context.tool_output_line_cap,
        )

        exit_code = self.shell.last_exit_code
        body = capped.text.rstrip() or "(no output)"
        if exit_code != 0:
            body = f"{body}\n\n[exit code {exit_code}]"

        return ToolResult(
            content=body,
            is_error=exit_code != 0,
            spilled_path=capped.spilled_path,
            summary=summarize_for_ui(raw) if raw.strip() else f"exit {exit_code}",
            metadata={"exit_code": exit_code, "truncated": capped.truncated},
        )

    def stream(self, params: dict[str, Any], ctx: ToolContext) -> AsyncIterator[str]:
        return self.shell.stream(str(params["command"]), self._timeout(params, ctx))

    @staticmethod
    def _timeout(params: dict[str, Any], ctx: ToolContext) -> float:
        """Model request, bounded by the configured ceiling.

        Both bounds come from settings rather than module constants, so
        `bash.timeout_seconds` in settings.json actually does something.
        """
        settings = ctx.settings.bash
        requested = params.get("timeout")
        default = float(getattr(settings, "timeout_seconds", DEFAULT_TIMEOUT))
        ceiling = float(getattr(settings, "max_timeout_seconds", MAX_TIMEOUT))
        return min(float(requested) if requested else default, ceiling)


async def _child_pids(parent: int) -> list[int]:
    """Direct children of a process, via ``pgrep -P``. Empty when none or unavailable."""
    try:
        process = await asyncio.create_subprocess_exec(
            "pgrep",
            "-P",
            str(parent),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
    except (OSError, TimeoutError):
        return []
    return [int(line) for line in stdout.decode().split() if line.isdigit()]


class BashOutputTool(Tool):
    """Read new output from a background job.

    Without this the Bash tool could start a job and never hear from it again,
    which makes ``run_in_background`` worse than useless: the work happens where
    nobody can see it.
    """

    name = "BashOutput"
    description = (
        "Read output from a background job started by Bash with run_in_background.\n\n"
        "Returns lines since the last read. Call it again to poll for more."
    )
    mutating = False

    def __init__(self, jobs: BackgroundJobs) -> None:
        self.jobs = jobs

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "since_line": {
                    "type": "integer",
                    "description": "Skip this many lines from the start (default: continue)",
                },
            },
            "required": ["job_id"],
        }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        job_id = str(params["job_id"])
        since = params.get("since_line")
        text, next_line = self.jobs.read(job_id, int(since) if since is not None else None)

        capped = cap_output(
            text,
            session_id=ctx.session_id,
            tool_use_id=ctx.tool_use_id,
            char_cap=ctx.settings.context.tool_output_char_cap,
            line_cap=ctx.settings.context.tool_output_line_cap,
        )
        status = self.jobs.state(job_id)
        body = capped.text
        if not status["running"]:
            body += f"\n\n[job finished, exit code {status['exit_code']}]"

        return ToolResult(
            content=body,
            spilled_path=capped.spilled_path,
            summary=f"{next_line} lines, {'running' if status['running'] else 'finished'}",
            metadata={"next_line": next_line, **status},
        )


class KillShellTool(Tool):
    """Stop a background job."""

    name = "KillShell"
    description = "Stop a background job started by Bash with run_in_background."
    mutating = True

    def __init__(self, jobs: BackgroundJobs) -> None:
        self.jobs = jobs

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        }

    def permission_specifier(self, params: dict[str, Any]) -> str | None:
        return str(params.get("job_id", ""))

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        job_id = str(params["job_id"])
        self.jobs.kill(job_id)
        return ToolResult(content=f"Killed {job_id}.", summary=f"killed {job_id}")


def _parse_exit_code(tail: str) -> int:
    for token in tail.strip().split():
        if token.lstrip("-").isdigit():
            return int(token)
    return 0
