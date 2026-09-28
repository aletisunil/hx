"""Driving the real ``hx`` binary through a pseudo-terminal.

The process gets a genuine controlling tty, so raw mode, SIGWINCH, bracketed
paste and the exit-time restore all run exactly as they do for a user. What it
paints is fed through ``pyte``, a VT emulator, so assertions are made against
the cells a terminal would show rather than against the bytes HX meant to
write.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import html
import os
import pty
import re
import signal
import struct
import termios
import threading
import time
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyte

KEYS: dict[str, str] = {
    "enter": "\r",
    "tab": "\t",
    "shift+tab": "\x1b[Z",
    "esc": "\x1b",
    "backspace": "\x7f",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "home": "\x1b[H",
    "end": "\x1b[F",
    "pgup": "\x1b[5~",
    "pgdn": "\x1b[6~",
    "ctrl+up": "\x1b[1;5A",
    "ctrl+down": "\x1b[1;5B",
    "ctrl+home": "\x1b[1;5H",
    "ctrl+end": "\x1b[1;5F",
    "alt+enter": "\x1b\r",
    # A legacy terminal has no code for shift+enter: it sends Enter. That is
    # the whole reason the keyboard protocols exist.
    "shift+enter": "\r",
    "ctrl+shift+z": "\x1a",
    "alt+b": "\x1bb",
    "alt+f": "\x1bf",
    "alt+d": "\x1bd",
    "alt+y": "\x1by",
    "ctrl+_": "\x1f",
    **{f"ctrl+{c}": chr(ord(c) - ord("a") + 1) for c in "abcdefghijklnopqrstuvwxyz"},
}
"""What a legacy terminal sends for each key. ``ctrl+m`` is omitted: it is ``enter``."""

_KITTY: dict[str, str] = {
    "shift+enter": "\x1b[13;2u",
    "alt+enter": "\x1b[13;3u",
    "shift+tab": "\x1b[9;2u",
    "esc": "\x1b[27u",
    # ctrl+_ is ctrl+shift+minus on a US keyboard: the key, its shifted
    # character (flag 4, alternate keys), and ctrl+shift.
    "ctrl+_": "\x1b[45:95;6u",
    "ctrl+shift+z": "\x1b[122:90;6u",
    # cmd is the protocol's super. Only a terminal that leaves cmd to the
    # program sends these (kitty, WezTerm); the rest keep it for their menus.
    "cmd+shift+z": "\x1b[122:90;10u",
    **{f"cmd+{c}": f"\x1b[{ord(c)};9u" for c in "abcdefghijklmnopqrstuvwxyz"},
    **{f"alt+{c}": f"\x1b[{ord(c)};3u" for c in "bfdy"},
    **{f"ctrl+{c}": f"\x1b[{ord(c)};5u" for c in "abcdefghijklnopqrstuvwxyz"},
}
"""The kitty keyboard protocol with disambiguation on: every key that the
legacy encoding makes ambiguous gets a ``CSI u`` code of its own. Arrows and
the rest keep their legacy codes, as they do in kitty."""

_MODIFY_OTHER_KEYS: dict[str, str] = {
    "shift+enter": "\x1b[27;2;13~",
    "alt+enter": "\x1b[27;3;13~",
    "ctrl+_": "\x1b[27;6;95~",
    "ctrl+shift+z": "\x1b[27;6;90~",
    **{f"alt+{c}": f"\x1b[27;3;{ord(c)}~" for c in "bfdy"},
    **{f"ctrl+{c}": f"\x1b[27;5;{ord(c)}~" for c in "abcdefghijklnopqrstuvwxyz"},
}
"""xterm with ``modifyOtherKeys`` at level 2 - also how tmux passes modified
keys to a program that asked for them."""

KEYBOARDS = ("kitty", "xterm", "legacy")
"""What the emulated terminal can do with the keyboard.

