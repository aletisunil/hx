"""An emacs kill ring for the prompt.

Every kill - ctrl+w, ctrl+u, ctrl+k - pushes here, ctrl+y pastes the newest
back, and alt+y walks further down the ring. It is separate from the system
clipboard on purpose: the ring is for text you cut a second ago and want back,
and using the clipboard for that would trample whatever the user had copied
from somewhere else.
"""

from __future__ import annotations

from collections import deque

MAX_ENTRIES = 32


class KillRing:
    """Newest kill first. Bounded, because this is a scratch buffer."""

    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self._entries: deque[str] = deque(maxlen=max_entries)
        self._index = 0

    def __len__(self) -> int:
        return len(self._entries)

    def kill(self, text: str) -> None:
        """Record killed text. Empty kills are dropped, not stored."""
        if not text:
            return
        self._entries.appendleft(text)
        self._index = 0

    def yank(self) -> str | None:
        """The newest kill, and the start of a yank-pop sequence."""
        if not self._entries:
            return None
        self._index = 0
        return self._entries[0]

    def yank_pop(self) -> str | None:
        """The next older kill, wrapping around at the end of the ring."""
        if len(self._entries) < 2:
            return None
        self._index = (self._index + 1) % len(self._entries)
        return self._entries[self._index]
