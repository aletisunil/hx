"""The conversation, and the dock beneath it.

The transcript is not a scrolling widget - it is the document, and the terminal
scrolls it. Blocks are appended and never moved, which is what lets finished
output become real scrollback that the terminal owns: selectable, copyable,
and still there after the process exits.

The dock below it is the part that does change: the prompt, its two rules, the
hints and the status bar. Keeping it at the end of the document means a redraw
of the live parts touches only the tail.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from hx.term.component import Component, Container
from hx.term.primitives import Spacer, Text
from hx.tui.glyphs import SPINNER
from hx.tui.paint import fg
from hx.tui.views.status import Header, HintsBar, StatusBar


class Transcript(Container):
    """Appended to, never rearranged.

    Every block brings the blank line above it, so two neighbours cannot each
    contribute one and leave a double gap.
    """

    def __init__(self, *children: Component) -> None:
        super().__init__(*children)
        self.cursor: Component | None = None
        """The block the keyboard is pointing at, for copy and expand."""

    def append(self, block: Component) -> Component:
        if self.children:
            super().add(Spacer(1))
        return super().add(block)

    @property
    def blocks(self) -> list[Component]:
        """The blocks, without the spacers between them."""
        return [child for child in self.children if not isinstance(child, Spacer)]

    def last(self) -> Component | None:
        blocks = self.blocks
        return blocks[-1] if blocks else None

    # -- the reading cursor ------------------------------------------------

    def navigable(self) -> list[Component]:
        """Blocks the cursor can land on: things somebody said."""
        from hx.tui.views.blocks import AssistantMessage, UserMessage

        return [b for b in self.blocks if isinstance(b, UserMessage | AssistantMessage)]

    def move_cursor(self, delta: int) -> Component | None:
        """Step to the next or previous message.

        Starting from the bottom, so the first press goes to the last message
        rather than the first - which is where the reader just was.
        """
        blocks = self.navigable()
        if not blocks:
            return None
        if self.cursor is None or self.cursor not in blocks:
            index = len(blocks) - 1 if delta < 0 else 0
        else:
            index = max(0, min(len(blocks) - 1, blocks.index(self.cursor) + delta))
        self.cursor = blocks[index]
        return self.cursor

    def cursored_text(self) -> str:
        """Text of the cursored block, falling back to the last answer.

        The fallback is what makes the copy key useful without navigating
        first: the thing a user most often wants is the answer just given.
        """
        from hx.tui.views.blocks import AssistantMessage

        if self.cursor is not None:
            return str(getattr(self.cursor, "text", "") or "")
        for block in reversed(self.blocks):
            if isinstance(block, AssistantMessage):
                return block.text
        return ""


@runtime_checkable
class Dockable(Protocol):
    """A framed block that can be told the dock will close it.

    A protocol rather than the :class:`~hx.tui.views.dialog.Framed` mixin
    itself, so this module does not have to import the dialogs it lays out.
    """

    def set_docked(self, docked: bool) -> None: ...


@runtime_checkable
class Prompt(Protocol):
    """What the dock needs from whatever is taking the user's typing.

    A protocol rather than a base class, so a stub and the real editor are
    interchangeable without either knowing about the other.
    """

    @property
    def value(self) -> str: ...

    def clear(self) -> None: ...

    def set_status(self, label: str) -> None: ...

    def render(self, width: int) -> list[str]: ...

    def handle_input(self, key: str, data: str) -> bool: ...


class WorkingIndicator:
    """The working status, written into the prompt's own top rule.

    Not a component. The prompt is framed by two rules whether or not a turn is
    running, so the status has nowhere of its own to be - it lives in a line
    that already exists, which is why a turn starting costs no layout and the
    transcript above never jumps. Borrowed from pi, and the highest-leverage
    thing in its design.
    """

    def __init__(self, target: Prompt) -> None:
        self.target = target
        self._frame = 0
        self._running = False
        self._elapsed = 0.0

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        self._running = True
        self._elapsed = 0.0
        self._paint()

    def stop(self) -> None:
        self._running = False
        self.target.set_status("")

    def tick(self, elapsed: float) -> None:
        if not self._running:
            return
        self._frame = (self._frame + 1) % len(SPINNER)
        self._elapsed = elapsed
        self._paint()

    def _paint(self) -> None:
        from hx.keys import primary_key

        self.target.set_status(
            fg("accent", SPINNER[self._frame])
            + fg(
                "muted",
                f" Working… ({self._elapsed:.0f}s · {primary_key('app.interrupt')} to interrupt)",
            )
        )


class StubPrompt(Text):
    """A single line of typing, and nothing else.

    Deliberately minimal. The real editor is a large piece of work and it is
    scheduled after the transcript has been proven against real sessions -
    putting a placeholder here is what lets that proving happen first.
    """

    def __init__(self) -> None:
        super().__init__("", 1, 0)
        self._buffer = ""
        # Drawn now rather than on the first keystroke, so a session opens with
        # a placeholder and a cursor instead of an empty row.
        self._refresh()

    @property
    def value(self) -> str:
        return self._buffer

    def clear(self) -> None:
        self._buffer = ""
        self._refresh()

    def handle_input(self, key: str, data: str) -> bool:
        if key in ("text", "paste"):
            self._buffer += data.replace("\n", " ")
        elif key == "backspace":
            self._buffer = self._buffer[:-1]
        elif key == "ctrl+u":
            self._buffer = ""
        else:
            return False
        self._refresh()
        return True

    def _refresh(self) -> None:
        from hx.term.ansi import inverse
        from hx.term.screen import CURSOR_MARKER

        placeholder = "Ask HX…  (/ for commands)"
        shown = fg("text", self._buffer) if self._buffer else fg("dim", placeholder)
        self.set_text(shown + CURSOR_MARKER + inverse(" "))


class Dock(Container):
    """Everything below the transcript, in the order it is drawn.

    It draws no frame of its own: the prompt is framed by its own two rules,
    and adding another pair here is how the dock ended up with four.
    """

    def __init__(self, prompt: Prompt | None = None) -> None:
        super().__init__()
        if prompt is None:
            from pathlib import Path

            from hx.tui.views.prompt import Prompt as RealPrompt

            prompt = RealPrompt(Path.cwd())
        self.prompt: Prompt = prompt
        self.working = WorkingIndicator(self.prompt)
        self.hints = HintsBar()
        self.status = StatusBar()

        self.add(Spacer(1))
        self.add(self.prompt)
        self.add(self.hints)
        self.add(self.status)


class Session(Container):
    """The whole document: a header, the transcript, an overlay, and the dock.

    The overlay sits between the transcript and the dock rather than on top of
    anything. There is no layering in a scrollback-native UI - the document is
    the screen - so a picker is a block that appears where the conversation is
    and takes the keyboard while it is there. Which is also the better
    behaviour: it does not cover the sentence the user is deciding about.
    """

    def __init__(
        self, version: str = "", quiet: bool = False, prompt: Prompt | None = None
    ) -> None:
        super().__init__()
        self.header = Header(version, quiet)
        self.transcript = Transcript()
        self.overlay = Container()
        self.dock = Dock(prompt)
        self._docked: Dockable | None = None
        """Whichever framed block is currently letting the dock close it."""

        self.add(self.header)
        self.add(self.transcript)
        self.add(self.overlay)
        self.add(self.dock)

    OVERLAY_GAP = 1
    """Blank rows :meth:`show` puts between the transcript and the overlay.

    Named because :meth:`overlay_rows` has to subtract it: a picker sized to
    the screen less the dock is exactly this much too tall, and the row it
    overruns by is the one its own top rule is drawn on.
    """

    def show(self, component: Component) -> None:
        self.overlay.clear()
        self.overlay.add(Spacer(self.OVERLAY_GAP))
        self.overlay.add(component)

    def render(self, width: int) -> list[str]:
        self._seat_frames()
        return super().render(width)

    def _seat_frames(self) -> None:
        """Tell whatever lands directly above the dock that the dock closes it.

        The dock opens with a blank line and then the prompt's own rule, so a
        framed block that also closes with a rule produces rule, blank, rule -
        three lines of frame for one edge, which reads as a rendering fault.

        Which block that is changes as the conversation grows: a picker while
        one is up, an approval when it is the last thing said, nothing at all
        once a tool block lands under it. So it is decided per frame here,
        rather than fixed when the block was made.
        """
        last = self.showing
        if last is None:
            blocks = self.transcript.blocks
            last = blocks[-1] if blocks else None
        bottom = last if isinstance(last, Dockable) else None
        if bottom is self._docked:
            return
        if self._docked is not None:
            self._docked.set_docked(False)
        self._docked = bottom
        if bottom is not None:
            bottom.set_docked(True)

    def dismiss(self) -> None:
        self.overlay.clear()

    @property
    def showing(self) -> Component | None:
        """Whatever currently owns the keyboard, if anything."""
        blocks = [child for child in self.overlay.children if not isinstance(child, Spacer)]
        return blocks[-1] if blocks else None

    def footer_height(self, width: int) -> int:
        """Rows the renderer holds on the bottom of the screen.

        The dock, and only the dock: the prompt a user is typing into should be
        where they last saw it, not wherever the conversation above happened to
        end.
        """
        return len(self.dock.render(width))

    def overlay_rows(self, width: int, rows: int) -> int:
        """How tall a thing on the overlay may be, on a terminal this size.

        The screen, less the dock it sits above and the gap :meth:`show` puts
        above it.
        """
        return max(1, rows - self.footer_height(width) - self.OVERLAY_GAP)

    def handle_input(self, key: str, data: str) -> bool:
        showing = self.showing
        if showing is not None:
            handler = getattr(showing, "handle_input", None)
            if handler is not None and handler(key, data):
                return True
            # An overlay swallows everything else while it is up: a key meant
            # for it must never fall through and be typed into the prompt.
            return True
        return self.dock.handle_input(key, data)
