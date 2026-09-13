"""Laying out columns, measured rather than guessed.

There were eleven hard-coded column widths across the old UI - ``{name:<14}``,
``{model.id:<44}``, ``{keys:<16}`` and so on - each a guess about the longest
value it would ever see. A guess that is too small misaligns the column the
first time a longer value appears, and one that is too large wastes the width
forever. Measuring the data costs a pass and is always right.
"""

from __future__ import annotations

from collections.abc import Sequence

from hx.term.width import cell_width, truncate_to_width
from hx.tui.glyphs import ELLIPSIS


def columns(rows: Sequence[Sequence[str]], gap: int = 2) -> list[str]:
    """Pad each column to the widest cell in it.

    Cells may already be styled; widths are measured on the visible text, so
    colour never shifts a column. The last column is not padded - trailing
    spaces are invisible and would only push the line toward the width limit.
    """
    if not rows:
        return []

    count = max(len(row) for row in rows)
    widths = [
        max((cell_width(row[index]) for row in rows if index < len(row)), default=0)
        for index in range(count)
    ]

    out = []
    for row in rows:
        parts = []
        for index, cell in enumerate(row):
            if index == len(row) - 1:
                parts.append(cell)
            else:
                parts.append(cell + " " * (widths[index] - cell_width(cell)))
        out.append((" " * gap).join(parts))
    return out


def meter_fill(fraction: float, cells: int) -> int:
    """How many cells of a ``cells``-wide gauge are filled at ``fraction``.

    Clamped at both ends: a fraction over 1.0 is a bad window figure, not a
    licence to overdraw. Any non-zero fraction lights at least one cell, so a
    session that has spent tokens never draws an empty bar.
    """
    fraction = min(max(fraction, 0.0), 1.0)
    filled = round(fraction * cells)
    return max(1, filled) if fraction > 0.0 else filled


def one_line(text: str, width: int) -> str:
    """Collapse to a single line, ellipsized to fit.

    For anywhere a multi-line value has to be summarised in a row - a queued
    prompt, a session title, the target of an approval.
    """
    flat = " ".join(text.split())
    if cell_width(flat) <= width:
        return flat
    return truncate_to_width(flat, max(0, width - cell_width(ELLIPSIS))) + ELLIPSIS
