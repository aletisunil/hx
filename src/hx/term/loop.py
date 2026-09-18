"""When to draw, and where keys go.

Two scheduling rules, and the difference between them is most of how the UI
feels.

**Keystrokes redraw immediately.** A character that appears 30ms after it was
typed reads as lag, and no amount of throughput makes up for it.

**Everything else is coalesced.** A model streaming tokens can invalidate the
document hundreds of times a second, and drawing every one of those would
spend the whole frame budget on text nobody can read at that rate. They are
collapsed into one frame at :data:`MAX_FRAMES_PER_SECOND`.

The other job here is the Escape key. A terminal sends ``\\x1b`` both for the
key and as the first byte of every arrow, so the decoder holds a trailing
Escape back rather than guessing. Something has to decide it was really the
key, and that decision is a timer - long enough that a slow arrow arrives
intact, short enough that Escape does not feel stuck.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

from hx.term.component import Component
from hx.term.keydecode import Decoder, Key
from hx.term.screen import MainScreen
from hx.term.terminal import ProcessTerminal, Terminal

MAX_FRAMES_PER_SECOND = 30
MIN_RENDER_INTERVAL = 1 / MAX_FRAMES_PER_SECOND

ESCAPE_TIMEOUT = 0.03
"""How long a lone Escape is held before being taken at face value.

