"""Decoding what the terminal sends.

The hardest part of a terminal UI to get right is also the part that is
trivial to test exhaustively, because it is a pure function. So it is tested
exhaustively.
"""

from __future__ import annotations

import pytest

from hx.term.keydecode import Decoder, Key, decode


def names(data: str) -> list[str]:
    keys, _ = decode(data)
    return [key.name for key in keys]


def only(data: str) -> Key:
    """The single key ``data`` decodes to, flushing anything held back.

    A trailing Escape is deliberately held in case more bytes follow, so the
    flush is what turns "nothing yet" into the Escape key.
    """
    decoder = Decoder()
    keys = decoder.feed(data) + decoder.take_pending()
    assert len(keys) == 1, keys
    return keys[0]


@pytest.mark.parametrize(
    ("data", "name"),
    [
        ("\r", "enter"),
        ("\n", "ctrl+j"),
        ("\t", "tab"),
        ("\x7f", "backspace"),
        ("\x08", "backspace"),
        ("\x00", "ctrl+space"),
        ("\x01", "ctrl+a"),
        ("\x03", "ctrl+c"),
        ("\x04", "ctrl+d"),
        ("\x0f", "ctrl+o"),
        ("\x10", "ctrl+p"),
        ("\x12", "ctrl+r"),
        ("\x14", "ctrl+t"),
        ("\x15", "ctrl+u"),
        ("\x17", "ctrl+w"),
        ("\x18", "ctrl+x"),
        ("\x19", "ctrl+y"),
        ("\x1a", "ctrl+z"),
    ],
)
def test_control_characters(data: str, name: str) -> None:
    assert only(data).name == name


def test_enter_and_ctrl_j_are_not_the_same_key() -> None:
    """In raw mode Enter sends CR and ctrl+j sends LF, and the prompt binds
    them to different things: submit, and insert a newline."""
    assert only("\r").name == "enter"
    assert only("\n").name == "ctrl+j"


@pytest.mark.parametrize(
    ("data", "name"),
    [
        ("\x1b[A", "up"),
        ("\x1b[B", "down"),
        ("\x1b[C", "right"),
        ("\x1b[D", "left"),
        ("\x1b[H", "home"),
        ("\x1b[F", "end"),
        ("\x1b[Z", "shift+tab"),
        ("\x1b[2~", "insert"),
        ("\x1b[3~", "delete"),
        ("\x1b[5~", "pageup"),
        ("\x1b[6~", "pagedown"),
        ("\x1b[1~", "home"),
        ("\x1b[4~", "end"),
        ("\x1bOA", "up"),
        ("\x1bOP", "f1"),
        ("\x1b[15~", "f5"),
    ],
)
def test_escape_sequences(data: str, name: str) -> None:
    assert only(data).name == name


@pytest.mark.parametrize(
    ("data", "name"),
    [
        ("\x1b[1;5A", "ctrl+up"),
        ("\x1b[1;5B", "ctrl+down"),
        ("\x1b[1;2A", "shift+up"),
        ("\x1b[1;3A", "alt+up"),
        ("\x1b[1;6A", "ctrl+shift+up"),
        ("\x1b[1;6B", "ctrl+shift+down"),
        ("\x1b[1;5H", "ctrl+home"),
        ("\x1b[1;5F", "ctrl+end"),
        ("\x1b[3;5~", "ctrl+delete"),
    ],
)
def test_modifier_encodings(data: str, name: str) -> None:
    assert only(data).name == name


def test_alt_is_an_escape_prefix() -> None:
    assert only("\x1bb").name == "alt+b"
    assert only("\x1bd").name == "alt+d"
    assert only("\x1bf").name == "alt+f"
    assert only("\x1by").name == "alt+y"
    assert only("\x1b\r").name == "alt+enter"
    assert only("\x1b\x7f").name == "alt+backspace"


def test_kitty_encoding_reaches_the_keys_no_other_encoding_can_express() -> None:
    """shift+enter and ctrl+shift+z have no legacy encoding at all, which is
    why hx.keys lists ctrl+j as the fallback for one of them."""
    assert only("\x1b[13;2u").name == "shift+enter"
    assert only("\x1b[122;6u").name == "ctrl+shift+z"


def test_printable_text_is_batched() -> None:
    """A fast typist, or a paste into a terminal with no bracketed paste."""
    key = only("hello")
    assert key.name == "text"
    assert key.data == "hello"


