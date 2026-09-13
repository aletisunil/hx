"""A runnable sample of the renderer: ``python -m hx.term.demo``.

Exists so the library can be exercised on a real terminal - iTerm2, Ghostty,
Alacritty, tmux, over ssh - before any of HX is built on top of it. Finding out
that a terminal disagrees about a width or an escape sequence is cheap here and
expensive later.

Type to edit the line, press ctrl+c to leave. Resize the window while it runs.
"""

from __future__ import annotations

import asyncio
import itertools

from hx.term.ansi import detect_color_mode, fg, inverse
from hx.term.component import Container
from hx.term.loop import TuiRunner
from hx.term.primitives import CURSOR, GUTTER, Box, HangingText, LabelledRule, Rule, Spacer, Text
from hx.term.screen import CURSOR_MARKER

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

ACCENT, TEXT, MUTED, DIM = "#8abeb7", "#d4d4d4", "#808080", "#666666"
BORDER, WARNING, SUCCESS = "#5f87ff", "#ffff00", "#b5bd68"
USER_BG, TOOL_BG = "#343541", "#283228"


def main() -> None:
    mode = detect_color_mode()

    def paint(color: str, text: str, **kw: bool) -> str:
        return fg(color, text, mode, **kw)

    root = Container()
    root.add(Spacer(1))
    root.add(Text(paint(ACCENT, "hx.term demo", bold=True) + paint(DIM, f"  {mode}"), 1, 0))
    root.add(Text(paint(MUTED, "type to edit · resize the window · ctrl+c to quit"), 1, 0))
    root.add(Spacer(1))

    root.add(
        Box(
            1,
            1,
            lambda line: f"\x1b[48;2;52;53;65m{line}\x1b[49m",
            Text(paint(TEXT, "a user message, tinted the width of the terminal"), 0, 0),
        )
    )
    root.add(Spacer(1))
    root.add(
        Box(
            1,
            1,
            lambda line: f"\x1b[48;2;40;50;40m{line}\x1b[49m",
            Text(paint(TEXT, "● ") + paint(MUTED, "a tool call that finished"), 0, 0),
        )
    )
    root.add(Spacer(1))
    root.add(
        HangingText(
            paint(MUTED, "· "),
            paint(
                MUTED,
                "a notice long enough to wrap, so the continuation lines "
                "indent under the text rather than under the bullet",
            ),
        )
    )
    root.add(Spacer(1))

    root.add(Rule(lambda line: paint(BORDER, line)))
    root.add(Spacer(1))
    root.add(Text(paint(TEXT, "Permission needed", bold=True), 1, 0))
    root.add(Text(paint(WARNING, "Bash", bold=True), 1, 0))
    root.add(Spacer(1))
    root.add(Text(paint(ACCENT, "$ ") + paint(TEXT, "rm -rf build/"), 1, 0))
    root.add(Spacer(1))
    for index, (key, label) in enumerate(
        [("y", "allow once"), ("s", "allow for this session"), ("a", "always allow"), ("n", "deny")]
    ):
        on = index == 0
        marker = paint(ACCENT, CURSOR) if on else GUTTER
        root.add(
            Text(
                marker
                + paint(ACCENT if on else DIM, key)
                + "  "
                + paint(ACCENT if on else TEXT, label),
                1,
                0,
            )
        )
    root.add(Spacer(1))
    root.add(Rule(lambda line: paint(BORDER, line)))
    root.add(Spacer(1))

    root.add(Text(paint(TEXT, "wide and combining: 日本語 👨‍👩‍👧‍👦 🇯🇵 👍🏽 café"), 1, 0))
    root.add(Spacer(1))

    status = LabelledRule(lambda line: paint(DIM, line))
    editor = Text("", 1, 0)
    root.add(status)
    root.add(editor)
    root.add(Rule(lambda line: paint(DIM, line)))
    root.add(Text(paint(DIM, "ctrl+c") + paint(MUTED, " quit"), 1, 0))

    buffer: list[str] = list("edit me")
    frames = itertools.cycle(SPINNER)
    runner = TuiRunner(root)

    def repaint() -> None:
        typed = "".join(buffer)
        editor.set_text(paint(TEXT, typed) + CURSOR_MARKER + inverse(" "))

    def on_key(key) -> None:  # type: ignore[no-untyped-def]
        if key.name == "ctrl+c":
            runner.stop()
            return
        if key.name == "backspace" and buffer:
            buffer.pop()
        elif key.name in ("text", "paste"):
            buffer.extend(key.data)
        repaint()

    runner._on_key = on_key
    repaint()

    async def spin() -> None:
        seconds = 0.0
        while True:
            status.set_label(
                paint(ACCENT, next(frames))
                + paint(MUTED, f" Working… ({seconds:.1f}s · esc to interrupt)")
            )
            runner.request_render()
            await asyncio.sleep(0.08)
            seconds += 0.08

    async def run() -> None:
        spinner = asyncio.create_task(spin())
        try:
            await runner.run()
        finally:
            spinner.cancel()

    asyncio.run(run())


if __name__ == "__main__":
    main()
