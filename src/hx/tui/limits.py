"""How much of a thing is shown before it is folded away.

These used to be six unrelated constants in five files - a tool preview was
six lines, a bash preview eight, an edit diff sixteen, an approval's detail
twenty-four - so how much you saw depended on which code path you arrived
through, for no reason anybody could state.
"""

from __future__ import annotations

PREVIEW_LINES = 8
"""Lines shown before a block offers to expand.

One number for tool output, bash output, diffs and approval details alike. A
renderer that genuinely needs more room overrides ``preview_lines`` on its
:class:`~hx.tui.renderers.ToolRenderer`, which is a declared exception rather
than a magic number at a call site.
"""

EXPANDED_MAX = 400
"""Ceiling on an expanded block, so ctrl+o cannot stall the renderer."""

LIST_VISIBLE = 8
"""Rows visible in any scrolling list: completions, pickers, todos."""

RECORD_WIDTH = 72
"""Cells a one-line summary is collapsed to before being ellipsized."""
