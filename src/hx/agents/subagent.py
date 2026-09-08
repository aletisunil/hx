"""Subagent execution.

Each subagent gets its own :class:`~hx.core.loop.AgentLoop`, transcript, tool
allowlist and model. ``Task`` is never in a subagent's allowlist, so recursion
is impossible by construction rather than by a depth counter.

Only the final assistant text returns to the parent as the tool result - the
subagent's intermediate tool output never enters the parent's context, which is
the entire reason to spawn one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hx.agents.definitions import AgentDefinition
from hx.core.events import SubagentFinished, SubagentStarted

if TYPE_CHECKING:
    from hx.config import Settings
    from hx.core.events import EventBus
    from hx.permissions.engine import PermissionEngine
    from hx.providers.base import Provider
    from hx.providers.models import ModelRegistry
    from hx.tools.registry import ToolRegistry

NEVER_AVAILABLE_TO_SUBAGENTS = frozenset({"Task"})
"""Recursion is prevented by omission, not by a depth counter that someone will
later raise 'just for this case'."""


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
        permissions: PermissionEngine | None,
        bus: EventBus,
        settings: Settings,
        models: ModelRegistry | None = None,
        parent_session_id: str | None = None,
        parent_usage: object | None = None,
    ) -> None:
        self.definitions = definitions
        self.provider = provider
        self.tools = tools
        self.permissions = permissions
        self.bus = bus
        self.settings = settings
        self.models = models
        self.parent_session_id = parent_session_id
        self.parent_usage = parent_usage
        self._active: dict[str, str] = {}

    async def run(self, agent_type: str, prompt: str, description: str) -> SubagentResult:
        """Run one subagent to completion.

        Permission prompts from a subagent surface in the parent TUI attributed
        to that subagent - an approval modal with no visible origin is not an
        informed approval.
        """
        definition = self.definitions.get(agent_type)
        if definition is None:
            available = ", ".join(sorted(self.definitions)) or "none"
            return SubagentResult(
                subagent_id="",
                agent_type=agent_type,
                report=f"Unknown agent type {agent_type!r}. Available: {available}",
                is_error=True,
                turns=0,
                cost_usd=0.0,
            )

        subagent_id = f"sub_{uuid.uuid4().hex[:8]}"
        self._active[subagent_id] = agent_type
        self.bus.publish(
            SubagentStarted(subagent_id=subagent_id, agent_type=agent_type, description=description)
        )

        try:
            loop = self._build_loop(definition, subagent_id)
            result = await loop.run(prompt)
            report = _final_text(result) or "(the subagent returned no text)"
            cost = loop.session.usage.total_cost_usd
            self._roll_up_cost(loop)
            return SubagentResult(
                subagent_id=subagent_id,
                agent_type=agent_type,
                report=report,
                is_error=result.error is not None,
                turns=len(loop.session.usage.turns),
                cost_usd=cost,
            )
        except Exception as exc:
            return SubagentResult(
                subagent_id=subagent_id,
                agent_type=agent_type,
                report=f"The subagent failed: {type(exc).__name__}: {exc}",
                is_error=True,
                turns=0,
                cost_usd=0.0,
            )
        finally:
            self._active.pop(subagent_id, None)
            self.bus.publish(SubagentFinished(subagent_id=subagent_id, is_error=False))

    def _build_loop(self, definition: AgentDefinition, subagent_id: str):  # type: ignore[no-untyped-def]
        from hx.core.compaction import Compactor
        from hx.core.context import ContextBuilder
        from hx.core.lateinject import InjectionRegistry
        from hx.core.loop import AgentLoop
        from hx.core.session import new_session

        model = (
            definition.model or self.settings.models.subagent_model or self.settings.models.model
        )
        tools = self.tools.subset(self._allowed_tools(definition))
        context = ContextBuilder(
            definition.system_prompt,
            self.settings.cwd,
            keep_recent_turns=self.settings.context.keep_recent_turns,
        )
        session = new_session(self.settings.cwd, model, parent_id=self.parent_session_id)

        loop = AgentLoop(
            provider=self.provider,
            session=session,
            tools=tools,
            permissions=self.permissions,
            context=context,
            compactor=Compactor(
                provider=self.provider,
                model=model,
                keep_recent_turns=self.settings.context.keep_recent_turns,
                context=context,
            ),
            injections=InjectionRegistry(),
            bus=self.bus,
            settings=self.settings,
            model_info=self.models.get_or_default(model) if self.models else None,
        )
        loop.origin = f"{definition.name} subagent"
        return loop

    def _allowed_tools(self, definition: AgentDefinition) -> set[str]:
        available = set(self.tools.names()) - set(NEVER_AVAILABLE_TO_SUBAGENTS)
        if not definition.tools:
            return available
        return {name for name in definition.tools if name in available}

    def _roll_up_cost(self, loop: object) -> None:
        """Fold the subagent's usage into the parent ledger.

        A subagent that spends real money without moving the parent's cost
        display makes that display a lie.
        """
        parent = self.parent_usage
        if parent is None:
            return
        for turn in loop.session.usage.turns:  # type: ignore[attr-defined]
            parent.record(turn)  # type: ignore[attr-defined]

    def active(self) -> list[str]:
        """Ids of running subagents, for the TUI progress rows."""
        return sorted(self._active)


def _final_text(result: object) -> str:
    messages = getattr(result, "messages", [])
    for message in reversed(messages):
        if message.role == "assistant" and message.text().strip():
            return str(message.text().strip())
    return ""
