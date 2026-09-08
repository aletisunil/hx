"""Skill discovery and progressive disclosure."""

from __future__ import annotations

from pathlib import Path

from hx.skills.loader import build_index, discover
from tests.conftest import unimplemented


@unimplemented
def test_index_contains_descriptions_but_not_bodies(project: Path) -> None:
    """Progressive disclosure is the whole point - bodies must stay out of the prefix."""
    skills = discover(project)
    index = build_index(skills)
    for skill in skills:
        assert skill.description in index
        assert skill.body not in index


@unimplemented
def test_index_is_sorted_for_prefix_stability(project: Path) -> None:
    skills = discover(project)
    lines = build_index(skills).splitlines()
    assert lines == sorted(lines)


@unimplemented
def test_project_skills_shadow_user_skills(project: Path) -> None:
    raise NotImplementedError


@unimplemented
def test_malformed_skill_is_skipped_not_fatal(project: Path) -> None:
    raise NotImplementedError
