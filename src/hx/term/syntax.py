"""Syntax highlighting, emitted as escape sequences.

:func:`hx.tui.theme.syntax_style` already builds a Pygments style class out of
the palette's ``syntax_*`` roles, so a code block follows the user's theme
rather than snapping to one of Pygments' own. That part is unchanged. What
changes is the consumer: instead of handing the style to Rich, the tokens are
walked here and turned into colour directly.

Language detection is deliberately absent. Guessing at an unknown language
reliably mistakes English prose for some obscure dialect and colours random
words as keywords, which looks like a rendering bug and is worse than plain
text.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from hx.term.ansi import ColorMode, fg
from hx.term.width import cell_width


@lru_cache(maxsize=256)
def _lexer_for(language: str | None, filename: str | None) -> Any:
    """A lexer, or ``None`` if we cannot be confident which one applies."""
    from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
    from pygments.util import ClassNotFound

    if language:
        try:
            return get_lexer_by_name(language, stripnl=False, ensurenl=False)
        except ClassNotFound:
            pass
    if filename:
        try:
            return get_lexer_for_filename(filename, stripnl=False, ensurenl=False)
        except ClassNotFound:
            pass
    return None


def highlight(
    code: str,
    language: str | None = None,
    *,
    filename: str | None = None,
    style: Any = None,
    mode: ColorMode = "truecolor",
) -> list[str]:
    """Colour ``code``, one string per line.

    Falls back to the text unchanged when the language is unknown, which is the
    right answer far more often than a guess would be.
    """
    lexer = _lexer_for(language, filename)
    if lexer is None:
        return code.split("\n")

    if style is None:
        from hx.tui.theme import syntax_style

        style = syntax_style()

    colours = _colour_table(style)

    lines: list[str] = [""]
    for token, value in lexer.get_tokens(code):
        colour = _lookup(colours, token)
        for index, piece in enumerate(value.split("\n")):
            if index:
                lines.append("")
            if piece:
                lines[-1] += fg(colour, piece, mode) if colour else piece

    # get_tokens appends a trailing newline whatever the input did.
    if lines and not cell_width(lines[-1]):
        lines.pop()
    return lines or [""]


@lru_cache(maxsize=8)
def _colour_table(style: Any) -> dict[Any, str]:
    table: dict[Any, str] = {}
    for token, definition in style.styles.items():
        if definition:
            table[token] = definition.split()[-1]
    return table


def _lookup(table: dict[Any, str], token: Any) -> str:
    """Walk up the token hierarchy until something has a colour.

    ``Name.Function.Magic`` should take ``Name.Function``'s colour rather than
    falling through to no colour at all.
    """
    current = token
    while current is not None:
        if current in table:
            return table[current]
        current = current.parent
    return ""
