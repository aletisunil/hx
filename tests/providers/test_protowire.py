"""The hand-rolled protobuf codec: what it writes is what a generated one would."""

from __future__ import annotations

import pytest

from hx.providers.protowire import WireError, Writer, parse


def test_scalars_round_trip() -> None:
    message = Writer()
    message.put_uint(1, 300)
    message.put_bool(2, True)
    message.put_double(3, 0.4)
    message.put_str(4, "héllo")
    fields = parse(message.finish())

    assert fields.uint(1) == 300
    assert fields.flag(2) is True
    assert fields.f64(3) == 0.4
    assert fields.text(4) == "héllo"


def test_known_bytes_match_the_protobuf_spec() -> None:
    """The encoding example from the protobuf docs: field 1 = 150."""
    message = Writer()
    message.put_uint(1, 150)
    assert message.finish() == b"\x08\x96\x01"


def test_proto3_defaults_are_not_written() -> None:
    message = Writer()
    message.put_uint(1, 0)
    message.put_bool(2, False)
    message.put_double(3, 0.0)
    message.put_str(4, "")
    assert message.finish() == b""


def test_an_empty_sub_message_is_still_present() -> None:
    """Presence is the point of a sub-message: ``ChatToolChoice {}`` differs from none."""
    message = Writer()
    message.put_message(1, Writer())
    fields = parse(message.finish())
    assert fields.has(1)
    assert fields.message(1) is not None
    assert fields.message(2) is None


def test_repeated_fields_keep_order_and_empty_entries() -> None:
    message = Writer()
    message.put_strs(1, ["a", "", "c"])
    for value in (1, 2):
        child = Writer()
        child.put_uint(1, value)
        message.put_message(2, child)
    fields = parse(message.finish())

    assert fields.texts(1) == ["a", "", "c"]
    assert [child.uint(1) for child in fields.messages(2)] == [1, 2]


def test_packed_varints_are_one_length_delimited_field() -> None:
    message = Writer()
    message.put_packed_uints(30, [3, 4, 8])
    assert message.finish() == b"\xf2\x01\x03\x03\x04\x08"


def test_negative_int32_decodes_from_its_ten_byte_form() -> None:
    message = Writer()
    message.put_uint(1, -2)
    assert parse(message.finish()).int32(1) == -2


def test_float32_fields_decode() -> None:
    import struct

    fields = parse(b"\x15" + struct.pack("<f", 1.5))
    assert fields.f32(2) == 1.5


def test_the_last_occurrence_of_a_singular_field_wins() -> None:
    assert parse(b"\x08\x01\x08\x02").uint(1) == 2


def test_a_field_read_as_the_wrong_type_is_its_default() -> None:
    """Schema drift must not crash a reader - it reads as unset, as protobuf does."""
    fields = parse(b"\x08\x01")
    assert fields.text(1) == ""
    assert fields.message(1) is None


@pytest.mark.parametrize(
    "data",
    [b"\x08", b"\x0a\x05ab", b"\x0b", b"\x00\x01", b"\x09\x00\x00"],
    ids=["truncated varint", "short string", "group", "field zero", "short fixed64"],
)
def test_malformed_input_is_refused(data: bytes) -> None:
    with pytest.raises(WireError):
        parse(data)
