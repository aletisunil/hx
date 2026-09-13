"""HX's frontend. Consumes the core event bus; the core never imports this.

:mod:`hx.tui.runtime` runs a session, drawing with :mod:`hx.term` into the
terminal's own scrollback. The palette, the theme loader, the fuzzy matcher,
the kill ring and the clipboard sit at this level; the blocks and dialogs are
in :mod:`hx.tui.views`.
"""
