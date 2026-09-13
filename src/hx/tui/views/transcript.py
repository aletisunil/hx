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
from hx.term.primitives import LabelledRule, Spacer, Text
from hx.tui.glyphs import SPINNER
from hx.tui.paint import fg, rule
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


class WorkingRule(LabelledRule):
    """The prompt's top rule, which doubles as the working indicator.

    A turn starting costs no layout: the rule is there either way, so the
    transcript above it does not jump when a spinner appears. Borrowed from pi,
    and the single highest-leverage thing in its design.
    """

    def __init__(self) -> None:
        super().__init__(rule("border_muted"))
        self._frame = 0
        self._running = False
        self._elapsed = 0.0
        self._overflow = ""

    def start(self) -> None:
        self._running = True
        self._elapsed = 0.0
        self.invalidate()

    def stop(self) -> None:
        self._running = False
        self.set_label("")
        self.invalidate()

    def tick(self, elapsed: float) -> None:
        if not self._running:
            return
        self._frame = (self._frame + 1) % len(SPINNER)
        self._elapsed = elapsed
        self.invalidate()

    def draw(self, width: int) -> list[str]:
        if self._running:
            from hx.keys import primary_key

            label = fg("accent", SPINNER[self._frame]) + fg(
                "muted",
                f" Working… ({self._elapsed:.0f}s · {primary_key('app.interrupt')} to interrupt)",
            )
            self.set_label(label)
        return super().draw(width)


@runtime_checkable
class Prompt(Protocol):
    """What the dock needs from whatever is taking the user's typing.

    A protocol rather than a base class, so the stub here and the real editor
    that replaces it are interchangeable without either knowing about the
    other.
    """

    @property
    def value(self) -> str: ...

    def clear(self) -> None: ...

    def render(self, width: int) -> list[str]: ...

    def handle_input(self, key: str, data: str) -> bool: ...


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
    """Everything below the transcript, in the order it is drawn."""

    def __init__(self, prompt: Prompt | None = None) -> None:
        super().__init__()
        self.working = WorkingRule()
        self.prompt: Prompt = prompt if prompt is not None else StubPrompt()
        self.hints = HintsBar()
        self.status = StatusBar()

        self.add(Spacer(1))
        self.add(self.working)
        self.add(self.prompt)
        self.add(LabelledRule(rule("border_muted")))
        self.add(self.hints)
        self.add(self.status)


class Session(Container):
    """The whole document: a header, the transcript, and the dock."""

    def __init__(self, version: str = "", quiet: bool = False) -> None:
        super().__init__()
        self.header = Header(version, quiet)
        self.transcript = Transcript()
        self.dock = Dock()

        self.add(self.header)
        self.add(self.transcript)
        self.add(self.dock)

    def handle_input(self, key: str, data: str) -> bool:
        return self.dock.handle_input(key, data)
