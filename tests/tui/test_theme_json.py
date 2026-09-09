"""Themes load from JSON, including pi's own files, and fail loudly when incomplete."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hx.tui.theme import THEME, builtins, palettes
from hx.tui.theme_json import load_file, parse, user_palettes


def _shipped(name: str) -> dict:
    path = Path("src/hx/tui/themes") / f"{name}.json"
    return json.loads(path.read_text())


def test_the_shipped_themes_all_load() -> None:
    assert set(builtins()) == {"dark", "light", "ansi"}
    assert builtins()["dark"].dark is True
    assert builtins()["light"].dark is False


def test_vars_are_resolved_and_literals_pass_through() -> None:
    palette = builtins()["dark"]
    assert palette.success == "#b5bd68"  # "green" in vars
    assert palette.md_link == "#81a2be"  # a literal


def test_a_missing_role_is_an_error_not_a_silent_black() -> None:
    data = _shipped("dark")
    del data["colors"]["accent"]
    with pytest.raises(ValueError, match="accent"):
        parse(data, source="test")


def test_an_unknown_role_is_an_error() -> None:
    data = _shipped("dark")
    data["colors"]["mdSparkle"] = "#ffffff"
    with pytest.raises(ValueError, match="mdSparkle"):
        parse(data, source="test")


def test_a_pi_theme_file_loads_unchanged() -> None:
    """pi keeps page and card colours in `export`, and carries no `dark` flag.

    Both are covered so its theme files can be dropped in as-is, which is the
    point of using its role names.
    """
    data = _shipped("dark")
    del data["colors"]["background"]
    del data["colors"]["surface"]
    del data["colors"]["panel"]
    del data["dark"]

    palette = parse(data, name="pi-dark", source="test")

    assert palette.background == "#18181e"  # from export.pageBg
    assert palette.surface == "#1e1e24"  # from export.cardBg
    assert palette.panel == palette.surface  # derived, pi has no such role
    assert palette.dark is True  # inferred from the background


def test_a_user_theme_is_discovered(hx_home: Path) -> None:
    themes = hx_home / "themes"
    themes.mkdir()
    data = _shipped("dark")
    data["colors"]["accent"] = "#ff00ff"
    (themes / "mine.json").write_text(json.dumps(data))

    assert user_palettes()["mine"].accent == "#ff00ff"
    assert "mine" in palettes()

    THEME.use("mine")
    assert THEME.color("accent") == "#ff00ff"


def test_a_broken_user_theme_is_skipped_not_fatal(hx_home: Path) -> None:
    themes = hx_home / "themes"
    themes.mkdir()
    (themes / "broken.json").write_text("{ nope")

    assert "broken" not in user_palettes()
    assert set(palettes()) >= {"dark", "light", "ansi"}


def test_the_filename_names_the_theme(hx_home: Path) -> None:
    themes = hx_home / "themes"
    themes.mkdir()
    data = _shipped("light")
    path = themes / "solarised.json"
    path.write_text(json.dumps(data))

    assert load_file(path).name == "solarised"


def test_syntax_colours_follow_the_theme() -> None:
    from hx.tui.theme import syntax_style

    THEME.use("dark")
    dark = syntax_style()
    THEME.use("light")
    light = syntax_style()
    assert dark.styles != light.styles