Long enough for the rest of an arrow key to arrive over a slow link, short
enough that the key a user presses to get out of things does not feel stuck.
"""

KeyHandler = Callable[[Key], None]


class TuiRunner:
    """Owns the terminal, the render schedule, and key dispatch."""

    def __init__(
        self,
        root: Component,
        terminal: Terminal | None = None,
        *,
        on_key: KeyHandler | None = None,
        fullscreen: bool = False,
    ) -> None:
        self._terminal = terminal or ProcessTerminal()
        self._root = root
        self._screen = MainScreen(self._terminal, root)
        self._decoder = Decoder()
        self._on_key = on_key

        self._running = False
        self._render_pending = False
        self._drawing = False
        self._redraw_after_draw = False
        self._resize_pending = False
        self._render_task: asyncio.Task[None] | None = None
        self._escape_task: asyncio.Task[None] | None = None
        self._last_render = 0.0
        self._stopped: asyncio.Event | None = None
        self._start_fullscreen = fullscreen
        """Applied once the terminal is in raw mode - a session configured for
        the alternate screen has to enter it after the terminal is ours, not
        while it still belongs to the shell."""

    @property
    def screen(self) -> MainScreen:
        return self._screen

    @property
    def terminal(self) -> Terminal:
        return self._terminal

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        """Draw until :meth:`stop` is called, then hand the terminal back."""
        self._stopped = asyncio.Event()
        self._running = True
        self._terminal.start(self._on_input, self._on_resize)
        try:
            if self._start_fullscreen:
                self._screen.set_fullscreen(True)
            self.request_immediate_render()
            await self._stopped.wait()
        finally:
            self._running = False
            await self._cancel_tasks()
            with contextlib.suppress(Exception):
                self._screen.close()
            self._terminal.stop()

    def stop(self) -> None:
        if self._stopped is not None:
            self._stopped.set()

    # -- the two screens ---------------------------------------------------

    @property
    def fullscreen(self) -> bool:
        return self._screen.fullscreen

    def set_fullscreen(self, enabled: bool) -> None:
        """Draw into the alternate screen, or back into the scrollback."""
        if enabled == self._screen.fullscreen:
            return
        self._screen.set_fullscreen(enabled)
        if not enabled:
            # Coming back, the normal screen holds whatever it held before the
            # alternate screen covered it - including this session's own
            # transcript up to the moment it left. Redrawing the document under
            # that would print it twice, so the screen is cleared first.
            #
            # The screen only: the scrollback above it is the user's, and some
            # of it predates hx. Erasing that to tidy up after a mode switch
            # would throw away what they had in the terminal before they ran
            # us, which no command they typed asked for.
            self._terminal.write("\x1b[2J\x1b[H")
        self.request_immediate_render()

    def scroll(self, rows: int) -> None:
        """Scroll the fullscreen viewport. A no-op on the normal screen."""
        if self._screen.scroll_by(rows):
            self.request_immediate_render()

    def scroll_to_top(self) -> None:
        if self._screen.scroll_to_top():
            self.request_immediate_render()

    def scroll_to_bottom(self) -> None:
        if self._screen.scroll_to_bottom():
            self.request_immediate_render()

    def clear_screen(self) -> None:
        """Wipe the screen and the scrollback, then draw the document again."""
        self._screen.clear()

    def suspend(self) -> None:
        """``ctrl+z``: give the terminal back until the shell resumes us.

        The screen belongs to the shell while we are stopped, so what is on it
        afterwards is unknown and the document is repainted from scratch.
        """
        suspend = getattr(self._terminal, "suspend", None)
        if suspend is None:  # pragma: no cover - a terminal that cannot stop
            return
        suspend()
        self._screen.invalidate()
        self.request_immediate_render()

    async def _cancel_tasks(self) -> None:
        for task in (self._render_task, self._escape_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._render_task = None
        self._escape_task = None

    # -- rendering ---------------------------------------------------------

    def request_render(self) -> None:
        """Draw soon, collapsing everything else asked for in the meantime."""
        if not self._running or self._render_pending:
            return
        self._render_pending = True
        self._render_task = asyncio.ensure_future(self._render_after_delay())

    def request_immediate_render(self) -> None:
        """Draw on the next tick, ahead of the throttle.

        For anything the user just did. A keystroke that waits for the frame
        clock is a keystroke that feels slow.
        """
        if not self._running:
            return
        self._render_pending = False
        if self._render_task is not None and not self._render_task.done():
            self._render_task.cancel()
        self._draw()

    async def _render_after_delay(self) -> None:
        loop = asyncio.get_running_loop()
        elapsed = loop.time() - self._last_render
        if elapsed < MIN_RENDER_INTERVAL:
            await asyncio.sleep(MIN_RENDER_INTERVAL - elapsed)
        if self._render_pending and self._running:
            self._draw()

    def _draw(self) -> None:
        """Paint once, and never from inside another paint.

        A paint writes to stdout, and Python runs a signal handler in the
        middle of a write that blocks. A handler that paints would re-enter the
        buffered writer, which raises ``RuntimeError: reentrant call`` and
        takes the session down. Nothing reaches the writer twice: the inner
        request is remembered and drawn by the outer one on its way out, which
        is also the frame that has the newer state in it.
        """
        if self._drawing:
            self._redraw_after_draw = True
            return
        self._render_pending = False
        self._last_render = asyncio.get_event_loop().time()
        self._drawing = True
        try:
            self._screen.render()
        finally:
            self._drawing = False
        if self._redraw_after_draw:
            self._redraw_after_draw = False
            self._draw()

    # -- input -------------------------------------------------------------

    def _on_input(self, data: str) -> None:
        keys = self._decoder.feed(data)
        for key in keys:
            self._dispatch(key)

        # Whatever is left is an unfinished sequence. If nothing follows it
        # shortly, it was the Escape key.
        self._arm_escape_timer()
        if keys:
            self.request_immediate_render()

    def _dispatch(self, key: Key) -> None:
        if self._on_key is not None:
            self._on_key(key)
            return
        handler = getattr(self._root, "handle_input", None)
        if handler is not None:
            handler(key.name, key.data)

    def _arm_escape_timer(self) -> None:
        if self._escape_task is not None and not self._escape_task.done():
            self._escape_task.cancel()
        self._escape_task = asyncio.ensure_future(self._flush_escape())

    async def _flush_escape(self) -> None:
        await asyncio.sleep(ESCAPE_TIMEOUT)
        pending = self._decoder.take_pending()
        if not pending:
            return
        for key in pending:
            self._dispatch(key)
        self.request_immediate_render()

    # -- resize ------------------------------------------------------------

    def _on_resize(self) -> None:
        """Hand the redraw to the loop: the wrapping of every line just changed.

        This runs in a signal handler, and a window manager sends a burst of
        SIGWINCH for one drag of a window corner - so the signal that lands
        while the previous paint is still writing is the normal case, not the
        rare one. Painting from here would re-enter stdout mid-write and raise
        ``RuntimeError: reentrant call``; the loop runs the paint between
        writes instead.

        The burst collapses into a single redraw, which is all a resize needs:
        the screen is repainted from the size it has when that redraw runs, not
        from the size that each signal announced.
        """
        if not self._running or self._resize_pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - signal outside the loop
            return
        self._resize_pending = True
        loop.call_soon_threadsafe(self._apply_resize)

    def _apply_resize(self) -> None:
        """The deferred half of :meth:`_on_resize`, back on the event loop."""
        self._resize_pending = False
        if self._running:
            self.request_immediate_render()
