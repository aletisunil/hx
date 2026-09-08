"""Permission rule engine.

Rules are ``Tool(specifier)`` strings - ``Bash(git commit:*)``, ``Edit(src/**)``,
``Read(~/.ssh/**)``. Evaluation order is deny, then ask, then allow: an explicit
deny can never be overridden by a broader allow, including in bypass mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from hx.config import PermissionMode


class Decision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class GrantScope(StrEnum):
    ONCE = "once"
    SESSION = "session"
    ALWAYS = "always"
    """Persisted to project settings.json."""


@dataclass(slots=True)
class Rule:
    tool: str
    specifier: str | None
    decision: Decision
    source: str
    """Where the rule came from, so ``/permissions`` can show it."""


@dataclass(slots=True)
class PermissionRequest:
    tool_name: str
    specifier: str | None
    params: dict[str, Any]
    mutating: bool
    description: str
    detail: str = ""
    """Rendered diff or command text shown in the approval modal."""


@dataclass(slots=True)
class PermissionOutcome:
    decision: Decision
    reason: str
    matched_rule: Rule | None = None


class PermissionEngine:
    def __init__(self, mode: PermissionMode, rules: list[Rule], cwd: Path) -> None:
        raise NotImplementedError

    def evaluate(self, request: PermissionRequest) -> PermissionOutcome:
        """Pure decision - no prompting, no I/O. Ordering: deny, ask, allow, then
        the mode default.

        For Bash the specifier is decomposed by ``hx.permissions.parser`` and every
        segment must pass; an unparseable command degrades to ASK.
        """
        raise NotImplementedError

    async def request(self, request: PermissionRequest) -> bool:
        """Evaluate and, on ASK, publish ``PermissionRequested`` and await the answer."""
        raise NotImplementedError

    def grant(self, request: PermissionRequest, scope: GrantScope) -> None:
        """Record an approval. ``ALWAYS`` writes a rule into project settings."""
        raise NotImplementedError

    def set_mode(self, mode: PermissionMode) -> None:
        raise NotImplementedError

    def allowed_tools(self, all_tools: list[str]) -> set[str]:
        """Tools exposed in the current mode - plan mode hides mutating tools
        entirely rather than letting the model call them and be refused."""
        raise NotImplementedError


def parse_rule(text: str, source: str, decision: Decision) -> Rule:
    """Parse ``Tool(specifier)`` or bare ``Tool``.

    Raises:
        InvalidRule: on malformed syntax - a rule that silently fails to parse is
            a rule that silently does not protect anything.
    """
    raise NotImplementedError


def load_rules(cwd: Path) -> list[Rule]:
    """Collect rules from user and project settings, project last."""
    raise NotImplementedError


class InvalidRule(Exception):
    pass
