"""Turning image bytes into something every route will accept.

An image reaches the model from three places - pasted into the prompt, read
from disk by the Read tool, or returned by an MCP server - and whatever it was,
it leaves here as an :class:`~hx.core.messages.ImageBlock` that no provider
will reject:

- **A format every route takes.** PNG, JPEG, GIF and WebP pass through; a BMP
  or TIFF (what the macOS clipboard hands over) is re-encoded as PNG.
- **Bounded dimensions.** The long edge is capped at :data:`MAX_EDGE`. Anthropic
  downsamples past about 1568 px anyway and refuses anything over 8000, and a
  request carrying more than twenty images drops that limit to 2000.
- **Bounded bytes.** :data:`MAX_BYTES` keeps the base64 under Anthropic's 5 MB
  per-image limit, the tightest of the three routes. A retina screenshot is the
  common case that needs this: a 5K display captures a PNG of well over that.

Nothing is re-encoded that does not have to be. An image already within every
limit is sent byte for byte, so a screenshot of small text keeps every pixel.
"""

from __future__ import annotations

import base64
import io
import math
import re
import shlex
import sys
import warnings
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from hx.core.messages import ContentBlock, ImageBlock, Message, TextBlock, ToolResultBlock

MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
"""What every route accepts as it stands."""

MAX_EDGE = 2000
"""Longest side sent, in pixels."""

MAX_BYTES = 3_750_000
"""Largest encoded image sent. Its base64 is 5 MB, Anthropic's per-image cap."""

MAX_SOURCE_BYTES = 64 * 1024 * 1024
"""Largest file even opened. A decoder handed a gigabyte of TIFF is how a
paste freezes the session, and nothing that size is a picture of anything the
model needs to see."""

MAX_SOURCE_PIXELS = 100_000_000
"""Largest image decoded. Pillow's own bomb check warns at 89 MP and refuses at
twice that; the warning is silenced and this is the limit instead, refused as
an error the user is shown."""

MAX_PASTE_CHARS = 16_384
"""Longest paste still checked for dragged-in paths. Tokenising is pure Python,
and a paste longer than this is text, not a list of files."""

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"})
"""Extensions treated as images when a path is pasted or read."""

TOKENS_PER_PIXEL = 1 / 750
"""Anthropic's published estimate. OpenAI's tile maths lands within a factor of
two either side of it, which is close enough for a context gauge."""

ESTIMATE_EDGE = 1568
ESTIMATE_PIXELS = 1_150_000
"""What Anthropic scales an image down to before counting it."""

_FORMAT_MEDIA_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}

_JPEG_QUALITY = 85

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage


class ImageError(ValueError):
    """The bytes are not an image HX can send. The message says why, for the user."""


def load_image(data: bytes, *, label: str = "") -> ImageBlock:
    """Normalise encoded image bytes into a block every route accepts.

    Raises:
        ImageError: when the bytes are not a decodable image, or one too large
            to decode safely.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    if not data:
        raise ImageError("the image is empty")
    if len(data) > MAX_SOURCE_BYTES:
        raise ImageError(
            f"the image is {_size(len(data))}, over the {_size(MAX_SOURCE_BYTES)} limit"
        )

    try:
        with warnings.catch_warnings():
            # Pillow warns from 89 MP, before the size can be checked here;
            # past twice that it raises DecompressionBombError itself.
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(data))
            width, height = image.size
            if width * height > MAX_SOURCE_PIXELS:
                raise ImageError(f"the image is {width}x{height}, too large to decode")
            image.load()
    except ImageError:
        raise
    except Image.DecompressionBombError:
        raise ImageError("the image is too large to decode") from None
    except UnidentifiedImageError:
        raise ImageError("not a readable image") from None
    except (OSError, ValueError, SyntaxError) as exc:
        # Pillow reports a truncated or corrupt file through any of these.
        raise ImageError(f"not a readable image ({exc})") from None

    media_type = _FORMAT_MEDIA_TYPES.get(image.format or "")
    rotated = _orientation(image) not in (None, 1)
    if (
        media_type is not None
        and not rotated
        and max(width, height) <= MAX_EDGE
        and len(data) <= MAX_BYTES
    ):
        return ImageBlock(
            media_type=media_type,
            data=base64.b64encode(data).decode("ascii"),
            width=width,
            height=height,
            label=label,
        )

    # Everything past here re-encodes. An animated GIF keeps its first frame:
    # no route plays one back, and most would show only that frame anyway.
    try:
        image.seek(0)
        frame = ImageOps.exif_transpose(image) if rotated else image
        return _reencode(frame, label=label)
    except (OSError, ValueError) as exc:
        # A mode Pillow cannot convert or write, say.
        raise ImageError(f"could not re-encode the image ({exc})") from None


def load_image_file(path: Path, *, label: str | None = None) -> ImageBlock:
    """Read and normalise an image file.

    Raises:
        ImageError: when the file cannot be read or is not a sendable image.
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ImageError(f"cannot read {path}: {exc.strerror or exc}") from None
    if size > MAX_SOURCE_BYTES:
        raise ImageError(f"{path.name} is {_size(size)}, over the {_size(MAX_SOURCE_BYTES)} limit")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ImageError(f"cannot read {path}: {exc.strerror or exc}") from None
    return load_image(data, label=path.name if label is None else label)


