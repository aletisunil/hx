"""Agent definitions from ``.hx/agents/*.md`` and ``~/.hx/agents/*.md``.

Same frontmatter shape as skills, plus ``tools`` and ``model``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class AgentDefinition:
    name: str
    description: str
    """Shown to the parent model in the Task tool schema, so it picks the right agent."""
    system_prompt: str
    tools: tuple[str, ...] = ()
    """Allowlist. Empty means all tools except Task."""
    model: str | None = None
    """Defaults to ``settings.models.subagent_model``."""
    path: Path | None = None


BUILTIN_AGENTS = ("explore", "plan", "general")
"""Always available: read-only search, planning, and a catch-all."""


def discover(cwd: Path) -> list[AgentDefinition]:
    """Builtins plus user and project definitions, project last."""
    raise NotImplementedError


def parse_agent_file(path: Path) -> AgentDefinition:
    raise NotImplementedError


class InvalidAgent(Exception):
    pass
