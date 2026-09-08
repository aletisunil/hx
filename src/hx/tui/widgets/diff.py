"""Diff rendering for permission prompts.

The painter itself lives in :mod:`hx.tui.renderers`, next to the tool that
produces the diff. This module keeps the small helpers the permission modal
needs and re-exports the painter so there is exactly one of them.
"""

from __future__ import annotations

from rich.console import RenderableType
from rich.text import Text

from hx.tui.renderers import MAX_DIFF_LINES, count_changes, render_diff
from hx.tui.theme import THEME

__all__ = ["MAX_DIFF_LINES", "count_changes", "render_diff", "render_edit_summary"]


def render_edit_summary(path: str, additions: int, deletions: int) -> RenderableType:
    """One-line ``path +3 -1`` summary for a collapsed edit block."""
    return Text.assemble(
        (path, THEME.fg("text", bold=True)),
        (f" +{additions}", THEME.fg("diff_added")),
        (f" -{deletions}", THEME.fg("diff_removed")),
    )
