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

#: A CSI sequence, per ECMA-48: parameter bytes (digits, ``;``, ``:`` and the
#: private markers ``<=>?``), intermediate bytes, then one final byte. The
#: private markers matter because a terminal's *answers* use them - ``CSI ? 1
#: u`` is a reply about the keyboard protocol - and a pattern that did not
#: admit them would read every answer as a sequence still arriving.
_CSI_PATTERN = re.compile(r"\x1b\[([0-?]*)([ -/]*)([@-~])")
_SS3_PATTERN = re.compile(r"\x1bO([@-~])")
_MOUSE_PATTERN = re.compile(r"\x1b\[<([0-9]+);([0-9]+);([0-9]+)([Mm])")

REPORT_PREFIX = "report:"
"""Names under this prefix are the terminal answering a query, not keys."""
KEYBOARD_REPORT = "report:keyboard"
"""The terminal's answer to ``CSI ? u``: which kitty keyboard flags are on.
``data`` holds the flags. Only a terminal that speaks the protocol answers."""
ATTRIBUTES_REPORT = "report:attributes"
"""The terminal's answer to ``CSI c`` (primary device attributes). Every
terminal answers this one, which is what makes it useful: sent after the
keyboard query, it arriving *alone* means the keyboard query went unanswered."""

#: Kitty's numbers for keys that have no character of their own. With
#: disambiguation on the keypad is reported this way, so a numpad digit
#: arrives as 57399-57408 rather than as the digit - and has to become the
#: digit again here, or typing on the numpad would do nothing.
_KITTY_KEYPAD_TEXT = {
    **{57399 + digit: str(digit) for digit in range(10)},
    57409: ".",
    57410: "/",
    57411: "*",
    57412: "-",
    57413: "+",
    57415: "=",
    57416: ",",
}
_KITTY_KEYPAD_NAMED = {
    57414: "enter",
    57417: "left",
    57418: "right",
    57419: "up",
    57420: "down",
    57421: "pageup",
    57422: "pagedown",
    57423: "home",
    57424: "end",
    57425: "insert",
    57426: "delete",
    57427: "begin",
}

#: ``ctrl`` plus a punctuation key, named the way a legacy terminal's C0 byte
#: is (see :func:`_control_name`). The same physical keys produce both
#: encodings - ``ctrl+_``, ``ctrl+-`` and ``ctrl+/`` all send ``0x1f`` in xterm
#: - so a binding written against one has to match the other, whichever the
#: terminal ended up speaking.
_CTRL_PUNCTUATION = {
    " ": "space",
    "@": "space",
    "_": "underscore",
    "-": "underscore",
    "/": "underscore",
    "\\": "backslash",
    "]": "right_square_bracket",
    "^": "circumflex_accent",
}

_KEY_RELEASE = 3
"""Kitty's event type for a key coming up. Only sent when asked for, which HX
does not do, but a terminal is not obliged to be minimal - and a release acted
on as a press is every key typed twice."""


