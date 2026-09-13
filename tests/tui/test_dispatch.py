"""Which frontend runs, and the boundary that lets there be two of them.

The renderer replacement is only safe while ``hx.cli`` keeps importing one
name that never moves and the escape hatch back to the old app keeps working.
Both are load-bearing for every stage, so both are asserted here.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from hx.config import ConfigError, Settings, TuiRenderer, TuiSettings, load_settings
from hx.tui.app import ENV_VAR, selected_renderer

SRC = Path(__file__).resolve().parents[2] / "src" / "hx"


def _with(renderer: TuiRenderer) -> Settings:
    return replace(Settings(), tui=TuiSettings(renderer=renderer))


def test_the_old_app_is_still_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert selected_renderer(Settings()) is TuiRenderer.LEGACY


def test_the_setting_selects_the_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert selected_renderer(_with(TuiRenderer.NEW)) is TuiRenderer.NEW


@pytest.mark.parametrize("value", ["new", "NEW", "  new  "])
def test_the_environment_overrides_the_setting(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(ENV_VAR, value)
    assert selected_renderer(Settings()) is TuiRenderer.NEW


def test_the_environment_can_always_get_back_to_the_old_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The escape hatch from a frontend that will not draw cannot require
    editing a config file inside the terminal that is not drawing."""
    monkeypatch.setenv(ENV_VAR, "legacy")
    assert selected_renderer(_with(TuiRenderer.NEW)) is TuiRenderer.LEGACY


def test_a_typo_in_the_override_is_ignored_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """It arrives from a shell profile as often as from a deliberate choice."""
    monkeypatch.setenv(ENV_VAR, "nonsense")
    assert selected_renderer(Settings()) is TuiRenderer.LEGACY


def test_an_empty_override_defers_to_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "")
    assert selected_renderer(_with(TuiRenderer.NEW)) is TuiRenderer.NEW


def test_the_setting_round_trips_through_config(project: Path) -> None:
    (project / ".hx" / "settings.json").write_text('{"tui": {"renderer": "new"}}')
    assert load_settings(project).tui.renderer is TuiRenderer.NEW


def test_an_unknown_renderer_in_config_names_the_known_ones(project: Path) -> None:
    (project / ".hx" / "settings.json").write_text('{"tui": {"renderer": "ncurses"}}')
    with pytest.raises(ConfigError, match=r"ncurses.*legacy, new"):
        load_settings(project)


def test_the_cli_imports_the_dispatcher_and_not_a_frontend() -> None:
    """``hx.tui.app.run_tui`` is the one symbol the CLI knows about. If a stage
    ever points this at a concrete frontend, the staged rollout has been lost."""
    tree = ast.parse((SRC / "cli.py").read_text())
    imported = {
        f"{node.module}.{alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    }
    assert "hx.tui.app.run_tui" in imported
    assert not any(name.startswith("hx.tui.legacy") for name in imported)


def test_the_shared_half_of_the_tui_did_not_move_into_legacy() -> None:
    """The palette, theme loader, matcher, kill ring and clipboard are used by
    both frontends. If one gets pulled into ``legacy/``, deleting Textual takes
    it with it."""
    for name in (
        "roles.py",
        "theme.py",
        "theme_json.py",
        "fuzzy.py",
        "killring.py",
        "clipboard.py",
    ):
        assert (SRC / "tui" / name).is_file(), f"{name} belongs to both frontends"
    assert (SRC / "tui" / "themes" / "dark.json").is_file()


def test_nothing_outside_the_tui_reaches_into_the_old_app() -> None:
    """The core never imports the frontend; that is what makes it replaceable."""
    offenders = [
        path.relative_to(SRC.parent)
        for path in SRC.rglob("*.py")
        if "tui" not in path.relative_to(SRC).parts and "hx.tui.legacy" in path.read_text()
    ]
    assert offenders == []
