"""Hook types.

A hook is a shell command HX runs at a named point in the turn. It reads a JSON
event on stdin and answers with an exit code, optionally with JSON on stdout.

The wire format deliberately matches Claude Code's, so a team that already has
``PreToolUse`` hooks on disk can point HX at them without a rewrite. What does
not match is where hooks may be declared - see :mod:`hx.hooks.engine`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

DEFAULT_TIMEOUT_SECONDS = 60

BLOCK_EXIT_CODE = 2
"""Exit 2 blocks the action and hands stderr to the model as the reason.

Any other non-zero exit is a broken hook, not a decision: it is surfaced to the
user as a notice and the action proceeds. A hook with a typo in it must not
silently wedge the session."""


class HookEvent(StrEnum):
    PRE_TOOL_USE = "PreToolUse"
    """Before a tool runs. May deny the call or rewrite its input."""

    POST_TOOL_USE = "PostToolUse"
    """After a tool returns. May add context for the model."""

    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    """Before a user message is sent. May deny it or add context."""

    STOP = "Stop"
    """When a turn ends."""


@dataclass(frozen=True, slots=True)
class HookCommand:
    command: str
    matcher: str = ""
    """Regex matched against the tool name in full. Empty or ``*`` matches all."""

    timeout: int = DEFAULT_TIMEOUT_SECONDS
    source: str = ""
    """The settings file it came from, so ``/hooks`` can show it."""

    def matches(self, tool_name: str) -> bool:
        if not self.matcher or self.matcher == "*":
            return True
        try:
            return re.fullmatch(self.matcher, tool_name) is not None
        except re.error:
            # A malformed matcher matches nothing rather than everything. The
            # engine reports it separately; failing open would be the wrong way
            # round for something whose job is to say no.
            return False


@dataclass(slots=True)
class HookOutcome:
    """The merged verdict of every hook that ran for one event."""

    blocked: bool = False
    reason: str = ""
    updated_input: dict[str, object] | None = None
    """``PreToolUse`` only: input the hook rewrote, applied before the tool runs."""

    context: list[str] = field(default_factory=list)
    """``additionalContext`` strings, injected for the model to read."""

    errors: list[str] = field(default_factory=list)
    """Hooks that failed to run or crashed. Reported, never blocking."""

    @property
    def context_text(self) -> str:
        return "\n".join(self.context)