def _modifiers(value: int) -> list[str]:
    """Decode the modifier parameter, which is a bitmask plus one.

    xterm defines shift, alt and ctrl; the kitty protocol adds super, hyper and
    meta above them, and caps lock and num lock above those. The locks are
    state rather than part of a chord - ctrl+c with caps lock on is still
    ctrl+c - so they are dropped. The others are kept even though nothing binds
    them, so that ctrl+super+c cannot pass for ctrl+c.
    """
    bits = max(0, value - 1)
    out = []
    if bits & 8:
        out.append("super")
    if bits & 16:
        out.append("hyper")
    if bits & 32:
        out.append("meta")
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

    params, intermediates, final = match.groups()
    end = match.end()

    if params.startswith("?") and not intermediates:
        # An answer to one of HX's own queries, not a key.
        if final == "u":
            return end, [Key(KEYBOARD_REPORT, params[1:])], False
        if final == "c":
            return end, [Key(ATTRIBUTES_REPORT, params[1:])], False
    if intermediates or params[:1] in ("<", "=", ">", "?"):
        # Some other reply or private sequence: nothing a user typed.
        return end, [], False

    fields = [
        [int(p) if p.isdigit() else 0 for p in field.split(":")] for field in params.split(";")
    ]
    modifier = fields[1][0] if len(fields) > 1 and fields[1] else 1
    event = fields[1][1] if len(fields) > 1 and len(fields[1]) > 1 else 1
    if event == _KEY_RELEASE:
        return end, [], False

    if final == "u":
        # The kitty keyboard protocol: ``CSI code[:shifted[:base]] ; mods u``.
        # The only encoding that can say shift+enter.
        code, *alternates = fields[0]
        shifted = alternates[0] if alternates and alternates[0] else None
        base = alternates[1] if len(alternates) > 1 and alternates[1] else None
        key = _unicode_key(code, shifted, base, _modifiers(modifier))
        return end, [key] if key is not None else [], False

    if final == "~":
        number = fields[0][0]
        if number == 27 and len(fields) > 2:
            # xterm's modifyOtherKeys: ``CSI 27 ; mods ; code ~``. The fallback
            # for terminals without the kitty protocol, and what tmux sends.
            key = _unicode_key(fields[2][0], None, None, _modifiers(modifier))
            return end, [key] if key is not None else [], False
        if number == 13 and modifier == 2:
            # Strictly shift+F3 in xterm's VT220 mode. But it is also what
            # terminals remapped by hand send for shift+enter, and HX binds no
            # function keys - so the reading that does something wins.
            return end, [Key("shift+enter")], False
        name = _CSI_TILDE.get(number, "unknown")
        return end, [Key(_with_modifiers(name, _modifiers(modifier)))], False

    if final in _CSI_FINAL:
        name = _CSI_FINAL[final]
        return end, [Key(_with_modifiers(name, _modifiers(modifier)))], False

    return end, [], False


def _unicode_key(code: int, shifted: int | None, base: int | None, mods: list[str]) -> Key | None:
    """A key reported by its codepoint, under either extended encoding.

    Named the way the legacy encoding names the same key wherever the legacy
    encoding can express it, so a binding means the same thing whichever one
    the terminal speaks. The exception is what the legacy encoding throws
    away: ``ctrl+shift+z`` is ``ctrl+z`` there, and here it is itself.
    """
    if code in _KITTY_KEYPAD_NAMED:
        return Key(_with_modifiers(_KITTY_KEYPAD_NAMED[code], mods))
    if code in _KITTY_KEYPAD_TEXT:
        char = _KITTY_KEYPAD_TEXT[code]
        return Key("text", char) if not mods else Key(_with_modifiers(char, mods))
    if code < 0x20 or code == 0x7F:
        name = _NAMED_CONTROLS.get(chr(code))
        return Key(_with_modifiers(name or _control_name(chr(code)), mods))
    if not 0 < code <= 0x10FFFF or 0xE000 <= code <= 0xF8FF:
        # Kitty's other private-use keys - media keys, lone modifiers - and
        # anything out of range: nothing HX binds, and never text.
        return None

    chording = [mod for mod in mods if mod != "shift"]
    if not chording:
        # Shift alone, or nothing: this is typing.
        if "shift" not in mods:
            return Key("text", chr(code))
        return Key("text", chr(shifted) if shifted else chr(code).upper())

    char = chr(code)
    if not char.isascii() and base:
        # A non-Latin layout. Shortcuts are meant by position - ctrl+c is the
        # key where C is on a US keyboard - and ``base`` is that key.
        char = chr(base)

    if "ctrl" in mods:
        if "shift" in mods and shifted and chr(shifted) in _CTRL_PUNCTUATION:
            char = chr(shifted)
        if char in _CTRL_PUNCTUATION:
            # A legacy terminal cannot tell these apart, so neither may we.
            rest = [mod for mod in mods if mod != "shift"]
            return Key(_with_modifiers(_CTRL_PUNCTUATION[char], rest))
        return Key(_with_modifiers("space" if char == " " else char.lower(), mods))

    # super, hyper and meta have no legacy encoding to agree with, so shift is
    # named like ctrl's: cmd+shift+z is super+shift+z, as a user writes it.
    if set(chording) - {"alt"}:
        return Key(_with_modifiers("space" if char == " " else char.lower(), mods))

    # alt alone. The legacy encoding sends ESC then the shifted character, so
    # alt+shift+b has always been alt+B.
    if "shift" in mods:
        char = chr(shifted) if shifted else char.upper()
        mods = [mod for mod in mods if mod != "shift"]
    return Key(_with_modifiers("space" if char == " " else char, mods))


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
