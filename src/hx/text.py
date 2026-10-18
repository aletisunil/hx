"""Wording shared by the tools, the TUI and the CLI."""

from __future__ import annotations


def count(number: int, singular: str, plural: str | None = None) -> str:
    """``1 line``, ``2 lines``: a number with its noun agreeing.

    ``plural`` is for the nouns an ``s`` does not make plural - ``match``.
    """
    noun = singular if number == 1 else (plural or f"{singular}s")
    return f"{number} {noun}"
