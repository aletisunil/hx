"""How wide is this text, in terminal cells.

Every other invariant in the library is enforced with :func:`cell_width`, so
where it disagrees with the terminal the whole layout shifts: a line measured
short leaves stale characters behind it, and a line measured long wraps and
cascades down the screen. It is worth more care than its size suggests.

Three things make a character's width not its length:

* **Escape sequences** occupy no cells. Text arrives here already styled, so
  the colour has to come off before anything is counted.
* **East Asian Wide and Fullwidth** characters occupy two cells, and combining
  marks occupy none. :mod:`wcwidth` knows both from the Unicode tables.
* **Grapheme clusters** - a base character plus the marks, joiners, variation
  selectors and modifiers that combine with it - are drawn as a single glyph
  in one or two cells, whatever their code points sum to. ``wcwidth`` does not
  cluster, so that part is here.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from functools import lru_cache

from wcwidth import wcwidth

TAB_WIDTH = 3
"""Cells a tab occupies. Terminals disagree and none of them ask; three is
narrow enough that a mismeasured tab cannot push a line over the width."""

_ANSI = re.compile(
    r"""
      \x1b\[ [0-?]* [ -/]* [@-~]          # CSI  - colours, cursor moves
    | \x1b\] .*? (?: \x07 | \x1b\\ )      # OSC  - hyperlinks, titles
    | \x1b_  .*? (?: \x07 | \x1b\\ )      # APC  - our own cursor marker
    | \x1bP  .*? (?: \x07 | \x1b\\ )      # DCS
    | \x1b [@-Z\\-_]                      # two-character escapes
    """,
    re.VERBOSE | re.DOTALL,
)

_ZWJ = "‍"
_VS15 = "︎"  # request the text presentation: narrow
_VS16 = "️"  # request the emoji presentation: wide
_KEYCAP = "⃣"

_PRINTABLE_ASCII = re.compile(r"^[\x20-\x7e]*$")


def strip_ansi(text: str) -> str:
    """The text without any escape sequence, which is what the user sees."""
    return _ANSI.sub("", text)


def _is_regional_indicator(char: str) -> bool:
    return "\U0001f1e6" <= char <= "\U0001f1ff"


def _is_skin_tone(char: str) -> bool:
    return "\U0001f3fb" <= char <= "\U0001f3ff"


def _extends(char: str) -> bool:
    """Does this code point attach to the cluster before it?"""
    if char in (_ZWJ, _VS15, _VS16, _KEYCAP):
        return True
    if _is_skin_tone(char):
        return True
    # Mn/Me are always combining; Mc is a spacing mark, which some scripts
    # (Devanagari, Thai) use as part of the same glyph.
    return unicodedata.category(char) in ("Mn", "Me", "Mc")


def grapheme_clusters(text: str) -> Iterator[str]:
    """Split into what the terminal draws as one glyph each.

    Deliberately not a full UAX #29 implementation. It covers the cases that
    reach a coding agent's transcript - combining marks, emoji with modifiers
    and variation selectors, ZWJ sequences, flags, keycaps - and treats
    anything else as its own cluster, which is what UAX #29 would do anyway.
    """
    index = 0
    length = len(text)
    while index < length:
        start = index
        char = text[index]
        index += 1

        # A flag is exactly two regional indicators; a third starts a new one.
        if _is_regional_indicator(char):
            if index < length and _is_regional_indicator(text[index]):
                index += 1
            yield text[start:index]
            continue

        while index < length:
            following = text[index]
            if _extends(following):
                index += 1
                # A joiner is only a joiner if something follows it to join to.
                if following == _ZWJ and index < length:
                    index += 1
                continue
            break

        yield text[start:index]


def _cluster_width(cluster: str) -> int:
    """Cells one glyph occupies."""
    if _VS16 in cluster or _KEYCAP in cluster:
        # The emoji presentation is always wide, even where the base character
        # alone is narrow (⚠ and ⚠️ are different widths).
        return 2
    if len(cluster) >= 2 and _is_regional_indicator(cluster[0]):
        return 2

    base = wcwidth(cluster[0])
    if base < 0:
        # A control character the terminal will not print. Counting it as zero
        # is the safe direction: measuring short only under-fills a line, while
        # measuring long wraps it.
        return 0
    if _VS15 in cluster:
        return 1
    return base


@lru_cache(maxsize=8192)
def _measure(text: str) -> int:
    return sum(_cluster_width(cluster) for cluster in grapheme_clusters(text))


def cell_width(text: str) -> int:
    """Columns ``text`` occupies once drawn, ignoring any styling in it.

    Called on every line of every frame, so the common case - printable ASCII,
    already the overwhelming majority of a transcript - never reaches the
    Unicode tables at all.
    """
    if _PRINTABLE_ASCII.match(text):
        return len(text)

    visible = strip_ansi(text)
    if _PRINTABLE_ASCII.match(visible):
        return len(visible)
    if "\t" in visible:
        visible = visible.replace("\t", " " * TAB_WIDTH)
    return _measure(visible)


def truncate_to_width(text: str, width: int) -> str:
    """The longest prefix of ``text`` that fits, cutting between glyphs.

    Styling is preserved and carried along, so a truncated line keeps the
    colours of the part that survived. A wide glyph that would straddle the
    edge is dropped rather than half-drawn.
    """
    if width <= 0:
        return ""
    if _PRINTABLE_ASCII.match(text):
        return text[:width]

    out: list[str] = []
    used = 0
    index = 0
    length = len(text)
    while index < length:
        escape = _ANSI.match(text, index)
        if escape:
            out.append(escape.group())
            index = escape.end()
            continue
        # Measure one glyph at a time so a cluster is never split.
        remainder = text[index:]
        cluster = next(grapheme_clusters(remainder), remainder[:1])
        step = _cluster_width(cluster)
        if used + step > width:
            break
        out.append(cluster)
        used += step
        index += len(cluster)
    return "".join(out)


def pad_to_width(text: str, width: int) -> str:
    """``text`` padded with spaces to exactly ``width`` cells.

    Over-wide text is truncated rather than allowed through: a line wider than
    the terminal wraps, and one wrapped line pushes every line below it down
    by one forever.
    """
    used = cell_width(text)
    if used > width:
        text = truncate_to_width(text, width)
        used = cell_width(text)
    return text + " " * (width - used)
