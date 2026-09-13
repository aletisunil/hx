"""Where a word starts and ends, for readline-style motion.

Emacs and readline treat a word as a run of alphanumerics, and skip whatever
sits between runs. That is not the same as splitting on whitespace: moving back
a word from the end of ``foo.bar()`` should land on ``bar``, not on ``foo``.
"""

from __future__ import annotations


def _is_word(char: str) -> bool:
    return char.isalnum() or char == "_"


def word_left(text: str, index: int) -> int:
    """Start of the word at or before ``index``."""
    index = max(0, min(index, len(text)))
    while index > 0 and not _is_word(text[index - 1]):
        index -= 1
    while index > 0 and _is_word(text[index - 1]):
        index -= 1
    return index


def word_right(text: str, index: int) -> int:
    """End of the word at or after ``index``."""
    length = len(text)
    index = max(0, min(index, length))
    while index < length and not _is_word(text[index]):
        index += 1
    while index < length and _is_word(text[index]):
        index += 1
    return index
