"""A terminal UI library: components are pure functions from width to lines.

The whole library rests on one invariant::

    render(width) -> list[str]     every line at most `width` cells wide,
                                   every line ending in a full reset

Everything else follows from it. A container is list concatenation. A rule is
one repeated character. A dialog is a rule, some spacers and some text. Nothing
negotiates for space, nothing owns a coordinate, and any component can be
asserted against a block of expected text in a unit test.

The renderer draws into the terminal's own scrollback rather than an alternate
screen, so finished output is real scrollback: the terminal scrolls it, selects
it and copies it, and this library never touches it again.

Nothing here imports :mod:`hx`. It knows about terminals, not about agents.
"""
