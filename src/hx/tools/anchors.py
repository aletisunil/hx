"""Content-hash line anchors.

``Read`` labels every line with a short hash and ``Edit`` accepts those hashes
in place of retyped line content. The model points at a span instead of copying
it, which is both cheaper and safer: an anchor that no longer resolves means the
file moved under the model, so the patch is rejected rather than applied to
whatever now sits at those coordinates.

The hash covers a three-line window - the line and its two neighbours - not the
line alone. Hashing the line alone makes every blank line in a file share one
anchor, and a file with two blank lines would have no usable anchors at all. A
window is unique for almost every line in practice, and only an edit within one
line of an anchor disturbs it.
"""

from __future__ import annotations

import hashlib

WIDTHS = (4, 6, 8)
"""Anchor widths tried in order. The narrowest that adds no collision wins."""


SEPARATOR = "\x00"
"""Marks which of the three lines is the anchored one.

Joining the window on a newline instead would make line 1 and line 2 of a
two-line file hash identically - both windows are the whole file. The separator
records each line's role, so a missing neighbour is an empty field rather than
a shorter window."""


def split(content: str) -> tuple[list[str], bool]:
    """Split into lines on ``\n`` alone, and say whether the file ended with one.

    ``str.splitlines`` also splits on ``\r``, form feed and the Unicode line
    separators, so rejoining its output on ``\n`` silently rewrites every one of
    them - a one-line edit to a CRLF file would rewrite the whole file. Both
    ``Read`` (which labels lines) and ``Edit`` (which reassembles them) split
    here, so an anchor indexes the same line on both sides.
    """
    lines = content.split("\n")
    trailing = bool(lines) and lines[-1] == ""
    if trailing:
        lines.pop()
    return lines, trailing


def window(lines: list[str], index: int) -> str:
    """The hashed neighbourhood of ``lines[index]``: the line and its neighbours.

    Out-of-range neighbours are empty fields, so the first and last lines of a
    file still hash over three positions.
    """
    previous = lines[index - 1] if index > 0 else ""
    following = lines[index + 1] if index + 1 < len(lines) else ""
    return SEPARATOR.join((previous, lines[index], following))


def digests(lines: list[str]) -> list[str]:
    return [hashlib.sha1(window(lines, i).encode("utf-8")).hexdigest() for i in range(len(lines))]


def anchor_width(full: list[str]) -> int:
    """Narrowest width in :data:`WIDTHS` that introduces no *new* collision.

    Truncation is only allowed to merge anchors that were already identical at
    full length - two genuinely identical windows stay ambiguous at every width,
    and widening cannot fix that.
    """
    distinct = len(set(full))
    for width in WIDTHS:
        if len({value[:width] for value in full}) == distinct:
            return width
    return len(full[0]) if full else WIDTHS[0]


def compute(lines: list[str]) -> tuple[list[str], int]:
    """Anchors for every line, and the width they were rendered at."""
    if not lines:
        return [], WIDTHS[0]
    full = digests(lines)
    width = anchor_width(full)
    return [value[:width] for value in full], width


def resolve(lines: list[str], anchor: str) -> int:
    """The zero-based index the anchor points at.

    Raises:
        AnchorError: when the anchor matches no line, or more than one.
    """
    cleaned = anchor.strip().lstrip("#")
    if not cleaned:
        raise AnchorError("empty anchor")

    full = digests(lines)
    width = len(cleaned)
    matches = [i for i, value in enumerate(full) if value[:width] == cleaned]

    if not matches:
        raise AnchorError(
            f"anchor {cleaned!r} does not match any line - the file changed since it "
            "was read. Read it again to get current anchors."
        )
    if len(matches) > 1:
        where = ", ".join(str(i + 1) for i in matches[:5])
        raise AnchorError(
            f"anchor {cleaned!r} is ambiguous: it matches lines {where}. "
            "Use old_string for this edit, or span a wider range."
        )
    return matches[0]


def replace_span(lines: list[str], start: int, end: int, replacement: str) -> list[str]:
    """Replace ``lines[start:end + 1]`` with ``replacement`` split into lines.

    An empty replacement deletes the span rather than inserting a blank line.
    """
    new_lines = replacement.split("\n") if replacement else []
    return lines[:start] + new_lines + lines[end + 1 :]


class AnchorError(Exception):
    """An anchor that does not resolve to exactly one line."""