def is_image_path(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_SUFFIXES


def sniff_image(head: bytes) -> bool:
    """Whether a file's first bytes are an image signature Pillow decodes.

    Lets Read recognise a screenshot saved without an extension, rather than
    calling it binary and giving up.
    """
    return head.startswith(
        (
            b"\x89PNG\r\n\x1a\n",
            b"\xff\xd8\xff",
            b"GIF87a",
            b"GIF89a",
            b"II*\x00",
            b"MM\x00*",
        )
    ) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")


def pasted_image_paths(text: str) -> list[Path] | None:
    """The image files a paste names, when naming them is all it does.

    Dragging a file from Finder or a file manager into a terminal pastes its
    path - quoted, or with spaces backslash-escaped, depending on the terminal -
    and ``file://`` URIs arrive from some Linux desktops. Any of those, alone or
    several at once, is read as "attach these". Anything else - a path among
    other words, a relative name, a path to something that is not an image, a
    file that does not exist - is ``None``, and the paste goes in as the text it
    was. A relative ``logo.png`` is somebody pasting a filename to talk about;
    a drag always pastes the whole path.
    """
    stripped = text.strip()
    if not stripped or len(stripped) > MAX_PASTE_CHARS or not _PATH_START.match(stripped):
        return None
    tokens = _split_paths(stripped, windows=sys.platform == "win32")
    if not tokens:
        return None

    paths: list[Path] = []
    for token in tokens:
        if token.startswith("file://"):
            from urllib.parse import unquote, urlparse

            token = unquote(urlparse(token).path)
        path = Path(token).expanduser()
        if not path.is_absolute():
            return None
        if not is_image_path(path) or not path.is_file():
            return None
        paths.append(path)
    return paths


_PATH_START = re.compile(r"""["']?(?:[/~]|file://|[A-Za-z]:[\\/]|\\\\)""")
"""How a dragged-in path begins: absolute POSIX, home, ``file://``, a Windows
drive or UNC share - optionally quoted."""


def _split_paths(text: str, *, windows: bool) -> list[str] | None:
    """Several files arrive space- or newline-separated; shlex takes both, and
    undoes the quoting. Backslash escapes only on POSIX: on Windows the
    backslash is the path separator, and a terminal quotes a path with spaces
    rather than escaping them."""
    try:
        tokens = shlex.split(text, posix=not windows)
    except ValueError:
        return None
    if windows:
        tokens = [
            token[1:-1]
            if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'"
            else token
            for token in tokens
        ]
    return tokens


def estimate_tokens(image: ImageBlock) -> int:
    """Roughly what one image costs in context, for the gauge and compaction.

    An image whose size was never recorded is charged as a full-size one:
    guessing low is how compaction fires one turn too late.
    """
    width, height = image.width, image.height
    if width <= 0 or height <= 0:
        return math.ceil(ESTIMATE_PIXELS * TOKENS_PER_PIXEL)
    scale = min(
        1.0, ESTIMATE_EDGE / max(width, height), math.sqrt(ESTIMATE_PIXELS / (width * height))
    )
    return math.ceil(width * height * scale * scale * TOKENS_PER_PIXEL)


def describe(image: ImageBlock) -> str:
    """``screenshot.png · 1440x900 · 312 kB`` - how an image is named on screen."""
    parts = [image.label or "image"]
    if image.source and image.source != image.label:
        parts.append(image.source)
    if image.width and image.height:
        parts.append(f"{image.width}x{image.height}")
    parts.append(_size(len(image.data) * 3 // 4))
    return " · ".join(parts)


def without_images(messages: Iterable[Message], model_name: str) -> list[Message]:
    """The conversation as a model that takes no images must be sent it.

    Each image becomes a line of text saying it was left out, rather than
    vanishing. The model then knows the user attached something it cannot see
    and can say so, instead of answering as if the message were complete. The
    transcript itself keeps the images, so switching back to a model that can
    see them loses nothing.
    """
    out: list[Message] = []
    for message in messages:
        if not _has_images(message):
            out.append(message)
            continue
        content: list[ContentBlock] = []
        for block in message.content:
            if isinstance(block, ImageBlock):
                content.append(TextBlock(text=f"\n{_omitted(block, model_name)}"))
            elif isinstance(block, ToolResultBlock) and block.images:
                notes = "\n".join(_omitted(image, model_name) for image in block.images)
                content.append(replace(block, content=f"{block.content}\n\n{notes}", images=[]))
            else:
                content.append(block)
        out.append(replace(message, content=content))
    return out


def _omitted(image: ImageBlock, model_name: str) -> str:
    return f"[{image.label or 'image'} omitted: {model_name} does not accept image input]"


def _has_images(message: Message) -> bool:
    return any(
        isinstance(block, ImageBlock) or (isinstance(block, ToolResultBlock) and block.images)
        for block in message.content
    )


def _orientation(image: PILImage) -> int | None:
    """The EXIF orientation tag. A phone photo is stored sideways and relies on
    this to be shown upright; no route is guaranteed to honour it."""
    try:
        value = image.getexif().get(0x0112)
    except Exception:
        return None
    return value if isinstance(value, int) else None


def _reencode(image: PILImage, *, label: str) -> ImageBlock:
    """Scale to fit, then encode small enough.

    PNG first when the image has transparency or is a palette image - a UI
    screenshot, a diagram - since that is what keeps text crisp. JPEG when PNG
    is too big, and after that the image shrinks until it fits.
    """
    frame = image
    if frame.mode not in ("RGB", "RGBA", "L", "LA", "P"):
        frame = frame.convert("RGBA" if "A" in frame.getbands() else "RGB")
    if frame.mode == "P":
        frame = frame.convert("RGBA" if "transparency" in frame.info else "RGB")

    if max(frame.size) > MAX_EDGE:
        frame = _scaled(frame, MAX_EDGE / max(frame.size))

    while True:
        encoded = _encode(frame, "PNG")
        media_type = "image/png"
        if len(encoded) > MAX_BYTES:
            encoded = _encode(_flattened(frame), "JPEG")
            media_type = "image/jpeg"
        if len(encoded) <= MAX_BYTES or min(frame.size) <= 64:
            break
        frame = _scaled(frame, 0.75)

    return ImageBlock(
        media_type=media_type,
        data=base64.b64encode(encoded).decode("ascii"),
        width=frame.size[0],
        height=frame.size[1],
        label=label,
    )


def _scaled(image: PILImage, factor: float) -> PILImage:
    from PIL import Image

    width, height = image.size
    size = (max(1, round(width * factor)), max(1, round(height * factor)))
    return image.resize(size, Image.Resampling.LANCZOS)


def _flattened(image: PILImage) -> PILImage:
    """Drop transparency onto white. JPEG has no alpha channel, and a
    transparent pixel's hidden colour is usually black - a UI icon becomes a
    black square."""
    from PIL import Image

    if image.mode in ("RGBA", "LA"):
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.getchannel("A"))
        return background
    return image.convert("RGB") if image.mode != "RGB" else image


def _encode(image: PILImage, fmt: str) -> bytes:
    buffer = io.BytesIO()
    if fmt == "JPEG":
        image.save(buffer, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    else:
        image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _size(count: int) -> str:
    if count < 1000:
        return f"{count} B"
    if count < 1_000_000:
        return f"{count / 1000:.0f} kB"
    return f"{count / 1_000_000:.1f} MB"
