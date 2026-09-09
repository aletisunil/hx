"""The README is checked against the code.

Every prior gap in this project was a claim that had drifted from behaviour.
Documentation drifts the same way and nothing catches it, so the tables that
enumerate commands, keys and settings are asserted here.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest

from hx.config import BashSettings, ContextSettings, ModelSettings, PermissionSettings, Settings
from hx.tui.app import HXApp
from hx.tui.commands import build_default_commands

README = Path(__file__).resolve().parent.parent / "README.md"


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text()


def _table_cells(readme: str, pattern: str) -> set[str]:
    return set(re.findall(pattern, readme, re.M))


def test_every_documented_command_exists(readme: str) -> None:
    documented = _table_cells(readme, r"^\| `/(\w+)")
    registered = {command.name for command in build_default_commands().all()}
    assert documented - registered == set(), "README documents commands that do not exist"


def test_every_command_is_documented(readme: str) -> None:
    documented = _table_cells(readme, r"^\| `/(\w+)")
    registered = {command.name for command in build_default_commands().all()}
    assert registered - documented == set(), "these commands are missing from the README"


#: Keys the prompt handles itself rather than through App.BINDINGS.
WIDGET_KEYS = {"enter", "ctrl+j"}


def _app_bindings() -> set[str]:
    keys = set()
    for binding in HXApp.BINDINGS:
        key = binding[0] if isinstance(binding, tuple) else binding.key
        keys.add("esc" if key == "escape" else key)
    return keys


def test_every_documented_keybinding_exists(readme: str) -> None:
    documented = _table_cells(readme, r"^\| `(ctrl\+\w+|shift\+tab|enter|esc)`")
    unknown = documented - _app_bindings() - WIDGET_KEYS
    assert unknown == set(), f"README documents keys nothing handles: {unknown}"


def test_widget_level_keys_are_really_handled(readme: str) -> None:
    """They are absent from App.BINDINGS, so only the widget can vouch for them."""
    source = (README.parent / "src" / "hx" / "tui" / "widgets" / "input.py").read_text()
    for key in WIDGET_KEYS:
        assert f'"{key}"' in source, f"{key} is documented but the prompt does not handle it"


def test_every_binding_is_documented(readme: str) -> None:
    documented = _table_cells(readme, r"^\| `(ctrl\+\w+|shift\+tab|enter|esc)`")
    missing = _app_bindings() - documented
    assert missing == set(), f"bound but undocumented: {missing}"


def _documented_settings(readme: str) -> dict:
    """Parse the jsonc block under Configuration, minus its comments."""
    block = re.search(r"```jsonc\n(.*?)```", readme, re.S)
    assert block is not None, "the Configuration section lost its example"
    stripped = re.sub(r"//.*", "", block.group(1))
    return json.loads(stripped)


def test_documented_settings_all_exist(readme: str) -> None:
    documented = _documented_settings(readme)
    groups = {
        "models": ModelSettings,
        "permissions": PermissionSettings,
        "context": ContextSettings,
        "bash": BashSettings,
    }
    top_level = {f.name for f in dataclasses.fields(Settings)}

    for key, value in documented.items():
        assert key in top_level, f"README documents unknown setting {key!r}"
        if key in groups:
            real = {f.name for f in dataclasses.fields(groups[key])}
            unknown = set(value) - real
            assert not unknown, f"README documents unknown {key} keys: {unknown}"


def test_every_setting_is_documented(readme: str) -> None:
    documented = _documented_settings(readme)
    groups = {
        "models": ModelSettings,
        "permissions": PermissionSettings,
        "context": ContextSettings,
        "bash": BashSettings,
    }
    for name, cls in groups.items():
        real = {f.name for f in dataclasses.fields(cls)}
        missing = real - set(documented.get(name, {}))
        assert not missing, f"{name} settings missing from the README: {missing}"


def test_documented_defaults_match_the_code(readme: str) -> None:
    """A default that has drifted is worse than none - it will be trusted."""
    documented = _documented_settings(readme)
    groups = {
        "models": ModelSettings,
        "permissions": PermissionSettings,
        "context": ContextSettings,
        "bash": BashSettings,
    }
    for name, cls in groups.items():
        for field in dataclasses.fields(cls):
            if field.default is dataclasses.MISSING or field.name not in documented[name]:
                continue
            actual = field.default
            actual = actual.value if hasattr(actual, "value") else actual
            shown = documented[name][field.name]
            if isinstance(actual, tuple):
                continue  # rule lists are illustrative, not defaults
            assert shown == actual, (
                f"{name}.{field.name}: README says {shown!r}, code says {actual!r}"
            )


def test_documented_env_vars_exist(readme: str) -> None:
    from hx.config import _ENV_MAP

    documented = set(re.findall(r"`(HX_[A-Z_]+)`", readme))
    known = set(_ENV_MAP) | {"HX_HOME", "HX_OPENROUTER_API_KEY"}
    assert documented - known == set(), "README documents env vars that do nothing"


def test_every_env_var_is_documented(readme: str) -> None:
    from hx.config import _ENV_MAP

    documented = set(re.findall(r"`(HX_[A-Z_]+)`", readme))
    assert set(_ENV_MAP) - documented == set()


def test_the_distribution_name_matches_pyproject() -> None:
    """install.sh, `hx upgrade` and the README must name the same package."""
    pyproject = (README.parent / "pyproject.toml").read_text()
    name = re.search(r'^name = "(.+?)"', pyproject, re.M).group(1)

    assert f"uv tool install {name}" in README.read_text()
    assert f"HX_PACKAGE:-{name}" in (README.parent / "install.sh").read_text()

    cli = (README.parent / "src" / "hx" / "cli.py").read_text()
    assert f'"upgrade", "{name}"' in cli


def test_documented_cli_flags_are_accepted(readme: str) -> None:
    from hx.cli import parse_args

    for flag in re.findall(r"^hx (--[a-z-]+)", readme, re.M):
        if flag in {"--version", "--help"}:
            continue
        value = {"--model": "m", "--mode": "plan", "--cwd": "."}.get(flag)
        parse_args([flag, value] if value else [flag])