``kitty`` speaks the kitty protocol (kitty, Ghostty, WezTerm, iTerm2, foot):
it keeps a stack of flags per screen and answers ``CSI ? u``. ``xterm`` has
``modifyOtherKeys`` instead. ``legacy`` has neither (Apple's Terminal). Each
sends the extended codes only once HX has asked for them, as the real ones do:
a test that always sent ``CSI 13;2u`` would pass against an HX that never
asked, which is exactly the bug it would be there to catch."""

_KEYBOARD_SEQUENCE = re.compile(
    rb"\x1b\[(?:>(\d*)u|<(\d*)u|\?u|c|>4;(\d+)m|>4m|\?(1049|1047|47)([hl]))"
)
_ESC_TAIL = re.compile(rb"\x1b(?:\[[0-?]*)?$")


class _Screen(pyte.HistoryScreen):
    """A VT screen that answers the terminal's own queries, as a real one does."""

    def __init__(self, columns: int, lines: int, reply: Callable[[str], None]) -> None:
        super().__init__(columns, lines, history=20_000, ratio=0.5)
        self._reply = reply

    def write_process_input(self, data: str) -> None:
        self._reply(data)


class Terminal:
    """One ``hx`` process on a pty, with the screen it has drawn."""

    def __init__(
        self,
        argv: list[str],
        *,
        env: dict[str, str],
        cwd: Path,
        columns: int = 100,
        rows: int = 30,
        record: Callable[[str, Terminal], None] | None = None,
        keyboard: str = "kitty",
    ) -> None:
        assert keyboard in KEYBOARDS, keyboard
        self.argv = argv
        self.keyboard = keyboard
        self.kitty_stacks: dict[str, list[int]] = {"main": [], "alt": []}
        """The kitty flags pushed on each screen, as the terminal keeps them."""
        self.modify_other_keys = 0
        self.alt_screen = False
        self.keyboard_log: list[str] = []
        """Every keyboard-protocol request HX made, in order: the evidence for
        what it negotiated and that it put everything back."""
        self._carry = b""
        self.columns, self.rows = columns, rows
        self._lock = threading.Lock()
        self._output = threading.Condition(self._lock)
        self._record = record
        self.raw = bytearray()
        self._last_output = time.monotonic()
        self.exit_code: int | None = None

        with warnings.catch_warnings():
            # forkpty() in a threaded process: the child only chdirs, sizes
            # its tty and execs, touching no lock another thread could hold.
            warnings.simplefilter("ignore", DeprecationWarning)
            pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - the child
            try:
                os.chdir(cwd)
                _set_size(0, columns, rows)
                os.execve(argv[0], argv, env)
            finally:
                os._exit(127)
        self.pid, self.fd = pid, fd
        _set_size(fd, columns, rows)
        self.screen = _Screen(columns, rows, self._write)
        self.stream = pyte.ByteStream(self.screen)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    # -- output ----------------------------------------------------------

    def _read(self) -> None:
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.EBADF):
                    break
                raise
            if not data:
                break
            with self._output:
                self.raw.extend(data)
                self.stream.feed(self._keyboard_requests(data))
                self._last_output = time.monotonic()
                self._output.notify_all()
        with self._output:
            self._output.notify_all()

    def _keyboard_requests(self, data: bytes) -> bytes:
        """Act on HX's keyboard-protocol requests, as the emulated terminal.

        Answered here rather than by pyte, which knows neither protocol, and
        in order - a kitty terminal answers ``CSI ? u`` before the ``CSI c``
        sent after it, and HX relies on that order. The requests are removed
        from what pyte is given; they draw nothing.
        """
        data = self._carry + data
        tail = _ESC_TAIL.search(data)
        self._carry = data[tail.start() :] if tail else b""
        if tail:
            data = data[: tail.start()]

        def act(match: re.Match[bytes]) -> bytes:
            push, pop, level, screen, state = match.groups()
            text = match.group(0).decode()
            stack = self.kitty_stacks["alt" if self.alt_screen else "main"]
            if screen is not None:
                self.alt_screen = state == b"h"
                return match.group(0)  # pyte draws the screen switch
            self.keyboard_log.append(text.replace("\x1b", "ESC"))
            if text == "\x1b[c":
                self._write("\x1b[?62;22c")
            elif self.keyboard == "kitty" and push is not None:
                stack.append(int(push or 0))
            elif self.keyboard == "kitty" and pop is not None:
                del stack[len(stack) - min(len(stack), int(pop or 1)) :]
            elif self.keyboard == "kitty" and text == "\x1b[?u":
                self._write(f"\x1b[?{stack[-1] if stack else 0}u")
            elif self.keyboard == "xterm" and (level is not None or text == "\x1b[>4m"):
                self.modify_other_keys = int(level or 0)
            return b""

        return _KEYBOARD_SEQUENCE.sub(act, data)

    @property
    def kitty_flags(self) -> int:
        """The flags in force on the current screen."""
        stack = self.kitty_stacks["alt" if self.alt_screen else "main"]
        return stack[-1] if stack else 0

    def encode(self, key: str) -> str:
        """What this terminal sends for ``key``, given what HX has asked for."""
        if self.kitty_flags & 1 and key in _KITTY:
            return _KITTY[key]
        if self.modify_other_keys >= 2 and key in _MODIFY_OTHER_KEYS:
            return _MODIFY_OTHER_KEYS[key]
        return KEYS[key]

    def text(self) -> str:
        """The visible screen, one line per row, trailing spaces trimmed."""
        with self._lock:
            return "\n".join(line.rstrip() for line in self.screen.display)

    def lines(self) -> list[str]:
        return self.text().split("\n")

    def scrollback(self) -> str:
        """Everything that scrolled off the top, then the visible screen."""
        with self._lock:
            top = [
                "".join(line[x].data for x in range(self.columns)).rstrip()
                for line in self.screen.history.top
            ]
        return "\n".join([*top, self.text()])

    def cursor(self) -> tuple[int, int]:
        with self._lock:
            return self.screen.cursor.x, self.screen.cursor.y

    def wait_for(
        self,
        expected: str | Callable[[str], bool],
        *,
        timeout: float = 10.0,
        where: str = "screen",
    ) -> str:
        """Block until the screen shows ``expected`` (or the predicate holds)."""
        test = expected if callable(expected) else (lambda screen: expected in screen)
        read = self.text if where == "screen" else self.scrollback
        deadline = time.monotonic() + timeout
        while True:
            screen = read()
            if test(screen):
                # Matched mid-paint is not matched: a frame is often drawn in
                # more than one write, so look again once HX has gone quiet.
                self.settle(quiet=0.08, timeout=2)
                screen = read()
                if test(screen):
                    return screen
                continue
            if time.monotonic() > deadline or (not self.alive and self._drained()):
                self.snapshot(f"timed out waiting for {expected!r}")
                raise AssertionError(
                    f"timed out after {timeout}s waiting for {expected!r}. Screen:\n{screen}"
                )
            with self._output:
                self._output.wait(0.05)

    def wait_gone(self, text: str, *, timeout: float = 10.0) -> str:
        return self.wait_for(lambda screen: text not in screen, timeout=timeout)

    def settle(self, quiet: float = 0.25, timeout: float = 10.0) -> str:
        """Wait until HX has stopped painting for ``quiet`` seconds."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                idle = time.monotonic() - self._last_output
            if idle >= quiet:
                return self.text()
            time.sleep(min(quiet - idle, 0.05) or 0.01)
        return self.text()

    def _drained(self) -> bool:
        return not self._reader.is_alive()

    # -- input -----------------------------------------------------------

    def _write(self, data: str) -> None:
        with contextlib.suppress(OSError):
            os.write(self.fd, data.encode())

    def type(self, text: str) -> None:
        """Type ``text`` one character at a time, as a person would."""
        for char in text:
            self._write(char)
            time.sleep(0.002)

    def paste(self, text: str, *, newline: str = "\r") -> None:
        """A bracketed paste - what a terminal sends for cmd+v or a drag.

        Line breaks go as carriage returns by default, because that is what
        iTerm2, Apple's Terminal, xterm and tmux send: a paste is delivered as
        if typed, and Enter types CR.
        """
        self._write(f"\x1b[200~{text.replace(chr(10), newline)}\x1b[201~")

    def press(self, *keys: str) -> None:
        for key in keys:
            self._write(self.encode(key))
            # A lone ESC is only known to be ESC once nothing follows it.
            time.sleep(0.12 if key == "esc" else 0.02)

    def submit(self, text: str) -> None:
        self.type(text)
        self.press("enter")

    def resize(self, columns: int, rows: int) -> None:
        with self._lock:
            self.columns, self.rows = columns, rows
            self.screen.resize(rows, columns)
        _set_size(self.fd, columns, rows)
        with contextlib.suppress(ProcessLookupError):
            os.kill(self.pid, signal.SIGWINCH)

    # -- lifetime --------------------------------------------------------

    @property
    def alive(self) -> bool:
        if self.exit_code is not None:
            return False
        pid, status = os.waitpid(self.pid, os.WNOHANG)
        if pid == 0:
            return True
        self.exit_code = os.waitstatus_to_exitcode(status)
        return False

    def wait_exit(self, timeout: float = 10.0) -> int:
        deadline = time.monotonic() + timeout
        while self.alive:
            if time.monotonic() > deadline:
                raise AssertionError(f"hx did not exit within {timeout}s. Screen:\n{self.text()}")
            time.sleep(0.05)
        self._reader.join(2)
        assert self.exit_code is not None
        return self.exit_code

    def close(self) -> None:
        if self.alive:
            with contextlib.suppress(ProcessLookupError):
                os.kill(self.pid, signal.SIGTERM)
            with contextlib.suppress(AssertionError):
                self.wait_exit(3)
        if self.alive:
            with contextlib.suppress(ProcessLookupError):
                os.kill(self.pid, signal.SIGKILL)
            self.wait_exit(3)
        with contextlib.suppress(OSError):
            os.close(self.fd)

    # -- the report ------------------------------------------------------

    def snapshot(self, label: str) -> None:
        """Record what the screen shows now, for the run's report."""
        if self._record is not None:
            self._record(label, self)

    def html(self) -> str:
        """The screen as coloured HTML cells, for the report."""
        with self._lock:
            rows = [_row_html(self.screen.buffer[y], self.columns) for y in range(self.rows)]
            cursor = self.screen.cursor
            hidden = cursor.hidden
            cx, cy = cursor.x, cursor.y
        marker = "" if hidden else f'<div class="cursor" style="--x:{cx};--y:{cy}"></div>'
        body = "\n".join(rows)
        return f'<pre class="term" style="--cols:{self.columns}">{body}{marker}</pre>'


