"""HX's prompt: the editor, plus everything the app expects of it.

Every behaviour here carries over from the Textual prompt, because it is all
behaviour users have muscle memory for. What changes is that most of it is now
implemented rather than inherited - ``TextArea`` was quietly supplying word
motion, undo and soft-wrap navigation underneath the parts HX wrote.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from hx.core.images import pasted_image_paths
from hx.term.editor import Editor
from hx.term.sanitize import plain_text
from hx.tui.format import columns
from hx.tui.fuzzy import filter_items
from hx.tui.glyphs import CURSOR, GUTTER
from hx.tui.killring import KillRing
from hx.tui.limits import LIST_VISIBLE
from hx.tui.paint import fg, rule

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hx.core.messages import ImageBlock

MAX_HISTORY = 200

IMAGE_TOKEN = re.compile(r"\[Image #(\d+)\]")
"""What an attached image looks like in the draft. The number is the image's
for the whole session, so a message recalled from history still means the
picture it meant when it was sent."""

PASTE_TOKEN = re.compile(r"\[Pasted text #(\d+) (?:\+\d+ lines|\d+ chars)\]")
"""What a large paste looks like in the draft. It stands for the text, which
goes out in its place on submit; like an image token, deleting it deletes the
paste."""

PASTE_COLLAPSE_LINES = 10
PASTE_COLLAPSE_CHARS = 1000
"""A paste over either limit is collapsed to a :data:`PASTE_TOKEN`. The draft
is capped at a third of the screen, so forty lines of a log would leave the
prompt showing a window onto the middle of it, with whatever the user typed
around it scrolled out of sight."""

_CSI_U_CONTROL = re.compile(r"\x1b\[(\d+);5u|\x1b\[27;5;(\d+)~")


def clean_paste(data: str) -> str:
    """Pasted text as the user copied it: real newlines, nothing that can act.

    Terminals send a pasted line break as a carriage return - it is what the
    Enter key sends, and a paste is typed as if by keyboard - so without
    translating it back every multi-line paste lands as one long line. Inside
    tmux with extended keys, control bytes can even arrive re-encoded as
    ``ctrl+<letter>`` key sequences, so those are decoded back to the byte
    first, before sanitizing would strip them to nothing.
    """

    def control(match: re.Match[str]) -> str:
        code = int(match.group(1) or match.group(2))
        if 0x61 <= code <= 0x7A:  # a-z
            return chr(code - 0x60)
        if 0x40 <= code <= 0x5F:  # @, A-Z and [\]^_
            return chr(code - 0x40)
        return match.group(0)

    if "\x1b" in data:
        data = _CSI_U_CONTROL.sub(control, data)
    data = data.replace("\r\n", "\n").replace("\r", "\n")
    return plain_text(data)


@dataclass
class Candidate:
    """One completion row: what gets inserted, and what the row says."""

    value: str
    label: str
    detail: str = ""


@dataclass
class Completion:
    """An open completion session over the prompt's current token."""

    start: int
    """Offset in the prompt text where the replaced token starts."""
    prefix: str
    """Text inserted before the value: ``/`` or ``@``."""
    candidates: list[Candidate] = field(default_factory=list)
    index: int = 0

    @property
    def current(self) -> Candidate | None:
        if not self.candidates:
            return None
        return self.candidates[self.index % len(self.candidates)]

    def move(self, delta: int) -> None:
        if self.candidates:
            self.index = (self.index + delta) % len(self.candidates)


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


def placeholder_text(*, running: bool = False, enter_steers: bool = False) -> str:
    """What the empty prompt says, and what Enter will do to it.

    The steer key is read from the registry rather than spelled out. Hard-coded
    it said "alt+enter" while /help said "option+enter" on macOS, and a rebind
    left it naming a key that no longer steered.
    """
    if not running:
        return "Ask HX…  (/ for commands)"

    from hx.keys import KEYMAP

    steer = KEYMAP.primary("tui.input.steer")
    if enter_steers:
        return f"Ask HX…  (Enter steers · {steer} queues)"
    return f"Ask HX…  (Enter queues · {steer} steers)"


