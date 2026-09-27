"""Asking the operating system which modifier keys are held.

For the one key a terminal cannot describe. A terminal built on xterm.js - the
Claude desktop app's panel, VS Code's - and Apple's Terminal speak neither the
kitty keyboard protocol nor modifyOtherKeys, so shift+enter reaches HX as
whatever the terminal makes of it: a bare carriage return, the same byte as
enter, or - in a terminal remapped so that shift+enter does *something* - ESC
then carriage return, the same bytes as alt+enter. The Claude desktop panel
does the second. Nothing on the wire tells either pair apart.

The keyboard still knows: on macOS the window server tracks which modifiers
are down, and any process may ask. So an enter, or an alt+enter, that arrives
while shift is held and option is not was shift+enter.

Only asked where the answer describes the person typing: on macOS, and not
over ssh, where the keyboard of the machine HX runs on is not the one in use.
``HX_MODIFIER_PROBE=0`` turns it off - for input that is not typed at all, from
``tmux send-keys`` or a script, where the keyboard says nothing about the key.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from functools import cache
from typing import Any

_COMBINED_SESSION_STATE = 0
"""``kCGEventSourceStateCombinedSessionState``: the hardware state combined
with what has been posted to this login session."""
_MASKS = {
    "shift": 0x00020000,  # kCGEventFlagMaskShift
    "ctrl": 0x00040000,  # kCGEventFlagMaskControl
    "alt": 0x00080000,  # kCGEventFlagMaskAlternate - option
    "super": 0x00100000,  # kCGEventFlagMaskCommand
}


@cache
def _flags_state() -> Any:
    """``CGEventSourceFlagsState``, or None where it cannot be asked."""
    if sys.platform != "darwin":
        return None
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return None
    if os.environ.get("HX_MODIFIER_PROBE", "").strip() == "0":
        return None
    try:
        graphics = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        function = graphics.CGEventSourceFlagsState
    except (OSError, AttributeError):
        return None
    function.argtypes = [ctypes.c_int32]
    function.restype = ctypes.c_uint64
    return function


def held() -> frozenset[str] | None:
    """The modifiers down right now - ``shift``, ``ctrl``, ``alt``, ``super``.

    None where that is unknowable, which callers must treat differently from
    "none held": an empty set is evidence, None is its absence.
    """
    function = _flags_state()
    if function is None:
        return None
    try:
        flags = function(_COMBINED_SESSION_STATE)
    except Exception:  # a window server gone away is not worth a crash
        return None
    return frozenset(name for name, mask in _MASKS.items() if flags & mask)


def shift_enter(key: str) -> bool:
    """Whether ``key`` - ``enter`` or ``alt+enter`` as decoded - was shift+enter.

    True when shift is held and option is not: the bytes said enter or
    alt+enter, the keyboard says shift. A real option+enter keeps option held
    and is left alone, so steering still works.
    """
    if key not in ("enter", "alt+enter"):
        return False
    modifiers = held()
    return modifiers is not None and "shift" in modifiers and "alt" not in modifiers


_ROLLOVER_SECONDS = 0.2
"""How soon after a shifted character an enter is taken to be rollover - shift
still on its way up from the ``?`` - rather than a deliberate shift+enter."""
_SHIFTED_SYMBOLS = frozenset('~!@#$%^&*()_+{}|:"<>?')


class ShiftEnter:
    """:func:`shift_enter`, minus the enter that only follows a shifted character.

    The keyboard is asked when the enter is read, not when it was pressed, and
    fast typists release shift late: ``does this work?`` then enter arrives
    with shift still down. An enter hard on the heels of a character that
    needed shift is read as the plain enter it almost always is.
    """

    def __init__(self) -> None:
        self._shifted_at = float("-inf")

    def typed(self, text: str) -> None:
        """Note what was just typed, for the enter that may follow it."""
        last = text[-1:]
        shifted = last.isupper() or last in _SHIFTED_SYMBOLS
        self._shifted_at = time.monotonic() if shifted else float("-inf")

    def __call__(self, key: str) -> bool:
        if time.monotonic() - self._shifted_at < _ROLLOVER_SECONDS:
            return False
        return shift_enter(key)
