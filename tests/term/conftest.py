"""Reading rendered output in a test.

Components return styled lines, which are unreadable in an assertion and
unreviewable in a diff. :func:`plain` gives back what the user sees;
:func:`assert_render` compares a component against a block of expected text and
prints a legible side-by-side when it disagrees.
"""

from __future__ import annotations

import textwrap

from hx.term.ansi import SEGMENT_RESET
from hx.term.component import Component
from hx.term.width import cell_width, strip_ansi


def plain(lines: list[str]) -> list[str]:
    """The lines with all styling removed and trailing space trimmed."""
    return [strip_ansi(line).rstrip() for line in lines]


def render_plain(component: Component, width: int) -> list[str]:
    return plain(component.render(width))


def assert_render(component: Component, width: int, expected: str) -> None:
    """Compare a component's visible output against an expected block.

    Each expected line starts at a ``|``, so that the one-column padding every
    line of chat carries is visible in the test rather than being eaten by
    :func:`textwrap.dedent`.
    """
    want = [
        line.split("|", 1)[1] if "|" in line else line
        for line in textwrap.dedent(expected).strip("\n").split("\n")
    ]
    got = render_plain(component, width)
    if got != want:
        ruler = "".join(str(i % 10) for i in range(width))
        report = ["", f"width {width}", f"     {ruler}"]
        for index in range(max(len(want), len(got))):
            w = want[index] if index < len(want) else "<missing>"
            g = got[index] if index < len(got) else "<missing>"
            flag = " " if w == g else "!"
            report.append(f"{flag}want {w!r}")
            report.append(f"{flag} got {g!r}")
        raise AssertionError("\n".join(report))


def assert_lines_fit(component: Component, width: int) -> None:
    """The renderer's two hard invariants, asserted on one component."""
    for index, line in enumerate(component.render(width)):
        assert cell_width(line) <= width, (
            f"line {index} is {cell_width(line)} cells wide at width {width}: {line!r}"
        )
        assert line == "" or line.endswith(SEGMENT_RESET), (
            f"line {index} does not end in a reset: {line!r}"
        )
