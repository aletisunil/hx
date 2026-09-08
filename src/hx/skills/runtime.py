"""The ``Skill`` tool - loads a skill body into the conversation on demand."""

from __future__ import annotations

from typing import Any

from hx.skills.loader import Skill
from hx.tools.base import Tool, ToolContext, ToolError, ToolResult

DESCRIPTION = """Load a skill's full instructions.

Skills are listed in the system context by name and description. Call this when
the current task matches one; its instructions then apply for the rest of the turn."""


class SkillTool(Tool):
    name = "Skill"
    description = DESCRIPTION
    mutating = False

    def __init__(self, skills: dict[str, Skill], active: ActiveSkills | None = None) -> None:
        self.skills = skills
        self.active = active if active is not None else ActiveSkills()

    def schema(self) -> dict[str, Any]:
        """``name`` is an enum of installed skills, so an invalid name fails at
        validation rather than after a round trip."""
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "enum": sorted(self.skills)},
            },
            "required": ["name"],
        }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Returns the skill body as the tool result.

        Skill bodies are author-controlled instructions the user installed, but
        they are still content, not privileged directives: an ``allowed-tools``
        restriction narrows permissions and never widens them.
        """
        name = str(params["name"])
        skill = self.skills.get(name)
        if skill is None:
            available = ", ".join(sorted(self.skills)) or "none"
            raise ToolError(f"unknown skill {name!r}. Installed skills: {available}")

        self.active.activate(skill)

        body = skill.body
        if skill.resources:
            listed = "\n".join(f"- {item}" for item in skill.resources)
            body += f"\n\nFiles shipped with this skill:\n{listed}"
        if skill.allowed_tools:
            body += f"\n\nWhile this skill is active, use only: {', '.join(skill.allowed_tools)}."

        return ToolResult(content=body, summary=f"loaded {name}")


class ActiveSkills:
    """Tracks skills loaded this session, for the status bar and tool gating."""

    def __init__(self) -> None:
        self._active: dict[str, Skill] = {}

    def activate(self, skill: Skill) -> None:
        self._active[skill.name] = skill

    def names(self) -> list[str]:
        return sorted(self._active)

    def tool_allowlist(self) -> set[str] | None:
        """Intersection of every active skill's ``allowed-tools``, or ``None``
        when unrestricted.

        Intersection, not union: a skill's allowlist may only narrow what is
        available. Loading a second skill must never widen the first one's
        restriction.
        """
        allowlists = [set(s.allowed_tools) for s in self._active.values() if s.allowed_tools]
        if not allowlists:
            return None
        return set.intersection(*allowlists)
