"""Tool-output capping.

Large outputs are the fastest way to burn a context window. Anything over the
cap keeps its head and tail, elides the middle with an explicit marker, and
spills the full text to
``~/.hx/sessions/<id>/outputs/<tool_use_id>.txt`` - whose path is handed to the
model so it can grep or read the rest deliberately.

The TUI always renders the uncapped stream; capping is a context concern only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from hx.paths import session_outputs_dir

MIN_BODY_CHARS = 200
"""Floor on kept output. A cap so small the elision notice eats it whole would
leave the model nothing to act on."""

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
    lines = text.splitlines()
    original_chars = len(text)
    original_lines = len(lines)

    if original_chars <= char_cap and original_lines <= line_cap:
        return CappedOutput(text, False, None, original_chars, original_lines)

    path = spill(text, session_id, tool_use_id)
    # The elision notice counts against the cap: the caller asked for a bound on
    # what enters the context, not on the body alone.
    reserve = len(
        ELISION_TEMPLATE.format(
            omitted_lines=original_lines, omitted_chars=original_chars, path=path
        )
    )
    body_cap = max(char_cap - reserve, MIN_BODY_CHARS)
    head_lines, tail_lines = _split(lines, body_cap, line_cap, head_ratio)
    omitted_lines = original_lines - len(head_lines) - len(tail_lines)
    kept_chars = sum(len(line) + 1 for line in (*head_lines, *tail_lines))

    elision = ELISION_TEMPLATE.format(
        omitted_lines=omitted_lines,
        omitted_chars=original_chars - kept_chars,
        path=path,
    )
    body = "\n".join(head_lines) + elision + "\n".join(tail_lines)
    return CappedOutput(body, True, str(path), original_chars, original_lines)


def _split(
    lines: list[str],
    char_cap: int,
    line_cap: int,
    head_ratio: float,
) -> tuple[list[str], list[str]]:
    """Choose the head and tail slices that fit inside both caps."""
    budget_lines = min(line_cap, len(lines))
    head_target = max(1, int(budget_lines * head_ratio))
    tail_target = max(1, budget_lines - head_target)

    head: list[str] = []
    used = 0
    head_budget = int(char_cap * head_ratio)
    for line in lines[:head_target]:
        if used + len(line) + 1 > head_budget:
            break
        head.append(line)
        used += len(line) + 1

    tail: list[str] = []
    tail_budget = char_cap - used
    for line in reversed(lines[len(head) :][-tail_target:]):
        if sum(len(item) + 1 for item in tail) + len(line) + 1 > tail_budget:
            break
        tail.insert(0, line)

    return head, tail


def spill(text: str, session_id: str, tool_use_id: str) -> Path:
    """Write the full output and return its path."""
    directory = session_outputs_dir(session_id)
    directory.mkdir(parents=True, exist_ok=True)
    # Tool ids come from the model; keep them from escaping the outputs dir.
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", tool_use_id) or "output"
    path = directory / f"{safe}.txt"
    path.write_text(text, encoding="utf-8", errors="replace")
    return path


def summarize_for_ui(text: str, max_len: int = 120) -> str:
    """One-line summary for a collapsed tool block in the transcript."""
    stripped = text.strip()
    if not stripped:
        return "no output"
    first = stripped.splitlines()[0].strip()
    extra = len(stripped.splitlines()) - 1
    if len(first) > max_len:
        first = first[: max_len - 1] + "…"
    return f"{first} (+{extra} lines)" if extra > 0 else first
