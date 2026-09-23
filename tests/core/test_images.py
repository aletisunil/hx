"""Image normalisation: what reaches a provider, whatever was pasted."""

from __future__ import annotations

import base64
import io
import os
from pathlib import Path

import pytest
from PIL import Image

from hx.core.images import (
    MAX_BYTES,
    MAX_EDGE,
    MAX_PASTE_CHARS,
    ImageError,
    _split_paths,
    describe,
    estimate_tokens,
    load_image,
    load_image_file,
    pasted_image_paths,
    sniff_image,
    without_images,
)
from hx.core.messages import (
    ImageBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    user_message,
)


def encoded(
    size: tuple[int, int] = (40, 30),
    fmt: str = "PNG",
    mode: str = "RGB",
    color: object = (200, 30, 30),
    **save: object,
) -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, format=fmt, **save)  # type: ignore[arg-type]
    return buffer.getvalue()


def noise(size: tuple[int, int]) -> bytes:
    """A PNG that does not compress, so a large one is large in bytes too."""
    buffer = io.BytesIO()
    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(buffer, format="PNG")
    return buffer.getvalue()


def decoded(image: ImageBlock) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(image.data)))


# -- load_image ---------------------------------------------------------------


def test_an_image_within_every_limit_is_sent_byte_for_byte() -> None:
    """Re-encoding a screenshot of small text is how it becomes unreadable."""
    raw = encoded((800, 600))
    image = load_image(raw, label="shot.png")
    assert base64.b64decode(image.data) == raw
    assert (image.media_type, image.width, image.height) == ("image/png", 800, 600)
    assert image.label == "shot.png"


@pytest.mark.parametrize(
    ("fmt", "media_type"),
    [("JPEG", "image/jpeg"), ("GIF", "image/gif"), ("WEBP", "image/webp")],
)
def test_every_format_the_routes_take_passes_through(fmt: str, media_type: str) -> None:
    raw = encoded(fmt=fmt)
    image = load_image(raw)
    assert image.media_type == media_type
    assert base64.b64decode(image.data) == raw


@pytest.mark.parametrize("fmt", ["BMP", "TIFF"])
def test_a_format_no_route_takes_is_re_encoded_as_png(fmt: str) -> None:
    """TIFF is what the macOS clipboard hands over for a copied picture."""
    image = load_image(encoded(fmt=fmt))
    assert image.media_type == "image/png"
    assert decoded(image).format == "PNG"
    assert (image.width, image.height) == (40, 30)


def test_a_large_image_is_scaled_to_the_edge_limit_keeping_its_shape() -> None:
    image = load_image(encoded((5120, 2880)))
    assert max(image.width, image.height) == MAX_EDGE
    assert image.width / image.height == pytest.approx(5120 / 2880, rel=0.01)
    assert decoded(image).size == (image.width, image.height)


def test_an_image_too_heavy_for_the_byte_limit_is_brought_under_it() -> None:
    """Retina screenshots are the common case: within the edge limit is no
    promise of being within 5 MB, and noise is the worst case for PNG."""
    raw = noise((1900, 1400))
    assert len(raw) > MAX_BYTES
    image = load_image(raw)
    assert len(base64.b64decode(image.data)) <= MAX_BYTES
    assert image.media_type == "image/jpeg"


def test_transparency_is_flattened_onto_white_when_jpeg_is_needed() -> None:
    """A transparent pixel's hidden colour is usually black; JPEG would show it."""
    buffer = io.BytesIO()
    pixels = os.urandom(1900 * 1400 * 4)
    Image.frombytes("RGBA", (1900, 1400), pixels).save(buffer, format="PNG")
    image = load_image(buffer.getvalue())
    assert image.media_type == "image/jpeg"
    assert decoded(image).mode == "RGB"


def test_a_phone_photo_is_turned_upright() -> None:
    """EXIF orientation 6 is stored sideways; not every route honours the tag."""
    exif = Image.Exif()
    exif[0x0112] = 6
    raw = encoded((60, 20), fmt="JPEG", exif=exif.tobytes())
    image = load_image(raw)
    assert (image.width, image.height) == (20, 60)


def test_an_animated_gif_within_limits_is_left_alone() -> None:
    buffer = io.BytesIO()
    frames = [Image.new("RGB", (20, 20), color) for color in ("red", "blue")]
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:])
    raw = buffer.getvalue()
    assert base64.b64decode(load_image(raw).data) == raw


@pytest.mark.parametrize(
    ("raw", "message"),
    [(b"", "empty"), (b"definitely not a picture", "not a readable image")],
)
def test_what_is_not_an_image_is_refused_with_a_reason(raw: bytes, message: str) -> None:
    with pytest.raises(ImageError, match=message):
        load_image(raw)


def test_a_truncated_image_is_refused_rather_than_sent() -> None:
    with pytest.raises(ImageError):
        load_image(encoded((400, 400))[:200])


def test_a_decompression_bomb_is_refused() -> None:
    """A tiny file claiming a huge canvas must not be decoded."""
    raw = encoded((12_000, 10_000), mode="1", color=0)
    with pytest.raises(ImageError, match="too large"):
        load_image(raw)


