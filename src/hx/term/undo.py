"""Undo, as a stack of whole snapshots.

A draft in a prompt is small - a few lines - so storing the entire text per
step costs nothing and removes every question about how edits compose. The
alternative, inverse operations, is worth it for a document and not for this.

Consecutive typing is coalesced: undo should step back a word or a line, not a
character, or it takes as many keystrokes to undo something as it did to type
it.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_DEPTH = 200


@dataclass(frozen=True, slots=True)
class Snapshot:
    text: str
    cursor: int


class UndoStack:
    def __init__(self) -> None:
        self._past: list[Snapshot] = []
        self._future: list[Snapshot] = []
        self._coalescing = False

    def record(self, text: str, cursor: int, *, coalesce: bool = False) -> None:
        """Remember a state before it is changed.

        ``coalesce`` merges this into the previous step, which is what makes a
        run of typed characters one undo rather than twenty.
        """
        if self._past and self._past[-1].text == text:
            return
        if coalesce and self._coalescing and self._past:
            self._coalescing = True
            return
        self._past.append(Snapshot(text, cursor))
        del self._past[:-MAX_DEPTH]
        self._future.clear()
        self._coalescing = coalesce

    def break_run(self) -> None:
        """End a coalescing run, so the next change starts its own step."""
        self._coalescing = False

    def undo(self, text: str, cursor: int) -> Snapshot | None:
        if not self._past:
            return None
        self._future.append(Snapshot(text, cursor))
        self._coalescing = False
        return self._past.pop()

    def redo(self, text: str, cursor: int) -> Snapshot | None:
        if not self._future:
            return None
        self._past.append(Snapshot(text, cursor))
        self._coalescing = False
        return self._future.pop()
