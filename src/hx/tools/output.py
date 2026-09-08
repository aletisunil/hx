"""Tool-output capping.

Large outputs are the fastest way to burn a context window. Anything over the
cap keeps its head and tail, elides the middle with an explicit marker, and
spills the full text to
``~/.hx/sessions/<id>/outputs/<tool_use_id>.txt`` - whose path is handed to the
model so it can grep or read the rest deliberately.

The TUI always renders the uncapped stream; capping is a context concern only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ELISION_TEMPLATE = (
    "\n\n... [{omitted_lines} lines / {omitted_chars} chars elided] ...\n"
    "Full output: {path}\n"
    "Use Read or Grep on that path to inspect the rest.\n\n"
)


@dataclass(slots=True)
class CappedOutput:
    text: str
    truncated: bool
    spilled_path: str | None
    original_chars: int
    original_lines: int


def cap_output(
    text: str,
    *,
    session_id: str,
    tool_use_id: str,
    char_cap: int = 25_000,
    line_cap: int = 2_000,
    head_ratio: float = 0.6,
) -> CappedOutput:
    """Apply char and line caps, spilling to disk when either is exceeded.

    ``head_ratio`` favours the head, where errors and headers usually live,
    while keeping the tail that holds exit status and final failures.
    """
    raise NotImplementedError


def spill(text: str, session_id: str, tool_use_id: str) -> Path:
    """Write the full output and return its path."""
    raise NotImplementedError


def summarize_for_ui(text: str, max_len: int = 120) -> str:
    """One-line summary for a collapsed tool block in the transcript."""
    raise NotImplementedError