def test_a_missing_file_says_which(tmp_path: Path) -> None:
    with pytest.raises(ImageError, match="cannot read"):
        load_image_file(tmp_path / "gone.png")


def test_a_file_is_labelled_by_its_name(tmp_path: Path) -> None:
    path = tmp_path / "mockup.png"
    path.write_bytes(encoded())
    assert load_image_file(path).label == "mockup.png"


# -- recognising images -------------------------------------------------------


def test_signatures_are_recognised_without_an_extension() -> None:
    assert sniff_image(encoded()[:16])
    assert sniff_image(encoded(fmt="JPEG")[:16])
    assert sniff_image(encoded(fmt="WEBP")[:16])
    assert not sniff_image(b"#!/usr/bin/env python\n")


def test_a_dragged_in_path_is_read_as_an_attachment(tmp_path: Path) -> None:
    spaced = tmp_path / "Screen Shot 1.png"
    spaced.write_bytes(encoded())
    other = tmp_path / "b.jpg"
    other.write_bytes(encoded(fmt="JPEG"))

    escaped = str(spaced).replace(" ", "\\ ")
    assert pasted_image_paths(escaped) == [spaced]
    assert pasted_image_paths(f"'{spaced}'") == [spaced]
    assert pasted_image_paths(f"file://{spaced.as_posix().replace(' ', '%20')}") == [spaced]
    assert pasted_image_paths(f"{escaped} {other}\n") == [spaced, other]


def test_a_paste_that_is_more_than_paths_stays_text(tmp_path: Path) -> None:
    image = tmp_path / "a.png"
    image.write_bytes(encoded())
    notes = tmp_path / "notes.txt"
    notes.write_text("hello")

    assert pasted_image_paths(f"look at {image}") is None
    assert pasted_image_paths(str(notes)) is None
    assert pasted_image_paths(str(tmp_path / "missing.png")) is None
    assert pasted_image_paths("print('unbalanced") is None
    assert pasted_image_paths("   ") is None
    # A bare name is a filename being talked about; a drag pastes the whole path.
    assert pasted_image_paths("a.png") is None


# -- accounting and presentation ---------------------------------------------


def test_a_windows_path_keeps_its_backslashes() -> None:
    assert _split_paths(r'C:\Users\me\shot.png "C:\My Pics\a.png"', windows=True) == [
        r"C:\Users\me\shot.png",
        r"C:\My Pics\a.png",
    ]
    assert _split_paths(r"/tmp/Screen\ Shot.png", windows=False) == ["/tmp/Screen Shot.png"]


def test_a_long_or_non_path_paste_is_not_tokenised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shlex

    monkeypatch.setattr(shlex, "split", lambda *_a, **_k: pytest.fail("tokenised"))
    assert pasted_image_paths("def main():\n    pass") is None
    assert pasted_image_paths("/" + "x" * MAX_PASTE_CHARS) is None


def test_a_failed_re_encode_is_an_image_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from hx.core import images

    def broken(*_: object, **__: object) -> ImageBlock:
        raise ValueError("conversion from La to L not supported")

    monkeypatch.setattr(images, "_reencode", broken)
    with pytest.raises(ImageError, match="could not re-encode"):
        load_image(encoded((MAX_EDGE + 10, 10)))


def test_the_token_estimate_follows_pixels_not_bytes() -> None:
    small = ImageBlock("image/png", "", width=200, height=200)
    large = ImageBlock("image/png", "", width=1568, height=730)
    huge = ImageBlock("image/png", "", width=4000, height=4000)
    assert estimate_tokens(small) == 54
    assert 1400 < estimate_tokens(large) < 1600
    # Scaled down before counting, as the provider does.
    assert estimate_tokens(huge) <= 1534
    # Unknown size is charged as a full one, never as free.
    assert estimate_tokens(ImageBlock("image/png", "")) == estimate_tokens(huge)


def test_an_image_is_described_by_name_size_and_weight() -> None:
    image = ImageBlock("image/png", "A" * 4000, width=1440, height=900, label="Image #1")
    assert describe(image) == "Image #1 · 1440x900 · 3 kB"


def test_a_text_only_model_is_told_what_it_is_not_shown() -> None:
    image = ImageBlock("image/png", "AAAA", label="Image #1")
    shot = ImageBlock("image/png", "BBBB", label="shot.png")
    messages = [
        user_message("what is this? [Image #1]", [image]),
        Message(role="user", content=[ToolResultBlock("t1", "Image shot.png", images=[shot])]),
        user_message("plain"),
    ]

    stripped = without_images(messages, "Tiny Model")

    first = stripped[0]
    assert not first.images()
    assert "[Image #1 omitted: Tiny Model does not accept image input]" in first.text()
    result = stripped[1].content[0]
    assert isinstance(result, ToolResultBlock)
    assert result.images == []
    assert "[shot.png omitted: Tiny Model does not accept image input]" in result.content
    assert stripped[2] is messages[2]
    # The transcript itself keeps them.
    assert messages[0].images() == [image]


def test_an_image_only_turn_carries_no_empty_text() -> None:
    image = ImageBlock("image/png", "AAAA")
    assert user_message("", [image]).content == [image]
    assert user_message("hi", [image]).content == [TextBlock("hi"), image]
    assert user_message("").content == [TextBlock("")]
