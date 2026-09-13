"""Painting theme roles onto text, for the scrollback-native frontend.

:mod:`hx.tui.theme` hands out Rich style strings, which is what the Textual app
needs and is meaningless to a renderer that emits escape sequences itself. Both
read the same :class:`~hx.tui.roles.Palette`, so a colour is still defined once
and ``/theme`` still moves the whole app; only the output form differs.

Everything goes through a role. There are no hex literals in the views, which
is what makes a user's theme file actually reach the screen - the drift that
matters is not a wrong colour, it is a colour that was never asked for.
"""

from __future__ import annotations

from collections.abc import Callable

from hx.term import ansi
from hx.term.markdown import Painter
from hx.tui.theme import THEME

_mode: ansi.ColorMode | None = None


def color_mode() -> ansi.ColorMode:
    """Truecolour or 256, detected once."""
    global _mode
    if _mode is None:
        _mode = ansi.detect_color_mode()
    return _mode


def set_color_mode(mode: ansi.ColorMode | None) -> None:
    """Override the detected depth. ``None`` re-detects.

    Tests pin this so an assertion about colour does not depend on the
    terminal the suite happens to be running under.
    """
    global _mode
    _mode = mode


def color(role: str) -> str:
    """The raw colour for a role, as the palette spells it."""
    return THEME.color(role)


def fg(
    role: str,
    text: str,
    *,
    bold: bool = False,
    italic: bool = False,
    underline: bool = False,
) -> str:
    """``text`` painted in ``role``, resetting only the foreground."""
    return ansi.fg(
        THEME.color(role), text, color_mode(), bold=bold, italic=italic, underline=underline
    )


def bg(role: str, text: str) -> str:
    """``text`` on ``role``, resetting only the background."""
    return ansi.bg(THEME.color(role), text, color_mode())


def tint(role: str) -> Callable[[str], str]:
    """A background filler, for :class:`~hx.term.primitives.Box`.

    Backgrounds mean exactly two things in this UI - whose turn this is, and
    how a tool call ended - so there are few of these and they are never used
    for emphasis.
    """

    def paint(line: str) -> str:
        return ansi.bg(THEME.color(role), line, color_mode())

    return paint


def rule(role: str = "border") -> Callable[[str], str]:
    """A colour for :class:`~hx.term.primitives.Rule`."""

    def paint(line: str) -> str:
        return ansi.fg(THEME.color(role), line, color_mode())

    return paint


def link(url: str, label: str, role: str = "md_link") -> str:
    """A hyperlinked label. Terminals without OSC 8 just show the label."""
    return ansi.hyperlink(url, fg(role, label))


class ThemePainter(Painter):
    """Bridges the markdown renderer to HX's palette and syntax highlighting.

    :mod:`hx.term.markdown` deliberately knows nothing about HX's roles, so
    that the library stays independent of the app. This is the one place the
    two are joined, and it is why ``mdCode``, ``mdQuote``, ``mdHr`` and the
    rest now do something when a user sets them.
    """

    def paint(self, role: str, text: str, **kwargs: bool) -> str:
        return fg(role, text, **kwargs)

    def code(self, source: str, language: str | None) -> list[str]:
        from hx.term.syntax import highlight

        return highlight(source, language, mode=color_mode())
