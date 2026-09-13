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

        self.add(self.header)
        self.add(self.transcript)
        self.add(self.overlay)
        self.add(self.dock)

    def show(self, component: Component) -> None:
        self.overlay.clear()
        self.overlay.add(Spacer(1))
        self.overlay.add(component)

    def dismiss(self) -> None:
        self.overlay.clear()

    @property
    def showing(self) -> Component | None:
        """Whatever currently owns the keyboard, if anything."""
        blocks = [child for child in self.overlay.children if not isinstance(child, Spacer)]
        return blocks[-1] if blocks else None

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
