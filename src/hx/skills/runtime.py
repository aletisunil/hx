"""The ``Skill`` tool - loads a skill body into the conversation on demand."""

from __future__ import annotations

from typing import Any

from hx.skills.loader import Skill
from hx.tools.base import Tool, ToolContext, ToolResult

DESCRIPTION = """Load a skill's full instructions.

Skills are listed in the system context by name and description. Call this when
the current task matches one; its instructions then apply for the rest of the turn."""


class SkillTool(Tool):
    name = "Skill"
    description = DESCRIPTION
    mutating = False

    def __init__(self, skills: dict[str, Skill]) -> None:
        raise NotImplementedError

    def schema(self) -> dict[str, Any]:
        """``name`` is an enum of installed skills, so an invalid name fails at
        validation rather than after a round trip."""
        raise NotImplementedError

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Returns the skill body as the tool result.

        Skill bodies are author-controlled instructions the user installed, but
        they are still content, not privileged directives: an ``allowed-tools``
        restriction narrows permissions and never widens them.
        """
        raise NotImplementedError


class ActiveSkills:
    """Tracks skills loaded this session, for the status bar and tool gating."""

    def __init__(self) -> None:
        raise NotImplementedError

    def activate(self, skill: Skill) -> None:
        raise NotImplementedError

    def tool_allowlist(self) -> set[str] | None:
        """Intersection of every active skill's ``allowed-tools``, or ``None``
        when unrestricted."""
        raise NotImplementedError
