"""Turning colours into escape sequences, and keeping them from leaking.

Two rules here do most of the work, and both are easy to get wrong in ways that
only show up on somebody else's terminal.

**Reset only what you set.** :func:`fg` ends with ``\\x1b[39m`` and :func:`bg`
with ``\\x1b[49m``, never with ``\\x1b[0m``. A full reset inside a block with a
background punches a hole in it: the characters after it are drawn on the
terminal's background instead of the block's, and a tool call ends up with a
ragged bite taken out of its tint. Because each resets only its own channel,
a coloured span can be nested inside a filled block and compose correctly.

**Reset everything at the end of a line.** :data:`SEGMENT_RESET` closes any
style and any hyperlink. Terminals carry SGR state across a newline, so one
unterminated bold sequence would otherwise embolden the rest of the session.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from functools import lru_cache
from typing import Literal

from hx.term.width import cell_width, strip_ansi

ColorMode = Literal["truecolor", "256color"]

FG_RESET = "\x1b[39m"
"""Restore the default foreground, leaving any background alone."""

BG_RESET = "\x1b[49m"
"""Restore the default background, leaving any foreground alone."""

SEGMENT_RESET = "\x1b[0m\x1b]8;;\x07"
"""Ends every rendered line: all attributes off, and any hyperlink closed."""

#: The eight named slots plus their bright variants, as the theme spells them.
#: These defer to whatever the user's terminal palette says, which is the point
#: of the ``ansi`` theme - it has no opinion about colour at all.
_ANSI_SLOTS: dict[str, int | None] = {
    "ansi_default": None,
    "ansi_black": 0,
    "ansi_red": 1,
    "ansi_green": 2,
    "ansi_yellow": 3,
    "ansi_blue": 4,
    "ansi_magenta": 5,
    "ansi_cyan": 6,
    "ansi_white": 7,
    "ansi_bright_black": 8,
    "ansi_bright_red": 9,
    "ansi_bright_green": 10,
    "ansi_bright_yellow": 11,
    "ansi_bright_blue": 12,
    "ansi_bright_magenta": 13,
    "ansi_bright_cyan": 14,
    "ansi_bright_white": 15,
}

#: The 6x6x6 colour cube's axis values, and the 24-step grey ramp, as xterm
#: defines them. Used to map a hex colour onto the nearest 256-colour index.
_CUBE = (0, 95, 135, 175, 215, 255)
_GREY_START, _GREY_STEP, _GREY_COUNT = 8, 10, 24

_TRUECOLOR_TERMS = frozenset(
    {"kitty", "xterm-kitty", "ghostty", "wezterm", "iterm2", "alacritty", "contour"}
)


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    if len(value) == 3:
        value = "".join(char * 2 for char in value)
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """Perceptual-ish distance, weighted the way the eye weights the channels.

    A plain Euclidean distance in RGB picks green substitutes for grey far too
    readily. The luminance weights are the same ones used to convert to
    greyscale, and they are enough to keep a quantised palette recognisable.
    """
    dr, dg, db = a[0] - b[0], a[1] - b[1], a[2] - b[2]
    return 0.299 * dr * dr + 0.587 * dg * dg + 0.114 * db * db


@lru_cache(maxsize=1024)
def rgb_to_256(red: int, green: int, blue: int) -> int:
    """The 256-colour index closest to an RGB triple.

    Both the colour cube and the grey ramp are searched, but grey only wins for
    a colour that is already near-neutral. Without that guard a muted blue-grey
    - which is most of a well-designed terminal palette - snaps to true grey
    and the theme loses its tint.
    """
    target = (red, green, blue)

    best_index = 16
    best_distance = float("inf")
    for r_index, r_value in enumerate(_CUBE):
        for g_index, g_value in enumerate(_CUBE):
            for b_index, b_value in enumerate(_CUBE):
                candidate = _distance(target, (r_value, g_value, b_value))
                if candidate < best_distance:
                    best_distance = candidate
                    best_index = 16 + 36 * r_index + 6 * g_index + b_index

    spread = max(target) - min(target)
    if spread < 10:
        level = round((sum(target) / 3 - _GREY_START) / _GREY_STEP)
        level = max(0, min(_GREY_COUNT - 1, int(level)))
        grey = _GREY_START + level * _GREY_STEP
        if _distance(target, (grey, grey, grey)) < best_distance:
            return 232 + level

    return best_index


def detect_color_mode(env: dict[str, str] | None = None) -> ColorMode:
    """Whether this terminal can be handed 24-bit colour.

    ``COLORTERM`` is the standard signal, but plenty of terminals that support
    truecolour do not set it, so the well-known ones are recognised by name.
    ``HX_TRUE_COLOR`` forces the answer either way, which is the only thing
    that helps on a terminal nobody has heard of.
    """
    source: Mapping[str, str] = os.environ if env is None else env

    forced = source.get("HX_TRUE_COLOR", "").strip().lower()
    if forced in ("1", "true", "yes"):
        return "truecolor"
    if forced in ("0", "false", "no"):
        return "256color"

    if source.get("COLORTERM", "").strip().lower() in ("truecolor", "24bit"):
        return "truecolor"

    term_program = source.get("TERM_PROGRAM", "").strip().lower()
    term = source.get("TERM", "").strip().lower()
    if term_program in _TRUECOLOR_TERMS or term in _TRUECOLOR_TERMS:
        return "truecolor"
    return "256color"


def _sgr(color: str, mode: ColorMode, *, background: bool) -> str:
    """The escape that selects ``color``, or nothing if it means "leave it"."""
    if not color:
        return ""

    if color.startswith("ansi"):
        slot = _ANSI_SLOTS.get(color)
        if slot is None:
            return ""  # ansi_default: the terminal's own choice, already in force
        base = 40 if background else 30
        if slot < 8:
            return f"\x1b[{base + slot}m"
        return f"\x1b[{base + 60 + slot - 8}m"

    if not color.startswith("#"):
        return ""

    red, green, blue = hex_to_rgb(color)
    channel = 48 if background else 38
    if mode == "truecolor":
        return f"\x1b[{channel};2;{red};{green};{blue}m"
    return f"\x1b[{channel};5;{rgb_to_256(red, green, blue)}m"


def fg(
    color: str,
    text: str,
    mode: ColorMode = "truecolor",
    *,
    bold: bool = False,
    italic: bool = False,
    underline: bool = False,
) -> str:
    """``text`` in ``color``, resetting only the foreground afterwards."""
    if not text:
        return text
    start = _sgr(color, mode, background=False)
    end = FG_RESET if start else ""
    if bold:
        start, end = "\x1b[1m" + start, end + "\x1b[22m"
    if italic:
        start, end = "\x1b[3m" + start, end + "\x1b[23m"
    if underline:
        start, end = "\x1b[4m" + start, end + "\x1b[24m"
    return f"{start}{text}{end}"


def bg(color: str, text: str, mode: ColorMode = "truecolor") -> str:
    """``text`` on ``color``, resetting only the background afterwards."""
    start = _sgr(color, mode, background=True)
    return f"{start}{text}{BG_RESET}" if start else text


def inverse(text: str) -> str:
    """Swap foreground and background.

    Reserved, throughout the UI, for the text cursor and for the changed words
    inside a one-line diff. Anything else that needs to stand out is recoloured
    instead, so that inversion keeps meaning one thing.
    """
    return f"\x1b[7m{text}\x1b[27m"


def hyperlink(url: str, label: str) -> str:
    """An OSC 8 hyperlink. Terminals that do not support it just show ``label``."""
    return f"\x1b]8;;{url}\x07{label}\x1b]8;;\x07"


def terminate(line: str) -> str:
    """End a rendered line: styles off, hyperlink closed.

    Idempotent, so a component that already terminated its own line - and the
    renderer, which terminates everything it is handed - do not stack resets.
    """
    return line if line.endswith(SEGMENT_RESET) else line + SEGMENT_RESET


_ANY_ESCAPE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\].*?(?:\x07|\x1b\\)|\x1b_.*?(?:\x07|\x1b\\)",
    re.DOTALL,
)

_SGR_PATTERN = re.compile(r"\x1b\[([0-9;]*)m")


def active_background(text: str) -> str:
    """The background escape still in force at the end of ``text``.

    Wrapping a styled line means re-opening, on the next line, whatever was
    open when the break happened. Foreground can be left to the caller, but a
    background that is not re-opened leaves a filled block with a torn edge
    down the right-hand side of every wrapped paragraph.

    A sequence that runs out of parameters part-way - ``\\x1b[48;5m``, which is
    what a log cut mid-escape and pasted into the prompt looks like - is
    ignored rather than read past the end. Text arriving here is whatever the
    user pasted or the model said, so malformed is a normal input, not a bug
    in the caller.
    """
    current = ""
    for match in _SGR_PATTERN.finditer(text):
        params = match.group(1) or "0"
        codes = [int(code or 0) for code in params.split(";")]
        index = 0
        while index < len(codes):
            code = codes[index]
            if code == 0 or code == 49:
                current = ""
            elif 40 <= code <= 47 or 100 <= code <= 107:
                current = f"\x1b[{code}m"
            elif code == 48 and index + 1 < len(codes):
                if codes[index + 1] == 5 and index + 2 < len(codes):
                    current = f"\x1b[48;5;{codes[index + 2]}m"
                    index += 2
                elif codes[index + 1] == 2 and index + 4 < len(codes):
                    current = f"\x1b[48;2;{codes[index + 2]};{codes[index + 3]};{codes[index + 4]}m"
                    index += 4
                else:
                    # Truncated. Nothing legible to re-open, and consuming the
                    # rest would be a guess at parameters that never arrived.
                    break
            index += 1
    return current


def fill_line(text: str, width: int, background: str = "") -> str:
    """One rendered line: padded to ``width``, tinted, and terminated.

    The padding is inside the background so a block's tint reaches the right
    edge of the terminal. Without that a message block is only as wide as its
    longest line, which reads as a ragged column rather than a block.
    """
    used = cell_width(text)
    if used > width:
        from hx.term.width import truncate_to_width

        text = truncate_to_width(text, width)
        used = cell_width(text)
    padded = text + " " * (width - used)
    if background:
        padded = f"{background}{padded}{BG_RESET}"
    return terminate(padded)


def wrap(text: str, width: int) -> list[str]:
    """Word-wrap styled text, carrying the active style across each break.

    Wrapping happens on the visible text, not the raw string, so an escape
    sequence never counts toward the line length and never gets split in half.
    A word longer than the whole width - a URL, a base64 blob, a long path -
    is broken rather than allowed to overflow.
    """
    if width <= 0:
        return [""]
    if not strip_ansi(text):
        return [text] if text else [""]

    lines: list[str] = []
    for paragraph in text.split("\n"):
        lines.extend(_wrap_one(paragraph, width))
    return lines


def _tokenize(text: str) -> list[tuple[bool, str]]:
    """Split into ``(is_escape, chunk)`` pairs: escapes, then single glyphs.

    Walking glyphs rather than characters is what keeps a wrap from ever
    landing in the middle of one, and keeping the escapes as their own tokens
    is what lets them be carried across a break without being measured.
    """
    from hx.term.width import grapheme_clusters

    tokens: list[tuple[bool, str]] = []
    index = 0
    while index < len(text):
        escape = _ANY_ESCAPE.match(text, index)
        if escape:
            tokens.append((True, escape.group()))
            index = escape.end()
            continue
        cluster = next(grapheme_clusters(text[index:]), text[index])
        tokens.append((False, cluster))
        index += len(cluster)
    return tokens


def _wrap_one(text: str, width: int) -> list[str]:
    """Wrap one paragraph, breaking at spaces where there is one to break at.

    Guarantees progress: every line consumes at least one glyph, so a glyph
    wider than the whole width ends up alone on its line rather than spinning
    forever. That line is over-wide by construction and the caller truncates
    it - which is the right trade, because the alternative is a hang.
    """
    if cell_width(text) <= width:
        return [text]

    lines: list[str] = []
    tokens = _tokenize(text)
    line: list[str] = []
    used = 0
    # Where the line could be broken instead, and what it had consumed there.
    break_at: int | None = None
    break_used = 0
    carry = ""

    def flush(upto: int | None) -> None:
        nonlocal line, used, break_at, break_used, carry
        if upto is None:
            emitted = "".join(line)
            rest: list[str] = []
        else:
            emitted = "".join(line[:upto])
            rest = line[upto + 1 :]  # drop the space itself
        lines.append(carry + emitted)
        carry = active_background(carry + emitted)
        line = rest
        used = sum(
            0 if is_escape else cell_width(chunk) for is_escape, chunk in _tokenize("".join(rest))
        )
        break_at = None
        break_used = 0

    for is_escape, chunk in tokens:
        if is_escape:
            line.append(chunk)
            continue

        step = cell_width(chunk)
        if used + step > width and line:
            if break_at is not None and break_used > 0:
                flush(break_at)
            else:
                flush(None)

        if chunk == " " and line:
            break_at = len(line)
            break_used = used
        line.append(chunk)
        used += cell_width(chunk)

    if line:
        lines.append(carry + "".join(line))
    return lines or [""]
