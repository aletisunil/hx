"""Making text that came from somewhere else safe to draw.

Everything in :mod:`hx.term` trusts the escape sequences in the strings it is
handed: :func:`~hx.term.width.cell_width` measures around them,
:func:`~hx.term.ansi.wrap` carries them across a break, and
:func:`~hx.term.width.truncate_to_width` preserves them through a cut. That is
what lets a component colour its own output.

None of it is true of text HX did not write. A file read by a tool, a command's
stdout, a model's reply, a paste into the prompt - all of it reaches the screen,
and a terminal acts on what it is given:

* ``\\x1b]52;c;...\\x07`` writes the user's clipboard.
* ``\\x1b]0;...\\x07`` renames their window.
* ``\\x1b]8;;http://...\\x07`` makes the next words a link to somewhere else.
* ``\\x1b[2J``, ``\\x1b[H``, ``\\r`` and ``\\x08`` move or erase what is already
  on screen - and the renderer's diff is built on knowing exactly what is on
  screen, so a cursor move it did not emit desynchronises every frame after it.
* ``\\x1b_hx:c\\x07`` is :data:`~hx.term.screen.CURSOR_MARKER`. Text carrying
  one would move the hardware cursor into the middle of a tool result.

So untrusted text is plain text. Every escape and every control character comes
off, including the colour: a tool's stdout is a pipe, where ``ls``, ``git`` and
``pytest`` emit no colour anyway, and keeping SGR would mean a stray ``\\x1b[0m``
inside a tinted block punching a hole in it - exactly the failure
:mod:`hx.term.ansi` is written to avoid.

Newlines survive, because they are structure rather than styling. Tabs are
expanded here rather than passed on, so that the width of a line is the same
number whoever asks.
"""

from __future__ import annotations

import re

from hx.term.width import TAB_WIDTH

_ESCAPES = re.compile(
    r"""
      \x1b\] .*? (?: \x07 | \x1b\\ | \Z )   # OSC  - clipboard, title, links
    | \x1b_  .*? (?: \x07 | \x1b\\ | \Z )   # APC  - our own cursor marker
    | \x1bP  .*? (?: \x07 | \x1b\\ | \Z )   # DCS
    | \x1b\^ .*? (?: \x07 | \x1b\\ | \Z )   # PM
    | \x1bX  .*? (?: \x07 | \x1b\\ | \Z )   # SOS
    | \x1b\[ [0-?]* [ -/]* [@-~]            # CSI  - colour, cursor, erase
    | \x1b\[ [0-?]* [ -/]* \Z               # CSI still arriving, or cut short
    | \x1b .                                # two-character escapes
    | \x1b \Z                               # a lone trailing escape
    """,
    re.VERBOSE | re.DOTALL,
)
"""Every escape sequence, terminated or not.

An unterminated one is dropped to the end of the string rather than left in
place. Leaving it would let the next chunk of a streaming tool result supply
the terminator, so a payload split across two reads would reassemble itself
after both halves had been passed as harmless.
"""

#: Control characters with no business in a line of text. C0 minus newline and
#: tab, DEL, and the C1 range - where a terminal reads 0x9b as CSI and 0x9d as
#: OSC, which is the same attack again without the escape byte.
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def plain_text(text: str) -> str:
    """``text`` with every escape sequence and control character removed.

    The one function to reach for whenever a string arrives from a tool, a
    model, a file or the clipboard. Cheap on the common case - text with
    nothing to remove is returned unchanged, without a copy.
    """
    if not text:
        return text
    if "\x1b" in text:
        text = _ESCAPES.sub("", text)
    if "\t" in text:
        text = text.expandtabs(TAB_WIDTH)
    return _CONTROLS.sub("", text)


def plain_lines(text: str) -> list[str]:
    """``text`` sanitized and split into lines."""
    return plain_text(text).split("\n")


__all__ = ["plain_lines", "plain_text"]
