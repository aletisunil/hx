"""Hook loading and execution.

**Where hooks may be declared is a security boundary, not a convenience.** A
hook is an arbitrary shell command, so HX loads hooks only from files the user
owns:

- ``~/.hx/settings.json``
- ``~/.hx/projects/<slug>/settings.local.json``

Hooks in a project's checked-in ``./.hx/settings.json`` are **ignored**, and the
session says so at startup. That file arrives with a clone, and cloning a
repository must never be enough to run commands on the machine that cloned it.
A team that wants a shared hook copies it into the local layer once, which is a
deliberate act by the person who will run it.

This is the one place HX departs from Claude Code's hook format.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from hx.config import read_settings_file
from hx.hooks.spec import (
    BLOCK_EXIT_CODE,
    DEFAULT_TIMEOUT_SECONDS,
    HookCommand,
    HookEvent,
    HookOutcome,
)
from hx.paths import project_local_settings_file, project_settings_file, user_settings_file

MAX_OUTPUT_BYTES = 64_000
"""A hook that prints a megabyte is a bug; truncate rather than feed it forward."""


class HookEngine:
    """Holds the loaded hooks and runs the ones that match."""

    def __init__(
        self,
        hooks: dict[HookEvent, list[HookCommand]] | None = None,
        cwd: Path | None = None,
        session_id: str = "",
        ignored: list[str] | None = None,
    ) -> None:
        self._hooks = hooks or {}
        self._cwd = cwd or Path.cwd()
        self._session_id = session_id
        self.ignored = ignored or []
        """Hooks refused because of where they were declared. Shown at startup."""

    @classmethod
    def load(cls, cwd: Path, session_id: str = "") -> HookEngine:
        trusted: dict[HookEvent, list[HookCommand]] = {}
        for path in (user_settings_file(), project_local_settings_file(cwd)):
            _collect(read_settings_file(path).get("hooks"), str(path), trusted)

        untrusted: dict[HookEvent, list[HookCommand]] = {}
        shared = project_settings_file(cwd)
        _collect(read_settings_file(shared).get("hooks"), str(shared), untrusted)
        ignored = [
            f"{event}: {command.command}"
            for event, commands in untrusted.items()
            for command in commands
        ]
        return cls(hooks=trusted, cwd=cwd, session_id=session_id, ignored=ignored)

    def __bool__(self) -> bool:
        return any(self._hooks.values())

    def commands(self, event: HookEvent) -> list[HookCommand]:
        return list(self._hooks.get(event, ()))

    def all_commands(self) -> dict[HookEvent, list[HookCommand]]:
        return {event: list(commands) for event, commands in self._hooks.items() if commands}

    async def pre_tool_use(self, tool_name: str, tool_input: dict[str, Any]) -> HookOutcome:
        return await self._fire(
            HookEvent.PRE_TOOL_USE,
            tool_name,
            {"tool_name": tool_name, "tool_input": tool_input},
        )

    async def post_tool_use(
        self, tool_name: str, tool_input: dict[str, Any], response: str, is_error: bool
    ) -> HookOutcome:
        return await self._fire(
            HookEvent.POST_TOOL_USE,
            tool_name,
            {
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_response": {"content": response, "is_error": is_error},
            },
        )

    async def user_prompt_submit(self, prompt: str) -> HookOutcome:
        return await self._fire(HookEvent.USER_PROMPT_SUBMIT, "", {"prompt": prompt})

    async def stop(self) -> HookOutcome:
        return await self._fire(HookEvent.STOP, "", {})

    async def _fire(self, event: HookEvent, tool_name: str, payload: dict[str, Any]) -> HookOutcome:
        outcome = HookOutcome()
        matching = [hook for hook in self._hooks.get(event, ()) if hook.matches(tool_name)]
        if not matching:
            return outcome

        body = {
            "session_id": self._session_id,
            "cwd": str(self._cwd),
            "hook_event_name": str(event),
            **payload,
        }
        document = json.dumps(body)

        for hook in matching:
            await self._run_one(hook, document, outcome)
            if outcome.blocked:
                # First refusal wins. Running the rest would be asking hooks
                # that cannot change the answer.
                break
        return outcome

    async def _run_one(self, hook: HookCommand, document: str, outcome: HookOutcome) -> None:
        try:
            process = await asyncio.create_subprocess_shell(
                hook.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._cwd),
            )
        except OSError as exc:
            outcome.errors.append(f"{hook.command}: {exc}")
            return

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(document.encode("utf-8")), timeout=hook.timeout
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            outcome.errors.append(f"{hook.command}: timed out after {hook.timeout}s")
            return

        _apply(hook, process.returncode or 0, _clip(stdout), _clip(stderr), outcome)


def _apply(hook: HookCommand, code: int, stdout: str, stderr: str, outcome: HookOutcome) -> None:
    """Fold one hook's result into the merged outcome."""
    decision = _decision(stdout)

    if code == BLOCK_EXIT_CODE:
        outcome.blocked = True
        outcome.reason = (decision.get("reason") or stderr or "blocked by hook").strip()
        return
    if code != 0:
        outcome.errors.append(
            f"{hook.command}: exit {code}{f' - {stderr.strip()}' if stderr else ''}"
        )
        return

    if decision.get("decision") == "block":
        outcome.blocked = True
        outcome.reason = str(decision.get("reason") or "blocked by hook").strip()
        return

    updated = decision.get("updatedInput")
    if isinstance(updated, dict):
        outcome.updated_input = updated

    extra = decision.get("additionalContext")
    if isinstance(extra, str) and extra.strip():
        outcome.context.append(extra.strip())


def _decision(stdout: str) -> dict[str, Any]:
    """Parse a hook's stdout as JSON, tolerating a hook that just prints."""
    text = stdout.strip()
    if not text.startswith("{"):
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _collect(raw: Any, source: str, into: dict[HookEvent, list[HookCommand]]) -> None:
    """Read one layer's ``hooks`` block. Malformed entries are skipped, not fatal."""
    if not isinstance(raw, dict):
        return
    for event_name, groups in raw.items():
        try:
            event = HookEvent(event_name)
        except ValueError:
            continue
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            matcher = str(group.get("matcher", ""))
            for entry in group.get("hooks", []) or []:
                if not isinstance(entry, dict):
                    continue
                command = str(entry.get("command", "")).strip()
                if not command or entry.get("type", "command") != "command":
                    continue
                into.setdefault(event, []).append(
                    HookCommand(
                        command=command,
                        matcher=matcher,
                        timeout=int(entry.get("timeout", DEFAULT_TIMEOUT_SECONDS)),
                        source=source,
                    )
                )


def _clip(raw: bytes) -> str:
    return raw[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
