"""Unified diff rendering for edit blocks and permission prompts."""

from __future__ import annotations

from rich.console import RenderableType
from rich.text import Text

MAX_DIFF_LINES = 200
"""A diff longer than this is summarised. Scrolling past 4000 lines to find the
Approve button is not review, it is fatigue."""


def render_diff(diff_text: str, theme_name: str = "dark") -> RenderableType:
    """Colourise a unified diff."""
    body = Text(no_wrap=False)
    lines = diff_text.splitlines()

    shown = lines[:MAX_DIFF_LINES]
    for line in shown:
        if line.startswith(("+++", "---")):
            body.append(line + "\n", style="bold")
        elif line.startswith("@@"):
            body.append(line + "\n", style="cyan")
        elif line.startswith("+"):
            body.append(line + "\n", style="green")
        elif line.startswith("-"):
            body.append(line + "\n", style="red")
        else:
            body.append(line + "\n", style="dim")

    if len(lines) > MAX_DIFF_LINES:
        body.append(f"\n… {len(lines) - MAX_DIFF_LINES} more diff lines\n", style="bold yellow")
    return body


def render_edit_summary(path: str, additions: int, deletions: int) -> RenderableType:
    """One-line ``path +3 -1`` summary for a collapsed edit block."""
    return Text.assemble(
        (path, "bold"),
        (f" +{additions}", "green"),
        (f" -{deletions}", "red"),
    )


def count_changes(diff_text: str) -> tuple[int, int]:
    """Added and removed line counts, ignoring the ``+++``/``---`` file headers."""
    added = sum(1 for line in diff_text.splitlines() if line.startswith("+") and line[1:2] != "+")
    removed = sum(1 for line in diff_text.splitlines() if line.startswith("-") and line[1:2] != "-")
    return added, removed
