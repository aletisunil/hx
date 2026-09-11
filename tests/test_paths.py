"""Where HX keeps what it writes for a project."""

from __future__ import annotations

from pathlib import Path

from hx.paths import project_local_settings_file, project_slug, project_state_dir, user_home


def test_two_projects_with_the_same_name_stay_apart(tmp_path: Path) -> None:
    """``~/work/api`` and ``~/personal/api`` flatten to the same readable half,
    which is what the digest is there to separate."""
    work = tmp_path / "work" / "api"
    personal = tmp_path / "personal" / "api"
    work.mkdir(parents=True)
    personal.mkdir(parents=True)

    assert project_slug(work) != project_slug(personal)
    assert project_slug(work) == project_slug(work), "the slug has to be stable"


def test_a_slug_is_readable_and_filesystem_safe(tmp_path: Path) -> None:
    """Somebody browsing ``~/.hx/projects`` has to be able to tell which
    directory a settings file belongs to."""
    project = tmp_path / "my project (v2)"
    project.mkdir()

    slug = project_slug(project)
    assert "my-project--v2-" in slug
    assert all(char.isalnum() or char in "-_." for char in slug)


def test_a_deep_path_keeps_the_end_that_identifies_it(tmp_path: Path) -> None:
    deep = tmp_path.joinpath(*[f"level{n}" for n in range(12)])
    deep.mkdir(parents=True)

    slug = project_slug(deep)
    assert len(slug) <= 60
    assert "level11" in slug


def test_what_hx_writes_for_a_project_lives_under_the_user_home(
    hx_home: Path, tmp_path: Path
) -> None:
    """Never inside the checkout: a grant is machine-local, and a file HX drops
    into a repository shows up in the user's next ``git status``."""
    project = tmp_path / "project"
    project.mkdir()

    local = project_local_settings_file(project)

    assert local.parent == project_state_dir(project)
    assert local.is_relative_to(user_home())
    assert not local.is_relative_to(project)
