"""HX's frontend. Consumes the core event bus; the core never imports this.

Two implementations live here during the renderer replacement: :mod:`hx.tui.legacy`
(the Textual app) and the scrollback-native one being built alongside it.
:mod:`hx.tui.app` picks between them. The palette, the theme loader, the fuzzy
matcher, the kill ring and the clipboard are shared by both and stay at this level.
"""
