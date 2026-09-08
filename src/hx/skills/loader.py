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

from dataclasses import dataclass, field
from pathlib import Path


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


def discover(cwd: Path) -> list[Skill]:
    """Find every skill, project-first. Malformed skills are skipped with a
    warning rather than aborting startup."""
    raise NotImplementedError


def parse_skill_file(path: Path) -> Skill:
    """Parse frontmatter + body.

    Raises:
        InvalidSkill: on missing ``name``/``description`` or unparseable frontmatter.
    """
    raise NotImplementedError


def build_index(skills: list[Skill]) -> str:
    """Render the prefix index: one ``- name: description`` line per skill,
    sorted by name so the block is byte-stable across runs."""
    raise NotImplementedError


class InvalidSkill(Exception):
    pass
