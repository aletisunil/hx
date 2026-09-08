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
self-contained. Only its final report returns to this conversation - use this
when the intermediate tool output would not be worth its space here."""


class TaskTool(Tool):
    name = "Task"
    description = DESCRIPTION
    mutating = True
    """Conservatively mutating: a subagent may run tools that write."""

    def __init__(self, runner: Any) -> None:
        self.runner = runner

    def schema(self) -> dict[str, Any]:
        """The ``subagent_type`` enum is filled from the loaded agent definitions."""
        definitions = self.runner.definitions
        described = "\n".join(
            f"- {name}: {definition.description}"
            for name, definition in sorted(definitions.items())
        )
        return {
            "type": "object",
            "properties": {
                "subagent_type": {
                    "type": "string",
                    "enum": sorted(definitions),
                    "description": f"Which agent to run.\n{described}",
                },
                "prompt": {
                    "type": "string",
                    "description": "Self-contained task. The subagent sees nothing else.",
                },
                "description": {
                    "type": "string",
                    "description": "3-5 word label shown to the user while it runs",
                },
            },
            "required": ["subagent_type", "prompt"],
        }

    def permission_specifier(self, params: dict[str, Any]) -> str | None:
        return str(params.get("subagent_type", ""))

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        result = await self.runner.run(
            str(params["subagent_type"]),
            str(params["prompt"]),
            str(params.get("description") or params["subagent_type"]),
        )
        return ToolResult(
            content=result.report,
            is_error=result.is_error,
            summary=f"{result.agent_type}: {result.turns} turns",
            metadata={"subagent_id": result.subagent_id, "cost_usd": result.cost_usd},
        )
