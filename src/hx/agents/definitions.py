"""Agent definitions from ``.hx/agents/*.md`` and ``~/.hx/agents/*.md``.

Same frontmatter shape as skills, plus ``tools`` and ``model``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from hx.frontmatter import FrontmatterError, read, require, string_tuple
from hx.paths import project_agents_dir, user_agents_dir

log = logging.getLogger(__name__)


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


_EXPLORE = AgentDefinition(
    name="explore",
    description=(
        "Read-only search across the codebase. Use when finding where something lives "
        "would cost the main conversation many tool results."
    ),
    system_prompt=(
        "You are a search agent. Locate the relevant code and report back concisely.\n\n"
        "Read only what you need - excerpts, not whole files. Report file paths with "
        "line numbers and a one-line note on what each contains. Do not review or "
        "critique the code, and do not modify anything.\n\n"
        "Your reply is the entire result: the caller cannot see your tool output."
    ),
    tools=("Read", "Glob", "Grep"),
)

_PLAN = AgentDefinition(
    name="plan",
    description="Design an implementation approach for a task, without writing code.",
    system_prompt=(
        "You are a software architect. Produce a concrete implementation plan.\n\n"
        "Read the relevant code first. Name the files to change and what changes in "
        "each, reuse what already exists rather than inventing parallel machinery, "
        "and state the trade-offs you rejected. Do not modify anything.\n\n"
        "Your reply is the entire result: the caller cannot see your tool output."
    ),
    tools=("Read", "Glob", "Grep"),
)

_GENERAL = AgentDefinition(
    name="general",
    description="A multi-step task that does not fit the other agents.",
    system_prompt=(
        "Carry out the task described and report what you did.\n\n"
        "You cannot ask follow-up questions, so make reasonable decisions and state "
        "the assumptions you made. Your reply is the entire result: the caller "
        "cannot see your tool output."
    ),
)

BUILTIN_AGENTS: tuple[AgentDefinition, ...] = (_EXPLORE, _PLAN, _GENERAL)
"""Always available: read-only search, planning, and a catch-all."""


def discover(cwd: Path) -> list[AgentDefinition]:
    """Builtins plus user and project definitions, project last."""
    found: dict[str, AgentDefinition] = {agent.name: agent for agent in BUILTIN_AGENTS}

    for directory in (user_agents_dir(), project_agents_dir(cwd)):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            try:
                agent = parse_agent_file(path)
            except FrontmatterError as exc:
                log.warning("skipping agent: %s", exc)
                continue
            found[agent.name] = agent

    return sorted(found.values(), key=lambda agent: agent.name)


def parse_agent_file(path: Path) -> AgentDefinition:
    document = read(path)
    require(document, "name", "description")
    return AgentDefinition(
        name=str(document.metadata["name"]).strip(),
        description=str(document.metadata["description"]).strip(),
        system_prompt=document.body,
        tools=string_tuple(document.metadata.get("tools")),
        model=(str(document.metadata["model"]).strip() if document.metadata.get("model") else None),
        path=path,
    )


InvalidAgent = FrontmatterError
