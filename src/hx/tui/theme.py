"""The active theme: one palette of named roles.

Palettes are data (see :mod:`hx.tui.theme_json`); this module tracks which one
is live. :mod:`hx.tui.paint` turns a role into escape sequences.

Every colour in the UI comes from a role, and there are no hex literals in the
views. That is what makes a user's theme file actually reach the screen: the
drift that matters is not a wrong colour, it is a colour nobody asked for.
"""

from __future__ import annotations

from dataclasses import fields
from functools import lru_cache
from typing import Any

from hx.tui.roles import Palette

DEFAULT_THEME = "dark"

_ROLES = frozenset(f.name for f in fields(Palette)) - {"name", "dark"}


@lru_cache(maxsize=1)
def builtins() -> dict[str, Palette]:
    """Themes shipped with HX. Parsed once; the files cannot change under us."""
    from hx.tui.theme_json import builtin_palettes

    return builtin_palettes()


def palettes() -> dict[str, Palette]:
    """Every theme available right now, user themes shadowing built-ins.

    Re-reads ``~/.hx/themes`` on each call so a user can add a theme and pick
    it up with ``/theme`` without restarting.
    """
    from hx.tui.theme_json import user_palettes

    available = dict(builtins())
    available.update(user_palettes())
    return available


def default_palette() -> Palette:
    return builtins()[DEFAULT_THEME]


class Theme:
    """The active palette, plus helpers for building Rich style strings.

    A module-level singleton rather than a value passed down the widget tree:
    every renderable in the app wants it, and threading it through forty
    constructors would buy nothing but noise.
    """

    def __init__(self, palette: Palette | None = None) -> None:
        self.palette = palette or default_palette()

    def use(self, name: str) -> Palette:
        """Switch palettes. Unknown names fall back to dark."""
        self.palette = palettes().get(name, default_palette())
        return self.palette

    def color(self, role: str) -> str:
        """Raw colour for a role."""
        if role not in _ROLES:
            raise KeyError(f"unknown theme role {role!r}")
        return str(getattr(self.palette, role))

    def fg(self, role: str, *, bold: bool = False, italic: bool = False) -> str:
        """Rich style string painting the foreground with ``role``."""
        style = self.color(role)
        if bold:
            style = f"bold {style}"
        if italic:
            style = f"italic {style}"
        return style

    def on(self, fg_role: str, bg_role: str, *, bold: bool = False) -> str:
        """Rich style string for ``fg_role`` drawn on ``bg_role``."""
        style = f"{self.color(fg_role)} on {self.color(bg_role)}"
        return f"bold {style}" if bold else style

    def bg(self, role: str) -> str:
        """Rich style string that only sets a background."""
        return f"on {self.color(role)}"


THEME = Theme()

#: Roles that map onto Pygments token types, so highlighted code follows the
#: user's theme instead of snapping to one of two canned Pygments styles.
_SYNTAX_TOKENS = {
    "syntax_comment": ("Comment",),
    "syntax_keyword": ("Keyword", "Operator.Word"),
    "syntax_function": ("Name.Function", "Name.Decorator"),
    "syntax_variable": ("Name", "Name.Variable", "Name.Attribute"),
    "syntax_string": ("String",),
    "syntax_number": ("Number",),
    "syntax_type": ("Name.Class", "Name.Builtin", "Keyword.Type"),
    "syntax_operator": ("Operator",),
    "syntax_punctuation": ("Punctuation",),
}


def syntax_style(palette: Palette | None = None) -> Any:
    """A Pygments style class built from the palette's ``syntax_*`` roles.

    Rich takes either a style name or a class; handing it one built here is
    what keeps a code block in a user theme from arriving in VS Code's colours.
    """
    return _syntax_style(palette or THEME.palette)


@lru_cache(maxsize=8)
def _syntax_style(active: Palette) -> Any:
    from pygments.style import Style
    from pygments.token import string_to_tokentype

    styles: dict[Any, str] = {}
    for role, tokens in _SYNTAX_TOKENS.items():
        color = str(getattr(active, role))
        if color.startswith("ansi"):  # Pygments cannot express terminal ANSI slots.
            continue
        for token in tokens:
            styles[string_to_tokentype(token)] = color

    return type(
        "HXSyntaxStyle",
        (Style,),
        {
            "background_color": None if active.background.startswith("ansi") else active.background,
            "styles": styles,
        },
    )
