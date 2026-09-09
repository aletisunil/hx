"""Fuzzy matching for the command palette, pickers and prompt completion.

Ported from pi's ``fuzzy.ts``. A query matches when its characters appear in
order, not necessarily together, and the score rewards what a human means by a
good match: runs of consecutive characters, hits at word boundaries, and early
positions. Lower is better.

Substring matching is what this replaces, and the difference shows up on the
queries people actually type: ``pm`` finds ``permissions``, ``mcp srv`` finds
``mcp server status``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import TypeVar

T = TypeVar("T")

_BOUNDARY = re.compile(r"[\s\-_./:]")
_TOKENS = re.compile(r"[\s/]+")
_LETTERS_DIGITS = re.compile(r"^([a-z]+)([0-9]+)$")
_DIGITS_LETTERS = re.compile(r"^([0-9]+)([a-z]+)$")


def _score(query: str, text: str) -> float | None:
    """Score ``query`` against ``text``, or None when it does not match."""
    if not query:
        return 0.0
    if len(query) > len(text):
        return None

    score = 0.0
    query_index = 0
    last_match = -1
    consecutive = 0

    for i, char in enumerate(text):
        if query_index >= len(query):
            break
        if char != query[query_index]:
            continue

        at_boundary = i == 0 or bool(_BOUNDARY.match(text[i - 1]))
        if last_match == i - 1:
            consecutive += 1
            score -= consecutive * 5
        else:
            consecutive = 0
            if last_match >= 0:
                score += (i - last_match - 1) * 2
        if at_boundary:
            score -= 10
        score += i * 0.1
        last_match = i
        query_index += 1

    if query_index < len(query):
        return None
    if query == text:
        score -= 100
    return score


def match(query: str, text: str) -> float | None:
    """Score one query against one string, retrying with digits and letters swapped.

    The swap covers ``4sonnet`` for ``sonnet-4``, which is how people reach for
    a model id when they cannot remember which way round it goes.
    """
    query_lower = query.lower()
    text_lower = text.lower()

    score = _score(query_lower, text_lower)
    if score is not None:
        return score

    swapped = ""
    if (found := _LETTERS_DIGITS.match(query_lower)) or (
        found := _DIGITS_LETTERS.match(query_lower)
    ):
        swapped = found.group(2) + found.group(1)
    if not swapped:
        return None

    swapped_score = _score(swapped, text_lower)
    return None if swapped_score is None else swapped_score + 5


def filter_items(
    items: Iterable[T],
    query: str,
    key: Callable[[T], str],
) -> list[T]:
    """Items that match every whitespace- or slash-separated token, best first.

    Requiring every token to match is what makes a second word narrow the list
    instead of widening it.
    """
    entries = list(items)
    if not query.strip():
        return entries

    tokens = [token for token in _TOKENS.split(query.strip()) if token]
    if not tokens:
        return entries

    scored: list[tuple[float, int, T]] = []
    for position, item in enumerate(entries):
        text = key(item)
        total = 0.0
        for token in tokens:
            score = match(token, text)
            if score is None:
                break
            total += score
        else:
            # Position breaks ties, so equal matches keep their original order.
            scored.append((total, position, item))

    scored.sort(key=lambda row: (row[0], row[1]))
    return [item for _, _, item in scored]
