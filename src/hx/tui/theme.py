"""Colour tokens and Rich/Textual styling.

Defined as semantic tokens rather than raw colours so light and dark themes
stay consistent and the status bar reads legibly on both.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Theme:
    name: str
    background: str
    surface: str
    text: str
    text_muted: str
    accent: str
    success: str
    warning: str
    error: str
    diff_add: str
    diff_remove: str
    cache_hit: str
    """Status-bar colour when the cache hit rate is healthy."""


DARK = Theme(
    name="dark",
    background="#0d1117",
    surface="#161b22",
    text="#e6edf3",
    text_muted="#8b949e",
    accent="#58a6ff",
    success="#3fb950",
    warning="#d29922",
    error="#f85149",
    diff_add="#2ea043",
    diff_remove="#f85149",
    cache_hit="#3fb950",
)

LIGHT = Theme(
    name="light",
    background="#ffffff",
    surface="#f6f8fa",
    text="#1f2328",
    text_muted="#59636e",
    accent="#0969da",
    success="#1a7f37",
    warning="#9a6700",
    error="#cf222e",
    diff_add="#1a7f37",
    diff_remove="#cf222e",
    cache_hit="#1a7f37",
)

THEMES = {"dark": DARK, "light": LIGHT}


def get_theme(name: str) -> Theme:
    """Look up a theme by name, falling back to dark."""
    return THEMES.get(name, DARK)
