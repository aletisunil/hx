"""Skill discovery.

A skill is a directory containing ``SKILL.md`` with YAML frontmatter. Searched
in ``<cwd>/.hx/skills/*/`` then ``~/.hx/skills/*/``; project skills shadow user
skills of the same name.

Progressive disclosure is the whole point: only ``name`` and ``description``
enter the cached prefix. The body is injected only when the model calls
``Skill(name)``, so a hundred installed skills cost a hundred one-line entries
rather than a hundred documents.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path

from hx.frontmatter import FrontmatterError, read, require, string_tuple
from hx.paths import project_skills_dir, user_skills_dir

SKILL_FILE = "SKILL.md"
MAX_DESCRIPTION_CHARS = 400

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Skill:
    name: str
    description: str
    body: str
    path: Path
    allowed_tools: tuple[str, ...] = ()
    """Restricts which tools may run while the skill is active. Empty means no restriction."""
    resources: tuple[Path, ...] = field(default_factory=tuple)
    """Scripts and reference files shipped alongside SKILL.md."""
    source: str = ""


def discover(cwd: Path) -> list[Skill]:
    """Find every skill, project-first. Malformed skills are skipped with a
    warning rather than aborting startup."""
    found: dict[str, Skill] = {}

    # User first, so a project skill of the same name overwrites it.
    for directory, source in ((user_skills_dir(), "user"), (project_skills_dir(cwd), "project")):
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            skill_file = entry / SKILL_FILE
            if not skill_file.is_file():
                continue
            try:
                skill = parse_skill_file(skill_file)
            except FrontmatterError as exc:
                log.warning("skipping skill: %s", exc)
                continue
            found[skill.name] = replace(skill, source=source)

    return sorted(found.values(), key=lambda skill: skill.name)


def parse_skill_file(path: Path) -> Skill:
    """Parse frontmatter + body.

    Raises:
        InvalidSkill: on missing ``name``/``description`` or unparseable frontmatter.
    """
    document = read(path)
    require(document, "name", "description")

    description = str(document.metadata["description"]).strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        # Descriptions live in the cached prefix for every skill installed.
        description = description[: MAX_DESCRIPTION_CHARS - 1] + "…"

    resources = tuple(sorted(item for item in path.parent.iterdir() if item.name != SKILL_FILE))
    return Skill(
        name=str(document.metadata["name"]).strip(),
        description=description,
        body=document.body,
        path=path,
        allowed_tools=string_tuple(document.metadata.get("allowed-tools")),
        resources=resources,
    )


def build_index(skills: list[Skill]) -> str:
    """Render the prefix index: one ``- name: description`` line per skill,
    sorted by name so the block is byte-stable across runs."""
    if not skills:
        return ""
    lines = [
        "Available skills. Call Skill(name) to load one when the task matches it:",
        *sorted(f"- {skill.name}: {skill.description}" for skill in skills),
    ]
    return "\n".join(lines)


InvalidSkill = FrontmatterError
"""Skills fail the same way as any other frontmatter document."""
