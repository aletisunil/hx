"""Protocol Buffers wire format, by hand.

The Devin route speaks protobuf, and only a few dozen fields of it. The
``protobuf`` package would bring generated code, a C extension on some
platforms, and a schema compiler into the build for what is a varint, a length
prefix and two fixed-width floats - so, like the terminal renderer, it is
written here instead.

Encoding is field-by-field with the ``put_*`` helpers on :class:`Writer`, which
skip proto3 default values exactly as a generated encoder does. Decoding is
schema-less: :func:`parse` turns a message into its fields by number, and the
``Fields`` accessors read them as the caller's schema says they are.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable

VARINT = 0
FIXED64 = 1
LEN = 2
FIXED32 = 5


class WireError(ValueError):
    """Bytes that are not a well-formed protobuf message."""


def _varint(value: int) -> bytes:
    if value < 0:
        # Negative int32/int64 are sign-extended to ten bytes on the wire.
        value += 1 << 64
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


class Writer:
    """Accumulates one message. Every ``put_*`` omits a proto3 default."""

    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf = bytearray()

    def _tag(self, number: int, wire_type: int) -> None:
        self._buf += _varint((number << 3) | wire_type)

    def put_uint(self, number: int, value: int) -> None:
        if value:
            self._tag(number, VARINT)
            self._buf += _varint(value)

    def put_bool(self, number: int, value: bool) -> None:
        self.put_uint(number, 1 if value else 0)

    def put_double(self, number: int, value: float) -> None:
        if value:
            self._tag(number, FIXED64)
            self._buf += struct.pack("<d", value)

    def put_str(self, number: int, value: str) -> None:
        if value:
            self.put_bytes(number, value.encode())

    def put_bytes(self, number: int, value: bytes) -> None:
        if value:
            self._tag(number, LEN)
            self._buf += _varint(len(value))
            self._buf += value

    def put_message(self, number: int, value: Writer | bytes | None) -> None:
        """A present sub-message is written even when empty: presence is the point."""
        if value is None:
            return
        raw = value.finish() if isinstance(value, Writer) else value
        self._tag(number, LEN)
        self._buf += _varint(len(raw))
        self._buf += raw

    def put_strs(self, number: int, values: Iterable[str]) -> None:
        for value in values:
            # Repeated strings keep empty entries; only singular fields elide.
            raw = value.encode()
            self._tag(number, LEN)
            self._buf += _varint(len(raw))
            self._buf += raw

    def put_packed_uints(self, number: int, values: Iterable[int]) -> None:
        packed = b"".join(_varint(v) for v in values)
        self.put_bytes(number, packed)

    def finish(self) -> bytes:
        return bytes(self._buf)


FieldValue = int | bytes


class Fields:
    """A decoded message: raw values by field number, in wire order."""

    __slots__ = ("_fields",)

    def __init__(self, fields: dict[int, list[tuple[int, FieldValue]]]) -> None:
        self._fields = fields

    def _last(self, number: int) -> tuple[int, FieldValue] | None:
        values = self._fields.get(number)
        # Proto3: for a singular field, the last occurrence wins.
        return values[-1] if values else None

    def has(self, number: int) -> bool:
        return number in self._fields

    def uint(self, number: int) -> int:
        entry = self._last(number)
        return entry[1] if entry is not None and entry[0] == VARINT else 0  # type: ignore[return-value]

    def int32(self, number: int) -> int:
        value = self.uint(number) & 0xFFFFFFFF
        return value - (1 << 32) if value & 0x80000000 else value

    def flag(self, number: int) -> bool:
        return self.uint(number) != 0

    def f32(self, number: int) -> float:
        entry = self._last(number)
        if entry is None or entry[0] != FIXED32:
            return 0.0
        return float(struct.unpack("<f", entry[1])[0])  # type: ignore[arg-type]

    def f64(self, number: int) -> float:
        entry = self._last(number)
        if entry is None or entry[0] != FIXED64:
            return 0.0
        return float(struct.unpack("<d", entry[1])[0])  # type: ignore[arg-type]

    def raw(self, number: int) -> bytes:
        entry = self._last(number)
        return entry[1] if entry is not None and entry[0] == LEN else b""  # type: ignore[return-value]

    def text(self, number: int) -> str:
        return self.raw(number).decode(errors="replace")

    def message(self, number: int) -> Fields | None:
        entry = self._last(number)
        if entry is None or entry[0] != LEN:
            return None
        return parse(entry[1])  # type: ignore[arg-type]

    def messages(self, number: int) -> list[Fields]:
        return [
            parse(value)  # type: ignore[arg-type]
            for wire_type, value in self._fields.get(number, ())
            if wire_type == LEN
        ]

    def texts(self, number: int) -> list[str]:
        return [
            value.decode(errors="replace")  # type: ignore[union-attr]
            for wire_type, value in self._fields.get(number, ())
            if wire_type == LEN
        ]


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise WireError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift >= 70:
            raise WireError("varint too long")


def parse(data: bytes) -> Fields:
    """Split a message into its fields.

    Raises:
        WireError: on truncation or a wire type protobuf does not define. A
            group (types 3 and 4) is refused rather than skipped: no message
            this route reads has used one since proto2.
    """
    fields: dict[int, list[tuple[int, FieldValue]]] = {}
    pos = 0
    end = len(data)
    while pos < end:
        key, pos = _read_varint(data, pos)
        number, wire_type = key >> 3, key & 0x7
        if number == 0:
            raise WireError("field number 0")
        value: FieldValue
        if wire_type == VARINT:
            value, pos = _read_varint(data, pos)
        elif wire_type == FIXED64:
            if pos + 8 > end:
                raise WireError("truncated fixed64")
            value, pos = data[pos : pos + 8], pos + 8
        elif wire_type == LEN:
            length, pos = _read_varint(data, pos)
            if pos + length > end:
                raise WireError("truncated length-delimited field")
            value, pos = data[pos : pos + length], pos + length
        elif wire_type == FIXED32:
            if pos + 4 > end:
                raise WireError("truncated fixed32")
            value, pos = data[pos : pos + 4], pos + 4
        else:
            raise WireError(f"unsupported wire type {wire_type}")
        fields.setdefault(number, []).append((wire_type, value))
    return Fields(fields)
