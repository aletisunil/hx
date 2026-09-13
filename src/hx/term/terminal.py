"""Owning the terminal: raw mode, the modes we turn on, and putting them back.

Everything in this module exists because of one asymmetry. Turning a terminal
mode on is a two-byte write that cannot fail. Turning it off again has to
happen on every path out of the process - a clean exit, an unhandled
exception, a signal, a crash in someone else's library - and if it does not,
the user is left in a shell with no echo, no line editing and an invisible
cursor, which reads as "the tool broke my terminal" and is fixed by typing
``reset`` blind.

A framework normally absorbs this. Since there is no framework here,
:meth:`ProcessTerminal.restore` is idempotent and is wired to ``atexit``, to
:data:`sys.excepthook` and to the fatal signals, so that every exit reaches it.

One path cannot be covered from inside the process: ``SIGKILL`` runs no
handler, by definition, and a session killed that way leaves the modes on.
:meth:`ProcessTerminal.start` therefore begins by turning everything off,
whether or not this process turned it on - so the first thing a new session
does is clean up after the last one, and the fix for a terminal left broken by
a kill is to run ``hx`` again.

Note what is *not* here: no alternate screen. Output stays in the terminal's
own scrollback, where the terminal can scroll it, select it and copy it, and
where it survives the process exiting.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import signal
import sys
import threading
from collections.abc import Callable
from types import FrameType, TracebackType
from typing import IO, Any, Protocol

DEFAULT_SIZE = (80, 24)
"""Used when the terminal will not say - a pipe, a CI log, a dead tty."""

# Modes HX turns on, each paired with the sequence that turns it off.
_BRACKETED_PASTE_ON, _BRACKETED_PASTE_OFF = "\x1b[?2004h", "\x1b[?2004l"
_MOUSE_ON, _MOUSE_OFF = "\x1b[?1000h\x1b[?1002h\x1b[?1006h", "\x1b[?1006l\x1b[?1002l\x1b[?1000l"
_CURSOR_HIDE, _CURSOR_SHOW = "\x1b[?25l", "\x1b[?25h"
_SGR_RESET = "\x1b[0m"


class Terminal(Protocol):
    """What the renderer needs from the outside world.

    Small on purpose: the fake implementation used by the tests is the same
    shape, so the differ can be driven and asserted on without a tty.
    """

    @property
    def size(self) -> tuple[int, int]:
        """Columns and rows."""
        ...

    def write(self, data: str) -> None: ...

    def start(self, on_input: Callable[[str], None], on_resize: Callable[[], None]) -> None: ...

    def stop(self) -> None: ...


class UnsupportedPlatform(RuntimeError):
    """Raised on a platform with no POSIX terminal control."""


class ProcessTerminal:
    """The real terminal this process is attached to."""

    def __init__(self, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> None:
        if sys.platform == "win32":  # pragma: no cover - refused before we get here
            raise UnsupportedPlatform(
                "HX's terminal renderer needs a POSIX terminal. On Windows, run it under WSL."
            )
        self._stdin: IO[str] = stdin or sys.stdin
        self._stdout: IO[str] = stdout or sys.stdout
        self._fd = self._stdin.fileno()

        self._saved_attributes: list[Any] | None = None
        self._entered = False
        self._restored = False
        self._lock = threading.Lock()

        self._on_resize: Callable[[], None] | None = None
        self._previous_excepthook: Callable[..., Any] | None = None
        self._previous_signals: dict[int, Any] = {}
        self._mouse = False

    # -- geometry ----------------------------------------------------------

    @property
    def size(self) -> tuple[int, int]:
        try:
            columns, rows = os.get_terminal_size(self._fd)
        except OSError:
            return DEFAULT_SIZE
        # A zero here is a terminal mid-resize or a pty that has not been sized
        # yet. Rendering into zero columns would divide by it.
        return (columns or DEFAULT_SIZE[0], rows or DEFAULT_SIZE[1])

    # -- output ------------------------------------------------------------

    def write(self, data: str) -> None:
        if not data:
            return
        try:
            self._stdout.write(data)
            self._stdout.flush()
        except (BrokenPipeError, ValueError):
            # The far end went away. There is nothing useful to do, and raising
            # here would turn a closed pager into a traceback.
            pass

    # -- lifecycle ---------------------------------------------------------

    def start(self, on_input: Callable[[str], None], on_resize: Callable[[], None]) -> None:
        """Enter raw mode, enable our modes, and arm every path back out.

        The restore hooks are installed *before* the modes are turned on, so a
        failure part-way through enabling still leaves something that will put
        the terminal back.
        """
        import termios
        import tty

        self._on_resize = on_resize
        self._install_safety_net()
        self._sanitize()

        if os.isatty(self._fd):
            self._saved_attributes = termios.tcgetattr(self._fd)
            tty.setraw(self._fd)
        self._entered = True
        self._restored = False

        self.write(_BRACKETED_PASTE_ON + _CURSOR_HIDE)
        self._install_resize_handler()
        self._attach_reader(on_input)

    def stop(self) -> None:
        self._detach_reader()
        self.restore()

    def _sanitize(self) -> None:
        """Turn off every mode HX can turn on, before turning any of them on.

        Costs one write at startup and is the only available remedy for a
        previous session that was killed outright.
        """
        self.write(_MOUSE_OFF + _BRACKETED_PASTE_OFF + _SGR_RESET + _CURSOR_SHOW)

    def restore(self) -> None:
        """Put the terminal back exactly as it was found. Safe to call twice.

        Called from ``atexit``, from the exception hook and from signal
        handlers, so it must not raise and must not depend on the event loop
        still running.
        """
        with self._lock:
            if self._restored or not self._entered:
                self._restored = True
                return
            self._restored = True

        # Order matters: turn the modes off while still in raw mode, then hand
        # the line discipline back.
        parts = [_MOUSE_OFF if self._mouse else "", _BRACKETED_PASTE_OFF, _SGR_RESET, _CURSOR_SHOW]
        with contextlib.suppress(Exception):  # best effort by definition
            self.write("".join(parts))

        if self._saved_attributes is not None:
            with contextlib.suppress(Exception):
                import termios

                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved_attributes)
            self._saved_attributes = None

        self._remove_safety_net()

    # -- optional modes ----------------------------------------------------

    def set_mouse(self, enabled: bool) -> None:
        """Mouse reporting on or off.

        Off by default, because while it is on the terminal's own
        click-to-select stops working - and in a UI whose transcript is real
        scrollback, the terminal's selection is the feature.
        """
        if enabled == self._mouse:
            return
        self._mouse = enabled
        self.write(_MOUSE_ON if enabled else _MOUSE_OFF)

    def park_cursor_below(self, rows_down: int = 0) -> None:
        """Leave the cursor under the last line drawn, on its own row.

        So the shell prompt lands after HX's output instead of on top of it.
        """
        self.write("\r" + ("\n" * rows_down) + _CURSOR_SHOW)

    # -- the safety net ----------------------------------------------------

    def _install_safety_net(self) -> None:
        atexit.register(self.restore)

        self._previous_excepthook = sys.excepthook

        def hook(
            exc_type: type[BaseException],
            exc: BaseException,
            traceback: TracebackType | None,
        ) -> None:
            self.restore()
            assert self._previous_excepthook is not None
            self._previous_excepthook(exc_type, exc, traceback)

        sys.excepthook = hook

        # SIGTERM and SIGHUP otherwise kill the process with the modes still
        # on. SIGINT is left alone: the app handles it as "interrupt the turn".
        for number in (signal.SIGTERM, signal.SIGHUP):
            try:
                self._previous_signals[number] = signal.getsignal(number)
                signal.signal(number, self._on_fatal_signal)
            except (ValueError, OSError):  # pragma: no cover - not the main thread
                pass

    def _on_fatal_signal(self, number: int, frame: FrameType | None) -> None:
        self.restore()
        previous = self._previous_signals.get(number)
        if callable(previous):
            previous(number, frame)
        else:
            signal.signal(number, signal.SIG_DFL)
            os.kill(os.getpid(), number)

    def _remove_safety_net(self) -> None:
        with contextlib.suppress(Exception):
            atexit.unregister(self.restore)
        if self._previous_excepthook is not None:
            sys.excepthook = self._previous_excepthook
            self._previous_excepthook = None
        for number, previous in self._previous_signals.items():
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(number, previous)
        self._previous_signals.clear()

    # -- resize ------------------------------------------------------------

    def _install_resize_handler(self) -> None:
        def handler(_number: int, _frame: FrameType | None) -> None:
            if self._on_resize is not None:
                self._on_resize()

        try:
            signal.signal(signal.SIGWINCH, handler)
            # SIGWINCH is not delivered while the process is stopped, so a
            # resize during ctrl+z is only discoverable on the way back.
            signal.signal(signal.SIGCONT, handler)
        except (ValueError, OSError):  # pragma: no cover - not the main thread
            pass

    # -- input -------------------------------------------------------------

    def _attach_reader(self, on_input: Callable[[str], None]) -> None:
        import asyncio

        def ready() -> None:
            try:
                data = os.read(self._fd, 65536)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            if data:
                on_input(data.decode("utf-8", errors="replace"))

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - started outside a loop
            return
        loop.add_reader(self._fd, ready)

    def _detach_reader(self) -> None:
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        with contextlib.suppress(Exception):
            loop.remove_reader(self._fd)


class FakeTerminal:
    """A terminal that records instead of drawing.

    Everything the renderer emits lands in :attr:`written`, so a test can feed
    it to a VT emulator and assert on what a real terminal would have shown -
    rather than on the escape sequences, which is testing the implementation.
    """

    def __init__(self, columns: int = 80, rows: int = 24) -> None:
        self._size = (columns, rows)
        self.written: list[str] = []
        self.restored = False
        self.on_input: Callable[[str], None] | None = None
        self.on_resize: Callable[[], None] | None = None

    @property
    def size(self) -> tuple[int, int]:
        return self._size

    @property
    def output(self) -> str:
        return "".join(self.written)

    def clear_output(self) -> None:
        self.written.clear()

    def write(self, data: str) -> None:
        self.written.append(data)

    def start(self, on_input: Callable[[str], None], on_resize: Callable[[], None]) -> None:
        self.on_input = on_input
        self.on_resize = on_resize

    def stop(self) -> None:
        self.restored = True

    def resize(self, columns: int, rows: int) -> None:
        self._size = (columns, rows)
        if self.on_resize is not None:
            self.on_resize()

    def feed(self, data: str) -> None:
        if self.on_input is not None:
            self.on_input(data)
