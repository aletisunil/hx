"""How the settings layers stack, and which one HX itself writes to."""

from __future__ import annotations

import json
from pathlib import Path

from hx.config import load_settings


def test_the_local_layer_overrides_the_shared_project_layer(project: Path) -> None:
    (project / ".hx" / "settings.json").write_text(json.dumps({"theme": "dark"}))
    (project / ".hx" / "settings.local.json").write_text(json.dumps({"theme": "light"}))

    assert load_settings(project).theme == "light"


def test_permission_lists_union_across_all_three_layers(project: Path, hx_home: Path) -> None:
    """A project deny must survive a local allow being added next to it."""
    (hx_home / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}))
    (project / ".hx" / "settings.json").write_text(
        json.dumps({"permissions": {"deny": ["Bash(curl:*)"]}})
    )
    (project / ".hx" / "settings.local.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(git push:*)"]}})
    )

    permissions = load_settings(project).permissions
    assert set(permissions.allow) == {"Bash(ls:*)", "Bash(git push:*)"}
    assert permissions.deny == ("Bash(curl:*)",)


def test_a_missing_local_layer_changes_nothing(project: Path) -> None:
    (project / ".hx" / "settings.json").write_text(json.dumps({"theme": "light"}))

    assert not (project / ".hx" / "settings.local.json").exists()
    assert load_settings(project).theme == "light"