def test_text_is_split_around_control_characters() -> None:
    assert names("ab\rcd") == ["text", "enter", "text"]


def test_a_bracketed_paste_arrives_as_one_event() -> None:
    key = only("\x1b[200~line one\nline two\x1b[201~")
    assert key.name == "paste"
    assert key.data == "line one\nline two"


def test_a_paste_containing_an_escape_is_still_one_event() -> None:
    """Pasting a log with colour in it must not be read as keystrokes."""
    key = only("\x1b[200~\x1b[31mred\x1b[0m\x1b[201~")
    assert key.name == "paste"
    assert key.data == "\x1b[31mred\x1b[0m"


def test_an_sgr_mouse_report_is_recognised_and_not_mistaken_for_text() -> None:
    assert only("\x1b[<0;12;34M").name == "mouse:0:12:34:M"
    assert only("\x1b[<0;12;34m").name == "mouse:0:12:34:m"


@pytest.mark.parametrize(
    "sequence",
    ["\x1b[1;5A", "\x1b[200~hello\x1b[201~", "\x1b[15~", "\x1bOA", "\x1b[<0;1;1M"],
)
@pytest.mark.parametrize("split", [1, 2, 3, 4])
def test_a_sequence_split_across_reads_is_not_garbled(sequence: str, split: int) -> None:
    """A single read lands wherever the kernel decides. A twelve-byte
    arrow-with-modifier can and does arrive as two."""
    if split >= len(sequence):
        pytest.skip("split past the end of this sequence")
    decoder = Decoder()
    first = decoder.feed(sequence[:split])
    second = decoder.feed(sequence[split:])
    whole, _ = decode(sequence)
    assert first + second == whole


def test_a_trailing_escape_is_held_rather_than_reported() -> None:
    """It could be the Escape key, or the first byte of an arrow still in
    flight. Reporting it immediately makes a slow arrow key press Escape."""
    keys, rest = decode("\x1b")
    assert keys == []
    assert rest == "\x1b"


def test_a_held_escape_is_eventually_reported_as_escape() -> None:
    """Otherwise the one key a user presses to get out of things does nothing."""
    decoder = Decoder()
    assert decoder.feed("\x1b") == []
    assert [key.name for key in decoder.take_pending()] == ["escape"]


def test_two_escapes_report_the_first_one() -> None:
    assert names("\x1b\x1b")[0] == "escape"


def test_an_incomplete_paste_is_held_whole() -> None:
    decoder = Decoder()
    assert decoder.feed("\x1b[200~partial") == []
    keys = decoder.feed(" rest\x1b[201~")
    assert [k.name for k in keys] == ["paste"]
    assert keys[0].data == "partial rest"


def test_every_key_the_app_binds_can_be_produced() -> None:
    """The decoder's whole job is to speak the vocabulary hx.keys already uses.
    A name here that nothing can emit is a binding that can never fire."""
    from hx.keys import KEYMAP

    bound: set[str] = set()
    for binding in KEYMAP.bindings.values():
        bound.update(binding.default_keys)

    producible = {
        "enter": "\r",
        "escape": "\x1b",
        "tab": "\t",
        "shift+tab": "\x1b[Z",
        "shift+enter": "\x1b[13;2u",
        "pageup": "\x1b[5~",
        "pagedown": "\x1b[6~",
        "ctrl+up": "\x1b[1;5A",
        "ctrl+down": "\x1b[1;5B",
        "ctrl+shift+up": "\x1b[1;6A",
        "ctrl+shift+down": "\x1b[1;6B",
        "ctrl+shift+z": "\x1b[122;6u",
        "ctrl+home": "\x1b[1;5H",
        "ctrl+end": "\x1b[1;5F",
        "ctrl+underscore": "\x1f",
        "alt+enter": "\x1b\r",
    }
    for name in sorted(bound):
        if name in producible:
            assert only(producible[name]).name == name
        elif name.startswith("alt+") and len(name) == 5:
            assert only("\x1b" + name[-1]).name == name
        elif name.startswith("ctrl+") and len(name) == 6:
            assert only(chr(ord(name[-1]) - 96)).name == name
        else:  # pragma: no cover - a new binding shape needs a case here
            raise AssertionError(f"no sequence known to produce {name!r}")
