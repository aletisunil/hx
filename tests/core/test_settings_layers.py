"""How the settings layers stack, and which one HX itself writes to."""

from __future__ import annotations

import json
from pathlib import Path

from hx.config import load_settings
from hx.paths import project_local_settings_file


def _write_local(project: Path, payload: dict) -> None:
    """This machine's layer for ``project``, which lives under ``$HX_HOME``."""
    path = project_local_settings_file(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def test_the_local_layer_overrides_the_shared_project_layer(project: Path) -> None:
    (project / ".hx" / "settings.json").write_text(json.dumps({"theme": "dark"}))
    _write_local(project, {"theme": "light"})

    assert load_settings(project).theme == "light"


def test_permission_lists_union_across_all_three_layers(project: Path, hx_home: Path) -> None:
    """A project deny must survive a local allow being added next to it."""
    (hx_home / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}))
    (project / ".hx" / "settings.json").write_text(
        json.dumps({"permissions": {"deny": ["Bash(curl:*)"]}})
    )
    _write_local(project, {"permissions": {"allow": ["Bash(git push:*)"]}})

    permissions = load_settings(project).permissions
    assert set(permissions.allow) == {"Bash(ls:*)", "Bash(git push:*)"}
    assert permissions.deny == ("Bash(curl:*)",)


def test_a_missing_local_layer_changes_nothing(project: Path) -> None:
    (project / ".hx" / "settings.json").write_text(json.dumps({"theme": "light"}))

    assert not project_local_settings_file(project).exists()
    assert load_settings(project).theme == "light"


def test_an_unknown_reasoning_effort_is_refused_with_the_known_ones(project: Path) -> None:
    """A typo that reaches the backend comes back as a 400 mid-turn."""
    import pytest

    from hx.config import ConfigError

    (project / ".hx" / "settings.json").write_text(
        json.dumps({"models": {"reasoning_effort": "telepathic"}})
    )

    with pytest.raises(ConfigError, match="telepathic"):
        load_settings(project)


def test_extra_codex_models_are_read_as_a_list_of_ids(project: Path) -> None:
    """The escape hatch for an id the account can call but the catalogue does
    not advertise."""
    (project / ".hx" / "settings.json").write_text(
        json.dumps({"models": {"codex_models": ["gpt-6-secret"]}})
    )

    assert load_settings(project).models.codex_models == ("gpt-6-secret",)
