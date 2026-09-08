"""Prompt input.

Multiline editing, history, ``@`` file completion, ``/`` command completion, and
``!`` shell passthrough.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual import events
from textual.message import Message as TextualMessage
from textual.widgets import TextArea

MAX_HISTORY = 500


class PromptInput(TextArea):
    """Multiline prompt.

    Enter submits, Ctrl+J inserts a newline - the opposite of a plain TextArea,
    because submitting is the common action and a stray newline on Enter would
    make the input feel broken.
    """

    class Submitted(TextualMessage):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, cwd: Path) -> None:
        super().__init__(id="prompt", soft_wrap=True, placeholder="Ask HX…  (/ for commands)")
        self.cwd = cwd
        self._history: list[str] = []
        self._history_index: int | None = None
        self._draft = ""
        self.completer = FileCompleter(cwd)

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            self._submit()
            return
        if event.key in {"ctrl+j", "shift+enter"}:
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        if event.key == "up" and self.cursor_location[0] == 0:
            event.prevent_default()
            event.stop()
            self.history_prev()
            return
        if event.key == "down" and self.cursor_location[0] == self.document.line_count - 1:
            event.prevent_default()
            event.stop()
            self.history_next()
            return
        if event.key == "tab":
            event.prevent_default()
            event.stop()
            self.complete()
            return
        await super()._on_key(event)

    def _submit(self) -> None:
        text = self.text.strip()
        if not text:
            return
        self._history.append(text)
        del self._history[:-MAX_HISTORY]
        self._history_index = None
        self._draft = ""
        self.clear()
        self.post_message(self.Submitted(text))

    def set_placeholder(self, text: str) -> None:
        self.placeholder = text

    def history_prev(self) -> None:
        if not self._history:
            return
        if self._history_index is None:
            self._draft = self.text
            self._history_index = len(self._history)
        self._history_index = max(0, self._history_index - 1)
        self.text = self._history[self._history_index]
        self.move_cursor(self.document.end)

    def history_next(self) -> None:
        if self._history_index is None:
            return
        self._history_index += 1
        if self._history_index >= len(self._history):
            self._history_index = None
            self.text = self._draft
        else:
            self.text = self._history[self._history_index]
        self.move_cursor(self.document.end)

    def complete(self) -> None:
        """Dispatch to file completion after ``@`` or command completion after ``/``."""
        text = self.text
        at = text.rfind("@")
        if at == -1 or " " in text[at + 1 :]:
            return
        matches = self.completer.complete(text[at + 1 :])
        if len(matches) == 1:
            self.text = text[:at] + "@" + matches[0]
            self.move_cursor(self.document.end)

    def set_enabled(self, enabled: bool) -> None:
        """Disabled while a turn streams; queued input is submitted after."""
        self.read_only = not enabled
        self.set_placeholder("Ask HX…  (/ for commands)" if enabled else "Esc to interrupt…")


class FileCompleter:
    """``@``-triggered path completion, gitignore-aware and ranked by recency."""

    IGNORED: ClassVar[frozenset[str]] = frozenset(
        {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
    )
    LIMIT: ClassVar[int] = 20

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd

    def complete(self, prefix: str) -> list[str]:
        pattern = f"{prefix}*" if prefix else "*"
        try:
            candidates = [
                path
                for path in self.cwd.glob(pattern)
                if not any(part in self.IGNORED or part.startswith(".") for part in path.parts)
            ]
        except (OSError, ValueError):
            return []
        candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        return [
            str(path.relative_to(self.cwd)) + ("/" if path.is_dir() else "")
            for path in candidates[: self.LIMIT]
        ]
