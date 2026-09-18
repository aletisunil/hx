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

The default is *not* the alternate screen. Output stays in the terminal's own
scrollback, where the terminal can scroll it, select it and copy it, and where
it survives the process exiting. ``/fullscreen`` turns the alternate screen on
for anyone who wants the window back - and because a shell left on the
alternate screen shows an empty rectangle with no way back, leaving it is wired
into :meth:`ProcessTerminal.restore` alongside every other mode, and into
:meth:`ProcessTerminal._sanitize` so a session killed in fullscreen is cleaned
up by the next start - with ``?1047l`` rather than ``?1049l``, because startup
is the one place the cursor is not ours to move. See :data:`_ALT_SCREEN_LEAVE`.
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
_ALT_SCREEN_ON, _ALT_SCREEN_OFF = "\x1b[?1049h", "\x1b[?1049l"
_ALT_SCREEN_LEAVE = "\x1b[?1047l"
"""Leave the alternate screen *without* restoring a cursor.

``?1049l`` is the right way out of a screen this process entered, because the
matching ``?1049h`` saved the cursor and leaving puts it back. It is the wrong
way to clean up after somebody else: it is specified as DECRC, and a terminal
asked to restore a cursor that was never saved homes it. That would put the
cursor at the top-left of a window still holding the user's shell output, and
:mod:`hx.term.screen` draws relative to wherever the cursor is - so the first
frame would land on top of their scrollback.

``?1047l`` is the same switch with no cursor in it, and a no-op when the
terminal is already on the normal screen, which is the case every time HX
starts after a session that exited properly."""
_SGR_RESET = "\x1b[0m"