class Prompt(Editor):
    """The prompt, with HX's keys, history, kill ring and completion."""

    def __init__(
        self,
        cwd: Path,
        *,
        commands: Any = None,
        rows_available: Any = None,
        on_submit: Any = None,
        on_steer: Any = None,
        on_image_request: Any = None,
    ) -> None:
        super().__init__(
            rule_color=rule("border_muted"),
            rows_available=rows_available or (lambda: 24),
        )
        self.cwd = cwd
        self.commands = commands
        self.completer = FileCompleter(cwd)
        self.kill_ring = KillRing()
        self.on_submit = on_submit
        self.on_steer = on_steer
        self.on_image_request = on_image_request
        """Called with image paths a paste named, or ``None`` for whatever image
        is on the clipboard. Loading is the app's job - it is disk and decoder
        work that must not run on the keypress - and it answers with
        :meth:`attach_image`."""
        self._images: dict[int, ImageBlock] = {}
        self._last_image = 0
        self._pastes: dict[int, str] = {}
        """Every collapsed paste this session. Kept after submit, so a draft
        recalled from history, or yanked back from the kill ring, still
        expands to what it stood for."""
        self._last_paste = 0

        self.completion: Completion | None = None
        self._completion_dismissed = False
        self._history: list[str] = []
        self._history_index: int | None = None
        self._draft = ""
        self._last_yank: tuple[int, int, str] | None = None
        self._running = False
        self._enter_steers = False
        self.placeholder = placeholder_text(running=False, enter_steers=False)

    # -- the app's view of it ----------------------------------------------

    @property
    def value(self) -> str:
        return self.text

    def attach_image(self, image: ImageBlock) -> ImageBlock:
        """Insert an image's token at the cursor. Returns the image as labelled.

        The token is the attachment: deleting it from the draft drops the
        image, and moving it moves where the text refers to it.
        """
        self._last_image += 1
        number = self._last_image
        labelled = replace(image, label=f"Image #{number}", source=image.source or image.label)
        self._images[number] = labelled
        token = f"[{labelled.label}]"
        before = self.text[: self.buffer.cursor]
        after = self.text[self.buffer.cursor :]
        if before and not before[-1].isspace():
            token = " " + token
        if not after or not after[0].isspace():
            token += " "
        self.insert(token)
        self._sync_completion()
        return labelled

    def expand_pastes(self, text: str) -> str:
        """``text`` with every paste token replaced by the text it stands for.

        One pass, so pasted text that happens to contain something shaped like
        a token is sent as it was pasted rather than expanded again.
        """
        if "[Pasted text #" not in text:
            return text
        return PASTE_TOKEN.sub(lambda m: self._pastes.get(int(m.group(1)), m.group(0)), text)

    def _paste(self, data: str) -> None:
        """Insert a paste - collapsed to a token when it is large."""
        text = clean_paste(data)
        # splitlines, so the newline most copied text ends with is not
        # counted as a line of its own.
        lines = len(text.splitlines())
        if lines <= PASTE_COLLAPSE_LINES and len(text) <= PASTE_COLLAPSE_CHARS:
            self.insert(text)
            return
        self._last_paste += 1
        number = self._last_paste
        self._pastes[number] = text
        size = f"+{lines} lines" if lines > PASTE_COLLAPSE_LINES else f"{len(text)} chars"
        # Not padded with spaces as an image token is: the token expands to
        # exactly what was pasted, and a space either side would change it.
        self.insert(f"[Pasted text #{number} {size}]")

    def images_in(self, text: str) -> list[ImageBlock]:
        """The images whose tokens ``text`` holds, in the order it mentions them."""
        numbers = dict.fromkeys(int(match.group(1)) for match in IMAGE_TOKEN.finditer(text))
        return [self._images[number] for number in numbers if number in self._images]

    def remember_images(self, images: Sequence[ImageBlock]) -> None:
        """Take back images that were sent from here before - a resumed session,
        a rewound prompt.

        Their tokens then resolve again, and the next image is numbered past
        them: a second ``Image #1`` would leave the model two pictures by one
        name.
        """
        for image in images:
            match = IMAGE_TOKEN.fullmatch(f"[{image.label}]")
            if match is None:
                continue
            number = int(match.group(1))
            self._images[number] = image
            self._last_image = max(self._last_image, number)

    def set_running(self, running: bool, *, enter_steers: bool = False) -> None:
        self._running = running
        self._enter_steers = enter_steers
        self.placeholder = placeholder_text(running=running, enter_steers=enter_steers)
        self.invalidate()

    def set_tint(self, color: Any) -> None:
        """Recolour the frame - the thinking level, or bash mode."""
        self.top_rule.set_color(color)
        self.bottom_rule.set_color(color)
        self.invalidate()

    # -- keys --------------------------------------------------------------

    def handle_input(self, key: str, data: str) -> bool:
        from hx.keys import KEYMAP

        def bound(action: str) -> bool:
            return key in KEYMAP.keys_for(action)

        if self._completion_key(key, bound):
            return True

        if bound("tui.input.submit"):
            if self._backslash_newline():
                return True
            self._submit()
            return True
        if bound("tui.input.steer"):
            self._steer()
            return True
        if bound("tui.input.newLine"):
            self.insert("\n")
            self._sync_completion()
            return True
        if bound("tui.input.complete"):
            self.complete()
            return True
        if bound("tui.input.pasteImage"):
            self._request_image(None)
            return True

        # History at the edges of the buffer, so the arrows still move the
        # cursor everywhere else in a multi-line draft.
        if key == "up" and self.buffer.row == 0:
            self.history_prev()
            return True
        if key == "down" and self.buffer.row == self.buffer.line_count - 1:
            self.history_next()
            return True

        return self._editing_key(key, data, bound)

    def _backslash_newline(self) -> bool:
        """``\\`` then Enter: a newline, for terminals that cannot send shift+enter.

        Apple's Terminal, a tmux without extended keys, and anything over a
        link that strips the modifier all send shift+enter as a bare Enter.
        The backslash is the shell's own line continuation, so it is the
        fallback people already reach for.

        A backslash escapes itself, so a message can still end in one: ``\\\\``
        then Enter sends a single trailing ``\\``. Returns whether the key was
        taken as a newline.
        """
        cursor = self.buffer.cursor
        before = self.text[:cursor]
        backslashes = len(before) - len(before.rstrip("\\"))
        if backslashes == 0:
            return False
        if backslashes % 2 == 0:
            # Escaped: drop the escape, and let the Enter send.
            self.buffer.delete_range(cursor - 1, cursor)
            return False
        self.undo_stack.record(self.text, cursor)
        self.buffer.delete_range(cursor - 1, cursor)
        self.buffer.insert("\n")
        self.invalidate()
        self._sync_completion()
        return True

    def _editing_key(self, key: str, data: str, bound: Any) -> bool:
        buffer = self.buffer

        if key == "paste" and self.on_image_request is not None:
            # A terminal sends an empty paste when the clipboard holds a
            # picture and no text, and a dragged-in file arrives as its path.
            # Either one is an image to attach, not text to insert.
            if not data:
                self._request_image(None)
                return True
            if paths := pasted_image_paths(clean_paste(data)):
                self._request_image(paths)
                return True
        # Both sanitized here rather than on submit: a paste is whatever was on
        # the clipboard, and the decoder's "printable" run admits the C1 range,
        # where a terminal reads 0x9b as CSI. The draft is drawn to the
        # terminal as it is typed, so an escape in it would act before anyone
        # pressed enter.
        if key == "paste":
            self._paste(data)
            self._sync_completion()
            return True
        if key == "text":
            self.insert(plain_text(data), coalesce=True)
            self._sync_completion()
            return True
        if key == "backspace":
            self.undo_stack.record(self.text, buffer.cursor, coalesce=True)
            if buffer.cursor > 0:
                buffer.delete_range(*self._widened(buffer.cursor - 1, buffer.cursor))
            self.invalidate()
            self._sync_completion()
            return True
        if key == "delete":
            self.undo_stack.record(self.text, buffer.cursor)
            if buffer.cursor < len(self.text):
                buffer.delete_range(*self._widened(buffer.cursor, buffer.cursor + 1))
            self.invalidate()
            self._sync_completion()
            return True

        if key == "left" or bound("tui.editor.cursorLeft"):
            buffer.left()
            self._snap_cursor(forward=False)
        elif key == "right" or bound("tui.editor.cursorRight"):
            buffer.right()
            self._snap_cursor(forward=True)
        elif bound("tui.editor.cursorWordLeft"):
            buffer.cursor = buffer.word_left()
            self._snap_cursor(forward=False)
        elif bound("tui.editor.cursorWordRight"):
            buffer.cursor = buffer.word_right()
            self._snap_cursor(forward=True)
        elif key in ("home", "ctrl+a"):
            buffer.home()
        elif key in ("end", "ctrl+e"):
            buffer.end()
        elif key == "up":
            buffer.up()
            self._snap_cursor(forward=False)
        elif key == "down":
            buffer.down()
            self._snap_cursor(forward=True)
        elif key in ("ctrl+w", "alt+backspace"):
            self._kill_to(buffer.word_left())
        elif bound("tui.editor.deleteWordForward"):
            self._kill_to(buffer.word_right())
        elif key == "ctrl+u":
            self._kill_to(buffer.line_start())
        elif key == "ctrl+k":
            self._kill_to(buffer.line_end())
        elif bound("tui.editor.yank"):
            self._yank()
        elif bound("tui.editor.yankPop"):
            self._yank_pop()
        elif bound("tui.editor.undo"):
            self.undo()
        elif bound("tui.editor.redo"):
            self.redo()
        else:
            return False

        self.invalidate()
        self._sync_completion()
        return True

    def _widened(self, start: int, end: int) -> tuple[int, int]:
        """``[start, end)`` grown to swallow any image token it cuts into.

        An image or paste token goes as a whole, whichever key deletes it:
        half of one is a stray bracket, and what it stood for would be
        silently dropped.
        """
        start, end = sorted((start, end))
        for token_start, token_end in self._token_spans():
            if token_start < end and token_end > start:
                start, end = min(start, token_start), max(end, token_end)
        return start, end

    def _token_spans(self) -> list[tuple[int, int]]:
        text = self.text
        spans = [m.span() for m in IMAGE_TOKEN.finditer(text)]
        spans += [m.span() for m in PASTE_TOKEN.finditer(text) if int(m.group(1)) in self._pastes]
        return spans

    def _snap_cursor(self, *, forward: bool) -> None:
        """Keep the cursor out of the middle of a token.

        A character typed inside one would leave a token that no longer
        matches - an ordinary bracketed string, with the image or the paste it
        stood for quietly gone. So the cursor steps over a token as if it were
        one character, landing on the side it was heading for.
        """
        cursor = self.buffer.cursor
        for start, end in self._token_spans():
            if start < cursor < end:
                self.buffer.cursor = end if forward else start
                return

    def _request_image(self, paths: list[Path] | None) -> None:
        if self.on_image_request is not None:
            self.on_image_request(paths)

    # -- kill ring ---------------------------------------------------------

    def _kill_to(self, target: int) -> None:
        """Delete between the cursor and ``target``, keeping the text on the ring."""
        killed = self.kill_range(*self._widened(self.buffer.cursor, target))
        if not killed:
            return
        self.kill_ring.kill(killed)
        self._last_yank = None

    def _yank(self) -> None:
        text = self.kill_ring.yank()
        if text is None:
            return
        start = self.buffer.cursor
        self.insert(text)
        self._last_yank = (start, self.buffer.cursor, text)

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
        self.buffer.delete_range(start, end)
        self.buffer.cursor = start
        self.insert(text)
        self._last_yank = (start, self.buffer.cursor, text)

    def _current_yank(self) -> tuple[int, int, str] | None:
        """The last yank while it is still intact under the cursor, else None."""
        if self._last_yank is None:
            return None
        start, end, text = self._last_yank
        if self.buffer.cursor != end or self.buffer.get_range(start, end) != text:
            return None
        return self._last_yank

    # -- submit and history ------------------------------------------------

    def _submit(self) -> None:
        self.close_completion()
        text = self.text.strip()
        if not text:
            return
        # History keeps the draft as it was seen, tokens and all; what goes out
        # is the text the tokens stand for.
        self._remember(text)
        if self.on_submit is not None:
            self.on_submit(self.expand_pastes(text).strip(), self.images_in(text))

    def _steer(self) -> None:
        """Steer the draft, or - with nothing typed - whatever is queued."""
        self.close_completion()
        text = self.text.strip()
        if text:
            self._remember(text)
        if self.on_steer is not None:
            self.on_steer(self.expand_pastes(text).strip(), self.images_in(text))

    def _remember(self, text: str) -> None:
        """File the sent text in history and clear the buffer for the next one."""
        self._history.append(text)
        del self._history[:-MAX_HISTORY]
        self._history_index = None
        self._draft = ""
        self.clear()

    def history_prev(self) -> None:
        if not self._history:
            return
        if self._history_index is None:
            self._draft = self.text
            self._history_index = len(self._history)
        self._history_index = max(0, self._history_index - 1)
        self.text = self._history[self._history_index]

    def history_next(self) -> None:
        if self._history_index is None:
            return
        self._history_index += 1
        if self._history_index >= len(self._history):
            self._history_index = None
            self.text = self._draft
        else:
            self.text = self._history[self._history_index]

    # -- completion --------------------------------------------------------

    def _completion_key(self, key: str, bound: Any) -> bool:
        """Keys the open completion owns."""
        completion = self.completion
        if completion is None:
            return False
        if key in ("up", "down"):
            completion.move(-1 if key == "up" else 1)
            self._sync_completions()
            return True
        if bound("tui.input.submit"):
            # Once the prompt already contains the highlighted completion,
            # Enter means submit. Treating it as another acceptance made exact
            # slash commands such as /clear need a surprising second Enter.
            candidate = completion.current
            if candidate is not None and self.text == completion.prefix + candidate.value:
                self._submit()
            else:
                self.accept_completion()
            return True
        if bound("tui.input.complete"):
            self.accept_completion()
            return True
        if bound("app.interrupt"):
            self.close_completion()
            return True
        return False

    def complete(self) -> None:
        """Open the completion for the token under the cursor, or advance it.

        Tab with one already open cycles, which makes the key work the same
        whether or not the user has looked at the list yet.
        """
        if self.completion is not None:
            self.completion.move(1)
            self._sync_completions()
            return
        self._completion_dismissed = False
        self.completion = self._build_completion()
        self._sync_completions()

    def _sync_completion(self) -> None:
        """Open, refilter or close for whatever is under the cursor.

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
        self._sync_completions()

    def close_completion(self) -> None:
        self._completion_dismissed = self.completion is not None
        self.completion = None
        self._sync_completions()

    def accept_completion(self) -> None:
        completion = self.completion
        candidate = completion.current if completion else None
        if completion is None or candidate is None:
            return
        # Splice, never truncate: the token being completed runs from the
        # completion's start to the cursor, and everything after the cursor is
        # the rest of the user's draft. Rebuilding from the prefix alone threw
        # that away - "@sr and fix the bug" became "@src/".
        text = self.text
        replacement = completion.prefix + candidate.value
        end = completion.start + len(replacement)
        self.buffer.set(text[: completion.start] + replacement + text[self.buffer.cursor :], end)
        self.completion = None
        self._completion_dismissed = False
        self.invalidate()
        self._sync_completions()

    def _build_completion(self) -> Completion | None:
        text = self.text[: self.buffer.cursor]
        if text.startswith("/") and "\n" not in text and " " not in text:
            return self._command_completion(text)

        at = text.rfind("@")
        if at != -1 and not any(char.isspace() for char in text[at + 1 :]):
            return self._path_completion(text, at)
        return None

    def _command_completion(self, text: str) -> Completion | None:
        if self.commands is None:
            return None
        query = text[1:]
        # Name only. Matching summaries here turns "/co" into half the list,
        # because some other command's summary contains a c before an o; the
        # palette is where searching descriptions belongs.
        matches = filter_items(self.commands.all(), query, key=lambda command: command.name)
        candidates = [
            Candidate(value=command.name, label=f"/{command.name}", detail=command.summary)
            for command in matches
        ]
        return Completion(start=0, prefix="/", candidates=candidates) if candidates else None

    def _path_completion(self, text: str, at: int) -> Completion | None:
        # A filename is bytes off a disk, not something HX wrote.
        matches = [plain_text(path) for path in self.completer.complete(text[at + 1 :])]
        candidates = [Candidate(value=path, label=path) for path in matches]
        return Completion(start=at, prefix="@", candidates=candidates) if candidates else None

    def _sync_completions(self) -> None:
        """Draw the completion list inside the editor's own line array.

        Part of the same component, so the list cannot end up positioned
        somewhere other than against the text it is completing. Above it: the
        prompt is on the bottom row of the screen, and a list below it would
        shove the prompt and the status bar up by one row per match as the
        query narrows.
        """
        completion = self.completion
        if completion is None or not completion.candidates:
            self.set_completions([])
            return

        total = len(completion.candidates)
        start = 0
        if total > LIST_VISIBLE:
            half = LIST_VISIBLE // 2
            start = max(0, min(completion.index - half, total - LIST_VISIBLE))
        window = completion.candidates[start : start + LIST_VISIBLE]

        rows = columns([[candidate.label, candidate.detail] for candidate in window])
        lines = []
        for offset, row in enumerate(rows):
            chosen = start + offset == completion.index
            marker = fg("accent", CURSOR) if chosen else GUTTER
            lines.append(marker + fg("accent" if chosen else "text", row))
        if total > LIST_VISIBLE:
            lines.append(GUTTER + fg("muted", f"({completion.index + 1}/{total})"))
        self.set_completions(lines)


__all__ = [
    "IMAGE_TOKEN",
    "Candidate",
    "Completion",
    "FileCompleter",
    "Prompt",
    "placeholder_text",
]
