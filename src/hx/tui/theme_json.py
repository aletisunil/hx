"""Themes as data.

A theme is a JSON file in pi's format: a ``vars`` block of raw colours and a
``colors`` block mapping semantic roles onto them. Splitting the two is what
lets a palette be retuned by editing five hex values instead of forty, and it
means a theme can be dropped into ``~/.hx/themes`` without touching Python.

Role names in the file are pi's camelCase, so one of its theme files loads here
unchanged; they are mapped onto the snake_case fields of :class:`Palette` on the
way in.
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from typing import Any

from hx.paths import user_themes_dir
from hx.tui.roles import Palette

#: JSON role name -> ``Palette`` field, for the roles whose names differ.
#: Everything else converts by the camelCase rule alone.
_ALIASES = {
    "thinkingText": "thinking",
    "userMessageBg": "user_bg",
    "userMessageText": "user_text",
    "mdListBullet": "md_bullet",
    "toolDiffAdded": "diff_added",
    "toolDiffRemoved": "diff_removed",
    "toolDiffContext": "diff_context",
    "toolDiffHunk": "diff_hunk",
}

#: Roles a foreign theme file may leave out, and where to find them instead.
#: pi keeps its page and card colours in ``export`` rather than ``colors``, so
#: this is what lets one of its files load without being edited first.
_EXPORT_FALLBACKS = {"background": "pageBg", "surface": "cardBg"}

#: Roles that borrow another role when absent. Only for roles pi has no concept
#: of; a role pi does define must be present or the file is rejected.
_DERIVED = {"panel": "surface", "diff_hunk": "md_link", "user_text": "text"}

_FIELDS = frozenset(f.name for f in fields(Palette)) - {"name", "dark"}


def _snake(name: str) -> str:
    if name in _ALIASES:
        return _ALIASES[name]
    out: list[str] = []
    for char in name:
        if char.isupper():
            out.append("_")
            out.append(char.lower())
        else:
            out.append(char)
    return "".join(out)


def _luminance(color: str) -> float:
    """Rough perceived lightness of a hex colour, 0 (black) to 1 (white)."""
    if not color.startswith("#") or len(color) != 7:
        return 0.0
    r, g, b = (int(color[i : i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def parse(data: dict[str, Any], *, name: str | None = None, source: str = "<memory>") -> Palette:
    """Build a :class:`Palette` from parsed theme JSON.

    Raises :class:`ValueError` with the offending role named, rather than
    letting a typo become a silently missing colour at render time.
    """
    variables = data.get("vars") or {}
    colors = data.get("colors") or {}
    export = data.get("export") or {}
    if not isinstance(variables, dict) or not isinstance(colors, dict):
        raise ValueError(f"{source}: 'vars' and 'colors' must be objects")

    def resolve(value: Any, role: str) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{source}: role {role!r} must be a string")
        # A value is a reference when it names a var, and a literal otherwise.
        return str(variables.get(value, value))

    resolved: dict[str, str] = {}
    for key, value in colors.items():
        field = _snake(key)
        if field not in _FIELDS:
            raise ValueError(f"{source}: unknown theme role {key!r}")
        resolved[field] = resolve(value, key)

    for field, export_key in _EXPORT_FALLBACKS.items():
        if field not in resolved and export_key in export:
            resolved[field] = resolve(export[export_key], export_key)

    for field, source_field in _DERIVED.items():
        if field not in resolved and source_field in resolved:
            resolved[field] = resolved[source_field]

    missing = sorted(_FIELDS - resolved.keys())
    if missing:
        raise ValueError(f"{source}: theme is missing roles: {', '.join(missing)}")

    theme_name = name or data.get("name")
    if not theme_name:
        raise ValueError(f"{source}: theme has no name")

    dark = data.get("dark")
    if dark is None:
        # pi's files do not carry the flag; infer it the way a reader would.
        dark = _luminance(resolved["background"]) < 0.5
    return Palette(name=str(theme_name), dark=bool(dark), **resolved)


def load_file(path: Path) -> Palette:
    """Read one theme file. The filename is the theme name."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: theme must be a JSON object")
    return parse(data, name=path.stem, source=str(path))


def builtin_palettes() -> dict[str, Palette]:
    """Themes shipped with HX."""
    directory = Path(__file__).parent / "themes"
    return {path.stem: load_file(path) for path in sorted(directory.glob("*.json"))}


def user_palettes() -> dict[str, Palette]:
    """Themes from ``~/.hx/themes``. A broken file is skipped, not fatal.

    A user theme that fails to parse should cost them that theme, not the
    ability to start HX; the reason is reported by :func:`user_palette_errors`.
    """
    palettes: dict[str, Palette] = {}
    directory = user_themes_dir()
    if not directory.is_dir():
        return palettes
    for path in sorted(directory.glob("*.json")):
        try:
            palettes[path.stem] = load_file(path)
        except (OSError, ValueError):
            continue
    return palettes


def user_palette_errors() -> list[str]:
    """Human-readable reasons any user theme file was skipped."""
    errors: list[str] = []
    directory = user_themes_dir()
    if not directory.is_dir():
        return errors
    for path in sorted(directory.glob("*.json")):
        try:
            load_file(path)
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
    return errors


def load_all() -> dict[str, Palette]:
    """Built-in themes, with user themes of the same name taking precedence."""
    palettes = builtin_palettes()
    palettes.update(user_palettes())
    return palettes
