"""The scrollback-native frontend's blocks.

Each is a component - a pure function from a width to lines - drawn with the
vocabulary in :mod:`hx.term.primitives` and coloured through :mod:`hx.tui.paint`.

The grammar throughout, taken from pi:

* A block that belongs to somebody is tinted, and the tint reaches the full
  width. Backgrounds mean *whose turn this is* or *how a tool call ended*, and
  nothing else - never emphasis.
* A section is delimited by a rule above and below, never by a rectangle.
* ``Spacer(1)`` separates blocks, emitted by the block itself, so two
  neighbours cannot both contribute one.
"""