def _set_size(fd: int, columns: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))


_NAMED = {
    "black": "#1e1e1e",
    "red": "#e06c75",
    "green": "#98c379",
    "brown": "#d19a66",
    "yellow": "#d19a66",
    "blue": "#61afef",
    "magenta": "#c678dd",
    "cyan": "#56b6c2",
    "white": "#dcdfe4",
    "brightblack": "#5c6370",
    "brightred": "#ff7a85",
    "brightgreen": "#b5e890",
    "brightyellow": "#f0c674",
    "brightblue": "#80c8ff",
    "brightmagenta": "#e0a0ff",
    "brightcyan": "#7fdbe6",
    "brightwhite": "#ffffff",
}


def _colour(value: str, default: str) -> str:
    if value == "default":
        return default
    if value in _NAMED:
        return _NAMED[value]
    if len(value) == 6:
        return f"#{value}"
    return default


def _row_html(line: Any, columns: int) -> str:
    out: list[str] = []
    run: list[str] = []
    style = ""

    def flush() -> None:
        if run:
            text = html.escape("".join(run))
            out.append(f'<span style="{style}">{text}</span>' if style else text)
            run.clear()

    for x in range(columns):
        char = line[x]
        fg = _colour(char.fg, "var(--fg)")
        bg = _colour(char.bg, "transparent")
        if char.reverse:
            fg, bg = (
                (bg if bg != "transparent" else "var(--bg)"),
                (fg if fg != "var(--fg)" else "var(--fg)"),
            )
        parts = []
        if fg != "var(--fg)":
            parts.append(f"color:{fg}")
        if bg != "transparent":
            parts.append(f"background:{bg}")
        if char.bold:
            parts.append("font-weight:700")
        if char.italics:
            parts.append("font-style:italic")
        if char.underscore:
            parts.append("text-decoration:underline")
        if char.strikethrough:
            parts.append("text-decoration:line-through")
        cell_style = ";".join(parts)
        if cell_style != style:
            flush()
            style = cell_style
        run.append(char.data or " ")
    flush()
    return "".join(out)
