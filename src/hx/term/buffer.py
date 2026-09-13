"""The text being edited, as a string and a cursor offset.

A flat string rather than a list of lines. A prompt is short, every operation
here is over a few hundred characters, and a single offset removes the whole
class of bugs where a row and a column disagree about where the cursor is.
Rows and columns are derived where they are needed, never stored.
"""

from __future__ import annotations

from hx.term.word_nav import word_left, word_right


class TextBuffer:
    """Editable text with one cursor."""

    __slots__ = ("_cursor", "_text")

    def __init__(self, text: str = "") -> None:
        self._text = text
        self._cursor = len(text)

    # -- state -------------------------------------------------------------

    @property
    def text(self) -> str:
        return self._text

    @text.setter
    def text(self, value: str) -> None:
        self._text = value
        self._cursor = min(self._cursor, len(value))

    @property
    def cursor(self) -> int:
        return self._cursor

    @cursor.setter
    def cursor(self, value: int) -> None:
        self._cursor = max(0, min(value, len(self._text)))

    @property
    def lines(self) -> list[str]:
        return self._text.split("\n")

    @property
    def row(self) -> int:
        return self._text.count("\n", 0, self._cursor)

    @property
    def column(self) -> int:
        start = self._text.rfind("\n", 0, self._cursor) + 1
        return self._cursor - start

    @property
    def line_count(self) -> int:
        return self._text.count("\n") + 1

    def line_start(self, offset: int | None = None) -> int:
        at = self._cursor if offset is None else offset
        return self._text.rfind("\n", 0, at) + 1

    def line_end(self, offset: int | None = None) -> int:
        at = self._cursor if offset is None else offset
        found = self._text.find("\n", at)
        return len(self._text) if found == -1 else found

    def set(self, text: str, cursor: int | None = None) -> None:
        self._text = text
        self._cursor = len(text) if cursor is None else max(0, min(cursor, len(text)))

    def clear(self) -> None:
        self.set("")

    # -- editing -----------------------------------------------------------

    def insert(self, text: str) -> None:
        self._text = self._text[: self._cursor] + text + self._text[self._cursor :]
        self._cursor += len(text)

    def delete_range(self, start: int, end: int) -> str:
        """Remove ``[start, end)`` and return what was removed."""
        start, end = sorted((max(0, start), min(len(self._text), end)))
        removed = self._text[start:end]
        if not removed:
            return ""
        self._text = self._text[:start] + self._text[end:]
        self._cursor = start
        return removed

    def backspace(self) -> str:
        if self._cursor == 0:
            return ""
        return self.delete_range(self._cursor - 1, self._cursor)

    def delete_forward(self) -> str:
        if self._cursor >= len(self._text):
            return ""
        return self.delete_range(self._cursor, self._cursor + 1)

    def get_range(self, start: int, end: int) -> str:
        start, end = sorted((max(0, start), min(len(self._text), end)))
        return self._text[start:end]

    # -- motion ------------------------------------------------------------

    def left(self) -> None:
        self.cursor = self._cursor - 1

    def right(self) -> None:
        self.cursor = self._cursor + 1

    def word_left(self) -> int:
        return word_left(self._text, self._cursor)

    def word_right(self) -> int:
        return word_right(self._text, self._cursor)

    def home(self) -> None:
        self.cursor = self.line_start()

    def end(self) -> None:
        self.cursor = self.line_end()

    def up(self) -> bool:
        """Move to the previous line, keeping the column. False at the top."""
        start = self.line_start()
        if start == 0:
            return False
        column = self._cursor - start
        previous_start = self.line_start(start - 1)
        previous_end = start - 1
        self.cursor = min(previous_start + column, previous_end)
        return True

    def down(self) -> bool:
        """Move to the next line, keeping the column. False at the bottom."""
        end = self.line_end()
        if end >= len(self._text):
            return False
        column = self._cursor - self.line_start()
        next_start = end + 1
        next_end = self.line_end(next_start)
        self.cursor = min(next_start + column, next_end)
        return True
