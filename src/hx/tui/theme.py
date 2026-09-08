"""Theme: one palette of named roles, shared by the CSS and by Rich renderables.

Widget chrome reaches colour through Textual's design tokens; the content a
widget draws with Rich - a tool header, a diff line, a footer field - cannot.
Both are fed from the same :class:`Palette` here, so a colour is defined once
and a theme switch moves the whole app rather than half of it.

Roles are named for what they mean, never for what they look like: ``error``
rather than ``red``. That is what lets the light palette invert lightness
without every call site having to care.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

DEFAULT_THEME = "dark"


@dataclass(frozen=True, slots=True)
class Palette:
    """Colour per role. Values are hex, except in the ANSI palette."""

    name: str
    dark: bool

    # Surfaces.
    background: str
    surface: str
    panel: str
    user_bg: str
    tool_pending_bg: str
    tool_success_bg: str
    tool_error_bg: str
    selected_bg: str

    # Type.
    text: str
    muted: str
    dim: str
    accent: str
    border: str
    border_accent: str
    border_muted: str

    # State.
    success: str
    error: str
    warning: str
    thinking: str

    # Tool blocks.
    tool_title: str
    tool_output: str

    # Markdown.
    md_heading: str
    md_link: str
    md_code: str
    md_code_block: str
    md_quote: str
    md_hr: str
    md_bullet: str

    # Diffs.
    diff_added: str
    diff_removed: str
    diff_context: str
    diff_hunk: str


#: Ported from pi's dark theme so the two agents read the same way side by side.
DARK = Palette(
    name="dark",
    dark=True,
    background="#18181e",
    surface="#1e1e24",
    panel="#26262e",
    user_bg="#343541",
    tool_pending_bg="#282832",
    tool_success_bg="#283228",
    tool_error_bg="#3c2828",
    selected_bg="#3a3a4a",
    text="#d4d4d4",
    muted="#808080",
    dim="#666666",
    accent="#8abeb7",
    border="#5f87ff",
    border_accent="#00d7ff",
    border_muted="#505050",
    success="#b5bd68",
    error="#cc6666",
    warning="#f0c674",
    thinking="#808080",
    tool_title="#d4d4d4",
    tool_output="#808080",
    md_heading="#f0c674",
    md_link="#81a2be",
    md_code="#8abeb7",
    md_code_block="#b5bd68",
    md_quote="#808080",
    md_hr="#808080",
    md_bullet="#8abeb7",
    diff_added="#b5bd68",
    diff_removed="#cc6666",
    diff_context="#808080",
    diff_hunk="#81a2be",
)

LIGHT = Palette(
    name="light",
    dark=False,
    background="#fafafa",
    surface="#ffffff",
    panel="#ececed",
    user_bg="#e4e6f0",
    tool_pending_bg="#eeeef2",
    tool_success_bg="#e6f0e2",
    tool_error_bg="#f7e4e4",
    selected_bg="#d8d8e4",
    text="#2e2e32",
    muted="#6a6a70",
    dim="#8c8c92",
    accent="#2a7f78",
    border="#3a63c8",
    border_accent="#0071a4",
    border_muted="#c4c4cc",
    success="#4a7a2a",
    error="#b03030",
    warning="#9a6b00",
    thinking="#6a6a70",
    tool_title="#2e2e32",
    tool_output="#6a6a70",
    md_heading="#9a6b00",
    md_link="#2a5db0",
    md_code="#2a7f78",
    md_code_block="#4a7a2a",
    md_quote="#6a6a70",
    md_hr="#8c8c92",
    md_bullet="#2a7f78",
    diff_added="#4a7a2a",
    diff_removed="#b03030",
    diff_context="#6a6a70",
    diff_hunk="#2a5db0",
)

#: Resolves against whatever the terminal is configured with. The only palette
#: that stays legible on a background HX cannot see.
ANSI = Palette(
    name="ansi",
    dark=True,
    background="ansi_default",
    surface="ansi_default",
    panel="ansi_default",
    user_bg="ansi_default",
    tool_pending_bg="ansi_default",
    tool_success_bg="ansi_default",
    tool_error_bg="ansi_default",
    selected_bg="ansi_blue",
    text="ansi_default",
    muted="ansi_bright_black",
    dim="ansi_bright_black",
    accent="ansi_cyan",
    border="ansi_blue",
    border_accent="ansi_bright_cyan",
    border_muted="ansi_bright_black",
    success="ansi_green",
    error="ansi_red",
    warning="ansi_yellow",
    thinking="ansi_bright_black",
    tool_title="ansi_default",
    tool_output="ansi_bright_black",
    md_heading="ansi_yellow",
    md_link="ansi_blue",
    md_code="ansi_cyan",
    md_code_block="ansi_green",
    md_quote="ansi_bright_black",
    md_hr="ansi_bright_black",
    md_bullet="ansi_cyan",
    diff_added="ansi_green",
    diff_removed="ansi_red",
    diff_context="ansi_bright_black",
    diff_hunk="ansi_blue",
)

PALETTES: dict[str, Palette] = {p.name: p for p in (DARK, LIGHT, ANSI)}
_ROLES = frozenset(f.name for f in fields(Palette)) - {"name", "dark"}


class Theme:
    """The active palette, plus helpers for building Rich style strings.

    A module-level singleton rather than a value passed down the widget tree:
    every renderable in the app wants it, and threading it through forty
    constructors would buy nothing but noise.
    """

    def __init__(self, palette: Palette = DARK) -> None:
        self.palette = palette

    def use(self, name: str) -> Palette:
        """Switch palettes. Unknown names fall back to dark."""
        self.palette = PALETTES.get(name, PALETTES[DEFAULT_THEME])
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


class Styles:
    """Named Rich styles, resolved against the active palette on every read.

    Widgets hold no colour of their own, so ``/theme light`` repaints the app
    without any of them being told.
    """

    # Type.
    text = property(lambda self: THEME.fg("text"))
    muted = property(lambda self: THEME.fg("muted"))
    dim = property(lambda self: THEME.fg("dim"))
    accent = property(lambda self: THEME.fg("accent"))
    emphasis = property(lambda self: THEME.fg("text", bold=True))
    model = property(lambda self: THEME.fg("accent", bold=True))
    user = property(lambda self: THEME.fg("text"))
    thinking = property(lambda self: THEME.fg("thinking", italic=True))

    # State.
    ok = property(lambda self: THEME.fg("success"))
    done = property(lambda self: THEME.fg("success"))
    warning = property(lambda self: THEME.fg("warning"))
    error = property(lambda self: THEME.fg("error", bold=True))
    running = property(lambda self: THEME.fg("accent"))
    pending = property(lambda self: THEME.fg("dim"))
    completed_text = property(lambda self: f"strike {THEME.fg('dim')}")

    # Tool blocks.
    tool_title = property(lambda self: THEME.fg("tool_title", bold=True))
    tool_output = property(lambda self: THEME.fg("tool_output"))

    # Diffs.
    diff_add = property(lambda self: THEME.fg("diff_added"))
    diff_remove = property(lambda self: THEME.fg("diff_removed"))
    diff_context = property(lambda self: THEME.fg("diff_context"))
    diff_hunk = property(lambda self: THEME.fg("diff_hunk"))
    diff_header = property(lambda self: THEME.fg("text", bold=True))

    # Footer.
    gauge_ok = property(lambda self: THEME.fg("success"))
    gauge_warn = property(lambda self: THEME.fg("warning"))
    gauge_danger = property(lambda self: THEME.fg("error", bold=True))
    cache_warm = property(lambda self: THEME.fg("success"))
    cache_cold = property(lambda self: THEME.fg("warning"))

    mode_plan = property(lambda self: THEME.fg("border"))
    mode_default = property(lambda self: THEME.fg("dim"))
    mode_accept_edits = property(lambda self: THEME.fg("warning"))
    mode_bypass = property(lambda self: THEME.fg("error", bold=True))


STYLES = Styles()


def textual_theme(name: str) -> Any:
    """A Textual theme built from the HX palette of the same name.

    Returned rather than registered so the caller decides when to install it;
    the app does that once, on mount.
    """
    from textual.theme import Theme as TextualTheme

    palette = PALETTES.get(name, PALETTES[DEFAULT_THEME])
    if palette is ANSI:
        return TextualTheme(name="hx-ansi", primary="ansi_blue", ansi=True, dark=True)

    return TextualTheme(
        name=f"hx-{palette.name}",
        primary=palette.accent,
        secondary=palette.border,
        accent=palette.border_accent,
        warning=palette.warning,
        error=palette.error,
        success=palette.success,
        foreground=palette.text,
        background=palette.background,
        surface=palette.surface,
        panel=palette.panel,
        dark=palette.dark,
        variables={
            "border-muted": palette.border_muted,
            "block-cursor-text-style": "none",
            "input-selection-background": f"{palette.selected_bg} 60%",
            "scrollbar": palette.panel,
            "scrollbar-hover": palette.border_muted,
            "scrollbar-active": palette.accent,
            "scrollbar-background": palette.background,
        },
    )
