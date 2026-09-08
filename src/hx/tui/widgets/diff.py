"""Unified diff rendering for edit blocks and permission prompts."""

from __future__ import annotations

from rich.console import RenderableType


def render_diff(diff_text: str, theme_name: str = "dark") -> RenderableType:
    """Colourise a unified diff with syntax highlighting on the payload lines."""
    raise NotImplementedError


def render_edit_summary(path: str, additions: int, deletions: int) -> RenderableType:
    """One-line ``path +3 -1`` summary for a collapsed edit block."""
    raise NotImplementedError