_RESIZE_SIGNALS = (signal.SIGWINCH, signal.SIGCONT)
"""SIGWINCH is not delivered while the process is stopped, so a resize during
``ctrl+z`` is only discoverable on the way back - which is what SIGCONT is
doing in a list of resize signals."""


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

    def set_alt_screen(self, enabled: bool) -> None: ...

    def suspend(self) -> None: ...


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
        self._resize_loop: Any = None
        self._resize_signals: list[int] = []
        self._resize_pending = False
        self._delivering_resize = False
        self._previous_excepthook: Callable[..., Any] | None = None
        self._previous_signals: dict[int, Any] = {}
        self._mouse = False
        self._alt_screen = False
        self._on_input: Callable[[str], None] | None = None

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
        # Only ever set by the fallback resize handler, which flags rather than
        # draws; see :meth:`_install_resize_handler`. Delivering it here means
        # the frame that the signal interrupted finishes first.
        if self._resize_pending:
            self._deliver_resize()

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
        self._on_input = on_input
        self._attach_reader(on_input)

    def stop(self) -> None:
        self._detach_reader()
        self.restore()

    def _sanitize(self) -> None:
        """Turn off every mode HX can turn on, before turning any of them on.

        Costs one write at startup and is the only available remedy for a
        previous session that was killed outright.
        """
        self.write(
            _ALT_SCREEN_LEAVE + _MOUSE_OFF + _BRACKETED_PASTE_OFF + _SGR_RESET + _CURSOR_SHOW
        )

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
        parts = [
            _ALT_SCREEN_OFF if self._alt_screen else "",
            _MOUSE_OFF if self._mouse else "",
            _BRACKETED_PASTE_OFF,
            _SGR_RESET,
            _CURSOR_SHOW,
        ]
        with contextlib.suppress(Exception):  # best effort by definition
            self.write("".join(parts))

        if self._saved_attributes is not None:
            with contextlib.suppress(Exception):
                import termios

                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved_attributes)
            self._saved_attributes = None

        self._remove_resize_handlers()
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

    def set_alt_screen(self, enabled: bool) -> None:
        """The alternate screen on or off.

        On it, the terminal keeps a second buffer with no scrollback of its
        own: the UI owns a fixed rectangle and gives the user's shell back
        untouched when it leaves. That is the old full-window feel, and the
        cost is the thing the default was chosen for - the transcript stops
        being scrollback the terminal can scroll, select and keep.
        """
        if enabled == self._alt_screen:
            return
        self._alt_screen = enabled
        self.write(_ALT_SCREEN_ON if enabled else _ALT_SCREEN_OFF)

    @property
    def alt_screen(self) -> bool:
        return self._alt_screen

    def suspend(self) -> None:
        """Hand the terminal back, stop this process, and take it again.

        ``ctrl+z`` in a raw-mode application cannot be left to the line
        discipline: raw mode is exactly what stops the terminal turning it into
        a signal. So the key is decoded like any other and lands here, and this
        has to do by hand what the shell would otherwise have done for free -
        put the modes back, stop, and on the way back in re-enter raw mode and
        turn them on again.

        Returns once the process has been continued, with the terminal in the
        state it had before. The caller repaints: the screen belongs to
        whatever the user did in the shell in between.
        """
        if not self._entered:  # pragma: no cover - suspend before start
            return
        import termios
        import tty

        alt = self._alt_screen
        self._detach_reader()
        self.write(
            (_ALT_SCREEN_OFF if alt else "")
            + (_MOUSE_OFF if self._mouse else "")
            + _BRACKETED_PASTE_OFF
            + _SGR_RESET
            + _CURSOR_SHOW
        )
        if self._saved_attributes is not None:
            with contextlib.suppress(Exception):
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved_attributes)

        os.kill(os.getpid(), signal.SIGTSTP)

        # Continued. Everything turned off above has to come back on.
        if os.isatty(self._fd):
            with contextlib.suppress(Exception):
                tty.setraw(self._fd)
        self.write(
            (_ALT_SCREEN_ON if alt else "")
            + (_MOUSE_ON if self._mouse else "")
            + _BRACKETED_PASTE_ON
            + _CURSOR_HIDE
        )
        if self._on_input is not None:
            self._attach_reader(self._on_input)

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
        """Hear about resizes without redrawing from inside the signal frame.

        A signal handler runs between two bytecodes of whatever the main thread
        happened to be doing, and for a terminal app that is usually the middle
        of writing a frame. Redrawing from in there re-enters the writer while
        it still holds its buffer, and Python refuses:
        ``RuntimeError: reentrant call inside <_io.BufferedWriter>``. A double
        click on the window - one resize, arriving during one write - was
        enough to end a session that way.

        So the redraw is handed to the event loop, which runs it as an ordinary
        callback between frames. ``add_signal_handler`` exists for exactly this
        and delivers through asyncio's self-pipe.
        """
        loop = self._current_loop()
        if loop is not None and self._add_loop_handlers(loop):
            return

        def handler(_number: int, _frame: FrameType | None) -> None:
            # No loop to defer to: flag it and let the next write deliver it,
            # which is the earliest moment this can be done safely.
            self._resize_pending = True

        for number in _RESIZE_SIGNALS:
            with contextlib.suppress(ValueError, OSError):  # not the main thread
                signal.signal(number, handler)

    @staticmethod
    def _current_loop() -> Any:
        import asyncio

        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def _add_loop_handlers(self, loop: Any) -> bool:
        """Route the resize signals through ``loop``. False if it will not take them."""
        installed: list[int] = []
        for number in _RESIZE_SIGNALS:
            try:
                loop.add_signal_handler(number, self._deliver_resize)
            except (ValueError, OSError, NotImplementedError, RuntimeError):
                continue
            installed.append(number)
        if not installed:
            return False
        self._resize_loop = loop
        self._resize_signals = installed
        return True

    def _remove_resize_handlers(self) -> None:
        loop, self._resize_loop = self._resize_loop, None
        signals, self._resize_signals = self._resize_signals, []
        if loop is None:
            return
        for number in signals:
            with contextlib.suppress(Exception):  # a closed loop is not worth raising over
                loop.remove_signal_handler(number)

    def _deliver_resize(self) -> None:
        """Tell the app the size changed. Never called from a signal frame."""
        self._resize_pending = False
        if self._on_resize is None or self._delivering_resize:
            return
        self._delivering_resize = True
        try:
            self._on_resize()
        finally:
            self._delivering_resize = False

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
        self.alt_screen = False
        self.suspends = 0
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
        self.alt_screen = False

    def set_alt_screen(self, enabled: bool) -> None:
        if enabled == self.alt_screen:
            return
        self.alt_screen = enabled
        self.write("\x1b[?1049h" if enabled else "\x1b[?1049l")

    def suspend(self) -> None:
        """Counted rather than performed: a test must not stop pytest."""
        self.suspends += 1

    def resize(self, columns: int, rows: int) -> None:
        self._size = (columns, rows)
        if self.on_resize is not None:
            self.on_resize()

    def feed(self, data: str) -> None:
        if self.on_input is not None:
            self.on_input(data)
