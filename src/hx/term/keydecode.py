"""Turning what the terminal sends into the key names the app already uses.

A terminal delivers keys as bytes, and the encoding is a pile of conventions
rather than a standard: an arrow is three or six bytes depending on whether a
modifier is held, Escape is indistinguishable from the start of every escape
sequence, and ``shift+enter`` cannot be expressed at all unless the terminal
speaks a newer protocol. All of that is dealt with here, once.

:func:`decode` is a pure function from a string to key events, which is the
point: the hardest part of a terminal UI to get right is also the part that is
trivial to test exhaustively, as long as nothing else is entangled with it.

The names it produces are the ones :mod:`hx.keys` already uses - ``ctrl+c``,
``shift+tab``, ``alt+enter``, ``ctrl+shift+up`` - so the keybinding registry,
``~/.hx/keybindings.json`` and every description in ``/help`` carry over
without a translation layer between them and this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ESC = "\x1b"

PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"


@dataclass(frozen=True, slots=True)
class Key:
    """One key event.

    ``name`` is what bindings match on. ``data`` is the text the key inserts,
    empty for anything that is not typing - so an editor appends ``data`` and
    never has to know which names are printable.
    """

    name: str
    data: str = ""

    @property
    def is_text(self) -> bool:
        return self.name in ("text", "paste")


#: The final byte of a CSI sequence, and what it means.
_CSI_FINAL = {
    "A": "up",
    "B": "down",
    "C": "right",
    "D": "left",
    "H": "home",
    "F": "end",
    "E": "begin",
    "P": "f1",
    "Q": "f2",
    "R": "f3",
    "S": "f4",
    "Z": "shift+tab",  # CSI Z is back-tab, and carries its shift in the name
}

#: ``CSI <number> ~`` sequences.
_CSI_TILDE = {
    1: "home",
    2: "insert",
    3: "delete",
    4: "end",
    5: "pageup",
    6: "pagedown",
    7: "home",
    8: "end",
    11: "f1",
    12: "f2",
    13: "f3",
    14: "f4",
    15: "f5",
    17: "f6",
    18: "f7",
    19: "f8",
    20: "f9",
    21: "f10",
    23: "f11",
    24: "f12",
}

#: Control characters that have a name of their own rather than a ``ctrl+``
#: one. Tab is ctrl+i and Enter is ctrl+m as far as the wire is concerned, but
#: no user thinks of them that way.
_NAMED_CONTROLS = {
    "\x00": "ctrl+space",
    "\x08": "backspace",  # ctrl+h on some terminals
    "\x09": "tab",
    "\x0a": "ctrl+j",  # LF: a real ctrl+j, since Enter sends CR in raw mode
    "\x0d": "enter",
    "\x1b": "escape",
    "\x7f": "backspace",
}

_CSI_PATTERN = re.compile(r"\x1b\[([0-9;:<]*)([@-~])")
_SS3_PATTERN = re.compile(r"\x1bO([@-~])")
_MOUSE_PATTERN = re.compile(r"\x1b\[<([0-9]+);([0-9]+);([0-9]+)([Mm])")


def _modifiers(value: int) -> list[str]:
    """Decode the xterm modifier parameter, which is a bitmask plus one."""
    bits = max(0, value - 1)
    out = []
    if bits & 4:
        out.append("ctrl")
    if bits & 2:
        out.append("alt")
    if bits & 1:
        out.append("shift")
    return out


def _with_modifiers(base: str, mods: list[str]) -> str:
    """Assemble a name, keeping the order the keybinding registry uses."""
    if not mods:
        return base
    # shift+tab already says so; adding it twice would not match anything.
    if base == "shift+tab" and mods == ["shift"]:
        return base
    return "+".join([*mods, base])


def _control_name(char: str) -> str:
    """``ctrl+<letter>`` for a C0 control that has no name of its own."""
    code = ord(char)
    if 1 <= code <= 26:
        return f"ctrl+{chr(code + 96)}"
    if code == 28:
        return "ctrl+backslash"
    if code == 29:
        return "ctrl+right_square_bracket"
    if code == 30:
        return "ctrl+circumflex_accent"
    if code == 31:
        return "ctrl+underscore"
    return "unknown"


def decode(buffer: str) -> tuple[list[Key], str]:
    """Decode as much of ``buffer`` as is complete.

    Returns the keys found and whatever trailing bytes were an escape sequence
    cut in half. A single read can land anywhere - a 12-byte arrow-with-modifier
    can arrive as two reads - so the remainder is carried into the next call
    rather than being decoded as a stray Escape and some letters.
    """
    keys: list[Key] = []
    index = 0
    length = len(buffer)

    while index < length:
        char = buffer[index]

        if char != ESC:
            consumed, key = _decode_plain(buffer, index)
            if key is not None:
                keys.append(key)
            index = consumed
            continue

        # A lone Escape at the very end of a read is ambiguous: it could be the
        # key, or the first byte of a sequence still in flight. Holding it is
        # what stops a slow arrow key from registering as Escape.
        if index + 1 >= length:
            return keys, buffer[index:]

        following = buffer[index + 1]

        if following == "[":
            consumed, produced, incomplete = _decode_csi(buffer, index)
            if incomplete:
                return keys, buffer[index:]
            keys.extend(produced)
            index = consumed
            continue

        if following == "O":
            match = _SS3_PATTERN.match(buffer, index)
            if match is None:
                return keys, buffer[index:]
            name = _CSI_FINAL.get(match.group(1), "unknown")
            keys.append(Key(name))
            index = match.end()
            continue

        if following == ESC:
            # Two escapes: the first is a real Escape press.
            keys.append(Key("escape"))
            index += 1
            continue

        # ESC then anything else is alt+that.
        if following in _NAMED_CONTROLS:
            keys.append(Key(f"alt+{_NAMED_CONTROLS[following]}"))
        elif ord(following) < 0x20:
            keys.append(Key(f"alt+{_control_name(following)}"))
        else:
            keys.append(Key(f"alt+{following}"))
        index += 2

    return keys, ""


def _decode_plain(buffer: str, index: int) -> tuple[int, Key | None]:
    """A control character, or a run of printable text."""
    char = buffer[index]

    if char in _NAMED_CONTROLS:
        return index + 1, Key(_NAMED_CONTROLS[char])
    if ord(char) < 0x20:
        return index + 1, Key(_control_name(char), char)

    # Printable characters are batched, so a fast typist or a paste into a
    # terminal without bracketed paste is one event rather than hundreds.
    end = index
    while end < len(buffer) and buffer[end] >= " " and buffer[end] != "\x7f":
        end += 1
    return end, Key("text", buffer[index:end])


def _decode_csi(buffer: str, index: int) -> tuple[int, list[Key], bool]:
    """Decode one ``CSI`` sequence. The third result means "not all here yet"."""
    if buffer.startswith(PASTE_START, index):
        end = buffer.find(PASTE_END, index)
        if end == -1:
            return index, [], True  # the paste is still arriving
        text = buffer[index + len(PASTE_START) : end]
        return end + len(PASTE_END), [Key("paste", text)], False

    mouse = _MOUSE_PATTERN.match(buffer, index)
    if mouse is not None:
        button, column, row, kind = mouse.groups()
        return mouse.end(), [Key(f"mouse:{button}:{column}:{row}:{kind}")], False

    match = _CSI_PATTERN.match(buffer, index)
    if match is None:
        # Either incomplete, or something we do not recognise. Treating an
        # unterminated sequence as incomplete is the safe reading: the worst
        # case is one dropped keystroke rather than a burst of garbage text.
        return index, [], True

    params, final = match.groups()
    parts = [int(p) if p.isdigit() else 0 for p in params.replace(":", ";").split(";")] or [0]

    if final == "u":
        # Kitty's protocol, the only encoding that can say "shift+enter".
        code = parts[0] if parts else 0
        mods = _modifiers(parts[1]) if len(parts) > 1 else []
        base = _NAMED_CONTROLS.get(chr(code), chr(code) if 0x20 <= code < 0x7F else "unknown")
        return match.end(), [Key(_with_modifiers(base, mods))], False

    if final == "~":
        name = _CSI_TILDE.get(parts[0], "unknown")
        mods = _modifiers(parts[1]) if len(parts) > 1 else []
        return match.end(), [Key(_with_modifiers(name, mods))], False

    if final in _CSI_FINAL:
        name = _CSI_FINAL[final]
        mods = _modifiers(parts[1]) if len(parts) > 1 else []
        return match.end(), [Key(_with_modifiers(name, mods))], False

    return match.end(), [], False


class Decoder:
    """Stateful wrapper that carries an incomplete sequence between reads."""

    __slots__ = ("_pending",)

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, data: str) -> list[Key]:
        keys, self._pending = decode(self._pending + data)
        return keys

    def take_pending(self) -> list[Key]:
        """Flush a held-back sequence as-is.

        A lone Escape is held in case it is the start of an arrow key. If no
        more bytes follow, it was the Escape key, and something has to say so -
        otherwise the one key a user presses to get out of things is the one
        key that does nothing.
        """
        if not self._pending:
            return []
        pending, self._pending = self._pending, ""
        if pending == ESC:
            return [Key("escape")]
        keys, _ = decode(pending + " ")
        return keys
