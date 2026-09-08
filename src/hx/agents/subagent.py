"""Subagent execution.

Each subagent gets its own :class:`~hx.core.loop.AgentLoop`, transcript, tool
allowlist and model. ``Task`` is never in a subagent's allowlist, so recursion
is impossible by construction rather than by a depth counter.

Only the final assistant text returns to the parent as the tool result - the
subagent's intermediate tool output never enters the parent's context, which is
the entire reason to spawn one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hx.agents.definitions import AgentDefinition

if TYPE_CHECKING:
    from hx.config import Settings
    from hx.core.events import EventBus
    from hx.permissions.engine import PermissionEngine
    from hx.providers.base import Provider
    from hx.tools.registry import ToolRegistry


@dataclass(slots=True)
class SubagentResult:
    subagent_id: str
    agent_type: str
    report: str
    is_error: bool
    turns: int
    cost_usd: float
    """Rolled into the parent session's ledger so the cost display stays honest."""


class SubagentRunner:
    """Spawns and supervises subagents."""

    def __init__(
        self,
        *,
        definitions: dict[str, AgentDefinition],
        provider: Provider,
        tools: ToolRegistry,
        permissions: PermissionEngine,
        bus: EventBus,
        settings: Settings,
    ) -> None:
        raise NotImplementedError

    async def run(self, agent_type: str, prompt: str, description: str) -> SubagentResult:
        """Run one subagent to completion.

        Permission prompts from a subagent surface in the parent TUI attributed
        to that subagent - an approval modal with no visible origin is not an
        informed approval.
        """
        raise NotImplementedError

    async def run_many(self, requests: list[tuple[str, str, str]]) -> list[SubagentResult]:
        """Run subagents concurrently via ``asyncio.TaskGroup``, preserving input order."""
        raise NotImplementedError

    def active(self) -> list[str]:
        """Ids of running subagents, for the TUI progress rows."""
        raise NotImplementedError
