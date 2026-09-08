"""Skill discovery and progressive disclosure."""

from __future__ import annotations

from pathlib import Path

import pytest

from hx.frontmatter import FrontmatterError
from hx.skills.loader import build_index, discover, parse_skill_file
from hx.skills.runtime import ActiveSkills, SkillTool


def write_skill(root: Path, name: str, description: str, body: str = "Do the thing.") -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n")
    return path


def test_index_contains_descriptions_but_not_bodies(project: Path) -> None:
    """Progressive disclosure is the whole point - bodies must stay out of the prefix."""
    write_skill(project / ".hx" / "skills", "deploy", "Ship a release", "SECRET BODY STEPS")
    skills = discover(project)
    index = build_index(skills)

    assert "Ship a release" in index
    assert "SECRET BODY STEPS" not in index


def test_index_is_sorted_for_prefix_stability(project: Path) -> None:
    root = project / ".hx" / "skills"
    for name in ("zebra", "alpha", "middle"):
        write_skill(root, name, f"the {name} skill")

    lines = build_index(discover(project)).splitlines()[1:]
    assert lines == sorted(lines)


def test_project_skills_shadow_user_skills(project: Path, hx_home: Path) -> None:
    write_skill(hx_home / "skills", "review", "user version")
    write_skill(project / ".hx" / "skills", "review", "project version")

    skills = discover(project)
    assert len(skills) == 1
    assert skills[0].description == "project version"
    assert skills[0].source == "project"


def test_malformed_skill_is_skipped_not_fatal(project: Path) -> None:
    """One broken skill must not cost the user every other skill."""
    root = project / ".hx" / "skills"
    write_skill(root, "good", "works fine")
    broken = root / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("---\nname: broken\n---\nno description\n")

    skills = discover(project)
    assert [skill.name for skill in skills] == ["good"]


def test_unterminated_frontmatter_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text("---\nname: x\ndescription: y\nbody without close\n")
    with pytest.raises(FrontmatterError, match="never closed"):
        parse_skill_file(path)


def test_long_descriptions_are_clipped(project: Path) -> None:
    """Descriptions sit in the cached prefix for every installed skill."""
    write_skill(project / ".hx" / "skills", "verbose", "x" * 2000)
    assert len(discover(project)[0].description) <= 400


def test_no_skills_produces_an_empty_index(project: Path) -> None:
    assert build_index(discover(project)) == ""


async def test_skill_tool_returns_the_body_on_demand(project: Path, tmp_path: Path) -> None:
    from hx.config import load_settings
    from hx.tools.base import ToolContext, ToolError

    write_skill(project / ".hx" / "skills", "deploy", "Ship a release", "1. Run the tests")
    skills = {skill.name: skill for skill in discover(project)}
    tool = SkillTool(skills)

    ctx = ToolContext(project, "s", "t", load_settings(project), lambda _c: None)
    result = await tool.run({"name": "deploy"}, ctx)
    assert "1. Run the tests" in result.content

    assert tool.schema()["properties"]["name"]["enum"] == ["deploy"]
    with pytest.raises(ToolError, match="unknown skill"):
        await tool.run({"name": "nope"}, ctx)


def test_active_skill_allowlists_only_narrow() -> None:
    """A second skill must never widen the first one's restriction."""
    from hx.skills.loader import Skill

    active = ActiveSkills()
    assert active.tool_allowlist() is None

    active.activate(Skill("a", "d", "b", Path("a"), allowed_tools=("Read", "Grep", "Bash")))
    assert active.tool_allowlist() == {"Read", "Grep", "Bash"}

    active.activate(Skill("b", "d", "b", Path("b"), allowed_tools=("Read", "Write")))
    assert active.tool_allowlist() == {"Read"}
