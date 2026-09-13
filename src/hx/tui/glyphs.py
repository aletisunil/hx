"""Every glyph the UI draws, defined once.

Before this, the same character meant different things in different widgets:
``○`` was both a running tool and a pending todo, ``●`` was both a finished
tool and the currently selected model, and ``✓`` meant four separate things.
A reader cannot learn a vocabulary where the words move, so each symbol here
means one thing and nothing outside this module spells one out.
"""

from __future__ import annotations

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
"""Frames for anything in progress. Advances every 80ms."""

TOOL_DONE = "●"
"""A tool call finished. Only that."""

TOOL_FAILED = "✗"
"""A tool call failed, or a notice reports an error."""

TODO_PENDING = "○"
"""A todo not started. Only that - a running tool uses the spinner."""

TODO_ACTIVE = "▸"
TODO_DONE = "✓"

CURSOR = "→ "
"""Where Enter will land, in any list."""

CURRENT = "✓ "
"""The value already in force. Sits beside :data:`CURSOR`, never instead of it.

Two markers, two questions: where the keyboard is, and what is already set.
Collapsing them into one is why picker rows used to signal a highlight three
different ways at once.
"""

GUTTER = "  "
"""An unselected row, occupying exactly what a marker would."""

NOTICE = {
    "info": "· ",
    "warning": "! ",
    "error": "✗ ",
    "success": "✓ ",
}
"""Bullets for a notice, by level."""

BASH = "$ "
"""A shell command, wherever one is shown - including in an approval, which
used to show the bare command while the tool block showed it with the prompt."""

METER_FULL = "▰"
METER_EMPTY = "▱"
"""One cell of a gauge, filled and unfilled. Only the context meter draws them,
and it is the only field down there with a quantity worth seeing at a glance
rather than reading."""

RULE = "─"
QUOTE_RAIL = "│ "
ELLIPSIS = "…"
SEPARATOR = " · "
"""Joins fields on one line: status bar, hints, record lines."""
