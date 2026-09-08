"""Task tool: spawn a subagent.

The subagent runs its own :class:`~hx.core.loop.AgentLoop` with an isolated
transcript, so a long exploratory search costs the parent only the final report
instead of every intermediate tool result.
"""

from __future__ import annotations

from typing import Any

from hx.tools.base import Tool, ToolContext, ToolResult

DESCRIPTION = """Launch a subagent to handle a multi-step task in its own context.

The subagent cannot ask follow-up questions, so the prompt must be
self-contained. Only its final report returns to this conversation."""


class TaskTool(Tool):
    name = "Task"
    description = DESCRIPTION
    mutating = True
    """Conservatively mutating: a subagent may run tools that write."""

    def __init__(self, runner: Any) -> None:
        """Args:
        runner: ``hx.agents.subagent.SubagentRunner``.
        """
        raise NotImplementedError

    def schema(self) -> dict[str, Any]:
        """The ``subagent_type`` enum is filled from the loaded agent definitions."""
        raise NotImplementedError

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise NotImplementedError
