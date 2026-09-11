"""Prompt input.

Multiline editing, history, ``@`` file completion, ``/`` command completion, and
``!`` shell passthrough.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from textual import events
from textual.message import Message as TextualMessage
from textual.widgets import TextArea

from hx.keys import KEYMAP
from hx.tui.fuzzy import filter_items
from hx.tui.killring import KillRing
from hx.tui.widgets.autocomplete import Autocomplete, Candidate, Completion

MAX_HISTORY = 500

PLACEHOLDER = "Ask HX…  (/ for commands)"


def running_placeholder(*, enter_steers: bool = False) -> str:
    """What the prompt says while a turn is running.

    The steer key is read from the registry rather than spelled out here: it is
    rebindable, and ``display_key`` writes it the way the platform does - the
    same key reads ``alt+enter`` on Linux and ``option+enter`` on macOS, which
    is the only name on a Mac keyboard. Hard-coding one of the two made this
    line disagree with ``/help`` on one platform or the other.
    """
    steer = KEYMAP.primary("tui.input.steer")
    if enter_steers:
        return f"Ask HX…  (Enter steers · {steer} queues)"
    return f"Ask HX…  (Enter queues · {steer} steers)"


#: Actions that have to beat TextArea's own bindings. A focused widget wins in
#: Textual, so ctrl+c would copy and ctrl+d would delete a character before the
#: app-level binding ever saw them.
_APP_ACTIONS = (
    "app.clear",
    "app.exit",
    "app.message.copy",
    "app.transcript.pageUp",
    "app.transcript.pageDown",
)


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

    class Steered(TextualMessage):
        """Send this into the turn that is already running.

        Empty text is deliberate and meaningful: it means "steer whatever is at
        the front of the queue", which is how a message already queued gets
        promoted without retyping it.
        """

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, cwd: Path) -> None:
        super().__init__(id="prompt", soft_wrap=True, placeholder=PLACEHOLDER)
        self.cwd = cwd
        self._history: list[str] = []
        self._history_index: int | None = None
        self._draft = ""
        self.completer = FileCompleter(cwd)
        self.kill_ring = KillRing()
        #: Range covered by the last yank and the text it put there, so alt+y
        #: can tell "immediately after a yank" from "the buffer has moved on".
        self._last_yank: tuple[tuple[int, int], tuple[int, int], str] | None = None
        self.completion: Completion | None = None
        #: Set by escape, cleared when the token being completed changes, so a
        #: dismissed popup stays dismissed but the next ``/`` still opens one.
        self._completion_dismissed = False

    def _matches(self, key: str, action: str) -> bool:
        return key in KEYMAP.keys_for(action)

    async def _on_key(self, event: events.Key) -> None:
        key = event.key
        handled = self._handle_key(key)
        if handled is None:
            await super()._on_key(event)
            return
        event.prevent_default()
        event.stop()
        if handled is not NotImplemented:
            await handled

    def _handle_key(self, key: str) -> Any:
        """Dispatch one key, or return None to let TextArea have it.

        Returns an awaitable when the key runs an app action, and
        ``NotImplemented`` when it was handled here and nothing needs awaiting.
        The three-way answer is what lets one branchy method cover editing
        keys, app keys, and everything TextArea already does well.
        """
        if self._autocomplete_key(key) is not None:
            return NotImplemented

        if self._matches(key, "tui.input.submit"):
            self._submit()
            return NotImplemented
        if self._matches(key, "tui.input.steer"):
            self._steer()
            return NotImplemented
        if self._matches(key, "tui.input.newLine"):
            self.insert("\n")
            return NotImplemented
        if self._matches(key, "tui.input.complete"):
            self.complete()
            return NotImplemented

        # History at the edges of the buffer, so the arrows still move the
        # cursor everywhere else in a multi-line draft.
        if key == "up" and self.cursor_location[0] == 0:
            self.history_prev()
            return NotImplemented
        if key == "down" and self.cursor_location[0] == self.document.line_count - 1:
            self.history_next()
            return NotImplemented

        if self._editing_key(key):
            return NotImplemented

        # ctrl+d deletes a character with text in the buffer, and exits without.
        if self._matches(key, "app.exit") and self.text:
            return None

        for action in _APP_ACTIONS:
            if self._matches(key, action):
                from hx.keys import action_name

                return getattr(self.app, f"action_{action_name(action)}")()
        return None

    def _editing_key(self, key: str) -> bool:
        """Readline keys TextArea does not bind, plus the kills we capture."""
        if self._matches(key, "tui.editor.cursorLeft"):
            self.move_cursor(self.get_cursor_left_location())
        elif self._matches(key, "tui.editor.cursorRight"):
            self.move_cursor(self.get_cursor_right_location())
        elif self._matches(key, "tui.editor.cursorWordLeft"):
            self.move_cursor(self.get_cursor_word_left_location())
        elif self._matches(key, "tui.editor.cursorWordRight"):
            self.move_cursor(self.get_cursor_word_right_location())
        elif key in {"ctrl+w", "alt+backspace"}:
            self._kill_to(self.get_cursor_word_left_location())
        elif self._matches(key, "tui.editor.deleteWordForward"):
            self._kill_to(self.get_cursor_word_right_location())
        elif key == "ctrl+u":
            self._kill_to((self.cursor_location[0], 0))
        elif key == "ctrl+k":
            row = self.cursor_location[0]
            self._kill_to((row, len(self.document[row])))
        elif self._matches(key, "tui.editor.yank"):
            self._yank()
        elif self._matches(key, "tui.editor.yankPop"):
            self._yank_pop()
        elif self._matches(key, "tui.editor.redo"):
            self.action_redo()
        else:
            return False
        return True

    def _kill_to(self, target: tuple[int, int]) -> None:
        """Delete between the cursor and ``target``, keeping the text on the ring."""
        start, end = sorted((self.cursor_location, target))
        killed = self.get_text_range(start, end)
        if not killed:
            return
        self.kill_ring.kill(killed)
        self._last_yank = None
        self.delete(start, end)

    def _yank(self) -> None:
        text = self.kill_ring.yank()
        if text is None:
            return
        start = self.cursor_location
        self.insert(text)
        self._last_yank = (start, self.cursor_location, text)

    def _yank_pop(self) -> None:
        """Replace the text just yanked with the next kill down the ring.

        Only valid immediately after a yank, as in emacs. The recorded range is
        re-checked against the buffer rather than trusted: any edit since the
        yank moves the text under it, and replacing a stale range mangles
        whatever happens to sit there now.
        """
        current = self._current_yank()
        if current is None:
            self._last_yank = None
            return
        text = self.kill_ring.yank_pop()
        if text is None:
            return
        start, end, _ = current
        self.delete(start, end)
        self.move_cursor(start)
        self.insert(text)
        self._last_yank = (start, self.cursor_location, text)

    def _current_yank(self) -> tuple[tuple[int, int], tuple[int, int], str] | None:
        """The last yank while it is still intact under the cursor, else None."""
        if self._last_yank is None:
            return None
        start, end, text = self._last_yank
        if self.cursor_location != end or self.get_text_range(start, end) != text:
            return None
        return self._last_yank

    def on_text_area_changed(self, event: object) -> None:
        """Keep an open popup in step with the text it is completing."""
        self.refresh_completion()

    def _submit(self) -> None:
        self.close_completion()
        text = self.text.strip()
        if not text:
            return
        self._remember(text)
        self.post_message(self.Submitted(text))

    def _steer(self) -> None:
        """Steer the draft, or - with nothing typed - whatever is queued."""
        self.close_completion()
        text = self.text.strip()
        if text:
            self._remember(text)
        self.post_message(self.Steered(text))

    def _remember(self, text: str) -> None:
        """File the sent text in history and clear the buffer for the next one."""
        self._history.append(text)
        del self._history[:-MAX_HISTORY]
        self._history_index = None
        self._draft = ""
        self.clear()

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

    # -- Completion ---------------------------------------------------------

    def _autocomplete_key(self, key: str) -> bool | None:
        """Keys the open popup owns. None when there is nothing open."""
        completion = self.completion
        if completion is None:
            return None
        if key in {"up", "down"}:
            completion.move(-1 if key == "up" else 1)
            self._sync_popup()
            return True
        if self._matches(key, "tui.input.submit"):
            # Once the prompt already contains the highlighted completion,
            # Enter means submit. Treating it as another completion acceptance
            # made exact slash commands such as /clear and /model require a
            # surprising second Enter.
            candidate = completion.current
            if candidate is not None and self.text == completion.prefix + candidate.value:
                self._submit()
            else:
                self.accept_completion()
            return True
        if self._matches(key, "tui.input.complete"):
            self.accept_completion()
            return True
        if self._matches(key, "app.interrupt"):
            self.close_completion()
            return True
        return None

    def complete(self) -> None:
        """Open the popup for the token under the cursor, or advance it.

        Tab with a popup already open cycles, which is what makes the key work
        the same whether or not the user has looked at the list yet.
        """
        if self.completion is not None:
            self.completion.move(1)
            self._sync_popup()
            return
        self._completion_dismissed = False
        self.completion = self._build_completion()
        self._sync_popup()

    def refresh_completion(self) -> None:
        """Open, refilter or close the popup for whatever is under the cursor.

        Opening as the user types is what makes ``/`` discoverable; tab still
        works for anyone who expects to ask for it.
        """
        built = self._build_completion()
        if built is None:
            self._completion_dismissed = False
            self.completion = None
        elif self._completion_dismissed:
            self.completion = None
        else:
            # Keep the highlighted row where it was while the list narrows.
            if self.completion is not None and self.completion.start == built.start:
                built.index = min(self.completion.index, max(0, len(built.candidates) - 1))
            self.completion = built
        self._sync_popup()

    def close_completion(self) -> None:
        self._completion_dismissed = self.completion is not None
        self.completion = None
        self._sync_popup()

    def accept_completion(self) -> None:
        completion = self.completion
        candidate = completion.current if completion else None
        if completion is None or candidate is None:
            return
        # Splice, never truncate: the token being completed runs from the
        # completion's start to the cursor, and everything after the cursor is
        # the rest of the user's draft. Rebuilding the buffer from the prefix
        # alone threw that away - "@sr and fix the bug" became "@src/".
        text = self.text
        replacement = completion.prefix + candidate.value
        end = completion.start + len(replacement)
        self.text = text[: completion.start] + replacement + text[self._offset_of_cursor() :]
        self.move_cursor(self._location_of_offset(end))
        self.completion = None
        self._completion_dismissed = False
        self._sync_popup()

    def _build_completion(self) -> Completion | None:
        text = self.text[: self._offset_of_cursor()]
        if text.startswith("/") and "\n" not in text and " " not in text:
            return self._command_completion(text)

        at = text.rfind("@")
        if at != -1 and not any(char.isspace() for char in text[at + 1 :]):
            return self._path_completion(text, at)
        return None

    def _command_completion(self, text: str) -> Completion | None:
        commands = getattr(self.app, "commands", None)
        if commands is None:
            return None
        query = text[1:]
        # Name only. Matching summaries here turns "/co" into half the list,
        # because some other command's summary contains a c before an o; the
        # palette is where searching descriptions belongs.
        matches = filter_items(commands.all(), query, key=lambda command: command.name)
        candidates = [
            Candidate(value=command.name, label=f"/{command.name}", detail=command.summary)
            for command in matches
        ]
        return Completion(start=0, prefix="/", candidates=candidates) if candidates else None

    def _path_completion(self, text: str, at: int) -> Completion | None:
        matches = self.completer.complete(text[at + 1 :])
        candidates = [Candidate(value=path, label=path) for path in matches]
        return Completion(start=at, prefix="@", candidates=candidates) if candidates else None

    def _offset_of_cursor(self) -> int:
        row, column = self.cursor_location
        lines = self.text.split("\n")
        return sum(len(line) + 1 for line in lines[:row]) + column

    def _location_of_offset(self, offset: int) -> tuple[int, int]:
        """The inverse of :meth:`_offset_of_cursor`, for placing the cursor
        back on a character index after the buffer has been rewritten."""
        lines = self.text.split("\n")
        remaining = max(0, offset)
        for row, line in enumerate(lines):
            if remaining <= len(line):
                return (row, remaining)
            remaining -= len(line) + 1
        return (len(lines) - 1, len(lines[-1]))

    def _sync_popup(self) -> None:
        try:
            popup = self.screen.query_one(Autocomplete)
        except Exception:  # Not mounted (unit tests, or a modal is up).
            return
        popup.show(self.completion)

    def set_running(self, running: bool, *, enter_steers: bool = False) -> None:
        """Reflect turn state without locking the user's draft.

        Submissions made while a turn is running are queued by ``HXApp``.  The
        prompt must stay editable so the user can prepare and submit them.
        """
        if not running:
            self.set_placeholder(PLACEHOLDER)
            return
        self.set_placeholder(running_placeholder(enter_steers=enter_steers))


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
