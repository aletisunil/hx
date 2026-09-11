"""Lifting HX's own files out of the user's checkout.

Two one-time cleanups: the local layer moves from ``./.hx/settings.local.json``
to ``$HX_HOME/projects/<slug>``, and the grants that much older versions
appended to the project's shared ``settings.json`` are lifted into it.
"""

from __future__ import annotations

import json
from pathlib import Path

from hx.config import lift_project_grants, load_settings, migrate_local_settings
from hx.paths import project_local_settings_file, project_migrations_file


def _local(project: Path) -> dict:
    return json.loads(project_local_settings_file(project).read_text())


def test_the_local_layer_moves_out_of_the_repository(project: Path) -> None:
    legacy = project / ".hx" / "settings.local.json"
    legacy.write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}))

    moved = migrate_local_settings(project)

    assert moved == project_local_settings_file(project)
    assert not legacy.exists()
    assert _local(project)["permissions"]["allow"] == ["Bash(ls:*)"]


def test_the_gitignore_that_only_hid_it_goes_too(project: Path) -> None:
    """HX wrote that file for itself; with nothing left to hide it is litter."""
    (project / ".hx" / "settings.local.json").write_text("{}")
    (project / ".hx" / ".gitignore").write_text("settings.local.json\n")

    migrate_local_settings(project)

    assert not (project / ".hx" / ".gitignore").exists()
    # Nothing of HX's was left behind, so neither is the directory.
    assert not (project / ".hx").exists()


def test_a_gitignore_the_user_added_to_keeps_their_rules(project: Path) -> None:
    (project / ".hx" / "settings.local.json").write_text("{}")
    (project / ".hx" / ".gitignore").write_text("settings.local.json\nscratch/\n")

    migrate_local_settings(project)

    assert (project / ".hx" / ".gitignore").read_text() == "scratch/\n"


def test_the_newer_location_wins_where_both_exist(project: Path) -> None:
    """Anything already granted there was granted more recently."""
    destination = project_local_settings_file(project)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps({"theme": "light"}))
    (project / ".hx" / "settings.local.json").write_text(
        json.dumps({"theme": "dark", "models": {"model": "openai/gpt-5"}})
    )

    migrate_local_settings(project)

    merged = _local(project)
    assert merged["theme"] == "light"
    assert merged["models"]["model"] == "openai/gpt-5"


def test_an_unreadable_legacy_file_is_left_exactly_where_it_is(project: Path) -> None:
    """Deleting a file we cannot parse loses whatever the user had in it."""
    legacy = project / ".hx" / "settings.local.json"
    legacy.write_text("{ not json")

    assert migrate_local_settings(project) is None
    assert legacy.exists()


def test_loading_settings_performs_the_move(project: Path) -> None:
    """The user never runs a migration command; starting HX is the trigger."""
    (project / ".hx" / "settings.local.json").write_text(json.dumps({"theme": "light"}))

    assert load_settings(project).theme == "light"
    assert not (project / ".hx" / "settings.local.json").exists()


def test_grants_are_lifted_out_of_the_file_the_project_checks_in(project: Path) -> None:
    shared = project / ".hx" / "settings.json"
    shared.write_text(
        json.dumps(
            {
                "permissions": {
                    "allow": ["Bash(uv run:*)"],
                    "deny": ["Bash(curl:*)"],
                    "ask": ["Bash(git push:*)"],
                },
                "theme": "light",
            }
        )
    )

    moved = lift_project_grants(project)

    assert moved == ["Bash(uv run:*)"]
    assert _local(project)["permissions"]["allow"] == ["Bash(uv run:*)"]
    # Only `allow` moves: HX never wrote the other two, and quietly relocating a
    # deny would weaken a guardrail the rest of the team is relying on.
    kept = json.loads(shared.read_text())
    assert kept["permissions"] == {"deny": ["Bash(curl:*)"], "ask": ["Bash(git push:*)"]}
    assert kept["theme"] == "light"


def test_a_shared_file_holding_nothing_but_grants_is_removed(project: Path) -> None:
    """An empty settings file is clutter, not content."""
    shared = project / ".hx" / "settings.json"
    shared.write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}))

    lift_project_grants(project)

    assert not shared.exists()
    assert not (project / ".hx").exists()


def test_the_lift_runs_once_so_a_rule_written_later_is_left_alone(project: Path) -> None:
    """Otherwise it stops being a migration and becomes a policy, fighting the
    user every time they deliberately put a rule back."""
    shared = project / ".hx" / "settings.json"
    shared.write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}))
    lift_project_grants(project)

    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text(json.dumps({"permissions": {"allow": ["Bash(git status)"]}}))

    assert lift_project_grants(project) is None
    assert json.loads(shared.read_text())["permissions"]["allow"] == ["Bash(git status)"]


def test_a_project_with_nothing_to_lift_settles_the_question_anyway(project: Path) -> None:
    assert lift_project_grants(project) is None
    assert json.loads(project_migrations_file(project).read_text())["lifted_project_grants"]


def test_the_lift_leaves_what_is_already_granted_locally(project: Path) -> None:
    (project / ".hx" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(ls:*)", "Bash(uv run:*)"]}})
    )
    destination = project_local_settings_file(project)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}))

    lift_project_grants(project)

    assert _local(project)["permissions"]["allow"] == ["Bash(ls:*)", "Bash(uv run:*)"]
