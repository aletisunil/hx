"""The set of colour roles a theme has to fill.

Roles are named for what they mean, never for what they look like: ``error``
rather than ``red``. That is what lets the light palette invert lightness
without every call site having to care.

Kept apart from :mod:`hx.tui.theme` so the JSON loader and the active-theme
singleton can both depend on the shape without depending on each other.
"""

from __future__ import annotations

from dataclasses import dataclass


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
    user_text: str
    custom_message_bg: str
    custom_message_text: str
    custom_message_label: str
    tool_pending_bg: str
    tool_success_bg: str
    tool_error_bg: str
    selected_bg: str
    search_match_bg: str
    search_match_text: str

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
    bash_mode: str

    # Chrome.
    scrollbar_track: str
    scrollbar_thumb: str

    # Tool blocks.
    tool_title: str
    tool_output: str

    # Markdown.
    md_heading: str
    md_link: str
    md_link_url: str
    md_code: str
    md_code_block: str
    md_code_block_border: str
    md_quote: str
    md_quote_border: str
    md_hr: str
    md_bullet: str

    # Diffs.
    diff_added: str
    diff_removed: str
    diff_context: str
    diff_hunk: str

    # Syntax highlighting.
    syntax_comment: str
    syntax_keyword: str
    syntax_function: str
    syntax_variable: str
    syntax_string: str
    syntax_number: str
    syntax_type: str
    syntax_operator: str
    syntax_punctuation: str
