"""The Textual frontend, on its way out.

This is the app HX shipped through 0.1.x. It is being replaced by a
scrollback-native renderer (:mod:`hx.term` plus :mod:`hx.tui.views`), and it
lives here - untouched, still green - so that every stage of that replacement
is additive and the branch is always releasable.

:func:`hx.tui.app.run_tui` decides which of the two runs. Nothing new should
import from this package; the shared halves of the old TUI (the palette, the
theme loader, the fuzzy matcher, the kill ring, the clipboard) deliberately
stayed behind in :mod:`hx.tui`.
"""
