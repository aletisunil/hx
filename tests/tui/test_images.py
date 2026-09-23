"""Attaching images in the TUI: the prompt's tokens, and the session around them."""

from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from hx.core.messages import ImageBlock, user_message
from hx.tui import clipboard, paint
from hx.tui.views.blocks import UserMessage
from hx.tui.views.prompt import Prompt
from tests.term.conftest import assert_lines_fit, plain
from tests.tui.support import Driver, build_session

PICTURE = ImageBlock("image/png", "iVBORw0KGgo=", width=1440, height=900)


@pytest.fixture(autouse=True)
def _pinned_colors() -> None:
    paint.set_color_mode("truecolor")


def png(size: tuple[int, int] = (30, 20)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "green").save(buffer, format="PNG")
    return buffer.getvalue()


# -- the prompt ---------------------------------------------------------------


def test_an_attached_image_is_a_token_at_the_cursor(tmp_path: Path) -> None:
    prompt = Prompt(tmp_path)
    prompt.handle_input("text", "what is")
    first = prompt.attach_image(PICTURE)
    prompt.handle_input("text", "and")
    prompt.attach_image(PICTURE)

    assert prompt.text == "what is [Image #1] and [Image #2] "
    assert first.label == "Image #1"
    assert [image.label for image in prompt.images_in(prompt.text)] == ["Image #1", "Image #2"]


def test_what_is_sent_is_what_the_text_still_mentions(tmp_path: Path) -> None:
    prompt = Prompt(tmp_path)
    sent: list[tuple[str, list[str]]] = []
    prompt.on_submit = lambda text, images: sent.append((text, [i.label for i in images]))
    prompt.attach_image(PICTURE)
    prompt.attach_image(PICTURE)
    # The user deletes the first token by hand and mentions the second twice.
    prompt.text = "compare [Image #2] with [Image #2]"
    prompt.handle_input("enter", "\r")
    assert sent == [("compare [Image #2] with [Image #2]", ["Image #2"])]


def test_backspace_takes_a_whole_token(tmp_path: Path) -> None:
    """Half a token is a stray bracket and an image silently dropped."""
    prompt = Prompt(tmp_path)
    prompt.handle_input("text", "see")
    prompt.attach_image(PICTURE)
    prompt.handle_input("backspace", "")  # the trailing space
    prompt.handle_input("backspace", "")
    assert prompt.text == "see "
    prompt.handle_input("backspace", "")
    assert prompt.text == "see"


def test_backspace_takes_the_token_at_the_cursor_not_an_earlier_one(tmp_path: Path) -> None:
    prompt = Prompt(tmp_path)
    prompt.attach_image(PICTURE)
    prompt.attach_image(PICTURE)
    assert prompt.text == "[Image #1] [Image #2] "
    prompt.handle_input("backspace", "")
    prompt.handle_input("backspace", "")
    assert prompt.text == "[Image #1] "


@pytest.mark.parametrize(
    ("key", "cursor", "left"),
    [
        ("ctrl+w", 13, "see  and"),  # word-deleting "#1]" takes the token
        ("delete", 4, "see  and"),  # forward from its opening bracket
        ("ctrl+k", 8, "see "),  # killing from inside it to the end
    ],
)
def test_every_delete_takes_a_whole_token(tmp_path: Path, key: str, cursor: int, left: str) -> None:
    prompt = Prompt(tmp_path)
    prompt.handle_input("text", "see")
    prompt.attach_image(PICTURE)
    prompt.handle_input("text", "and")
    assert prompt.text == "see [Image #1] and"
    prompt.buffer.cursor = cursor
    prompt.handle_input(key, "")
    assert prompt.text == left


def test_a_whitespace_paste_is_text_not_an_image_request(tmp_path: Path) -> None:
    prompt = Prompt(tmp_path)
    asked: list[object] = []
    prompt.on_image_request = asked.append
    prompt.handle_input("paste", "    ")
    assert asked == []
    assert prompt.text == "    "


def test_a_pasted_image_path_asks_for_the_image(tmp_path: Path) -> None:
    (tmp_path / "Screen Shot.png").write_bytes(png())
    prompt = Prompt(tmp_path)
    asked: list[object] = []
    prompt.on_image_request = asked.append

    prompt.handle_input("paste", str(tmp_path / "Screen Shot.png").replace(" ", "\\ "))
    prompt.handle_input("paste", "")  # a terminal's paste of a picture-only clipboard
    prompt.handle_input("ctrl+v", "")
    prompt.handle_input("paste", "some words")

    assert asked == [[tmp_path / "Screen Shot.png"], None, None]
    assert prompt.text == "some words"


def test_numbering_carries_on_past_images_already_sent(tmp_path: Path) -> None:
    prompt = Prompt(tmp_path)
    prompt.remember_images([replace(PICTURE, label="Image #4"), replace(PICTURE, label="x.png")])
    assert prompt.attach_image(PICTURE).label == "Image #5"
    # A recalled message still resolves the image it was sent with.
    assert [i.label for i in prompt.images_in("again [Image #4]")] == ["Image #4"]


def test_a_sent_message_lists_its_images_under_the_text() -> None:
    block = UserMessage("look [Image #1]", [replace(PICTURE, data="A" * 4000, label="Image #1")])
    lines = [line.strip() for line in plain(block.render(60))]
    assert "look [Image #1]" in lines
    assert "▣ Image #1 · 1440x900 · 3 kB" in lines


def test_a_long_image_line_is_clipped_to_the_width() -> None:
    block = UserMessage("x", [replace(PICTURE, label="a-very-long-file-name" * 5)])
    assert_lines_fit(block, 40)
    assert any(line.strip().startswith("▣ a-very-long") for line in plain(block.render(40)))


# -- the session --------------------------------------------------------------


def seeing(session: object) -> None:
    loop = session.loop  # type: ignore[attr-defined]
    loop.model_info = replace(loop.model_info, supports_images=True)


async def test_ctrl_v_attaches_the_clipboard_image_and_enter_sends_it(
    hx_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_read(**_: object) -> clipboard.ClipboardImage:
        return clipboard.ClipboardImage(png((64, 48)))

    monkeypatch.setattr(clipboard, "read_image", fake_read)
    session = build_session(tmp_path)
    seeing(session)

    async with Driver(session) as driver:
        driver.type("what is this ")
        driver.type("\x16")  # ctrl+v
        await driver.settle()
        assert session.prompt.text == "what is this [Image #1] "

        driver.type("\r")
        await driver.settle(rounds=40)

    sent = session.loop.provider.requests[0].context.messages[-1]
    [image] = sent.images()
    assert (image.label, image.width, image.height) == ("Image #1", 64, 48)
    assert "▣ Image #1 · 64x48" in driver.transcript_text()


async def test_an_empty_clipboard_says_so(
    hx_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def nothing(**_: object) -> None:
        return None

    monkeypatch.setattr(clipboard, "read_image", nothing)
    session = build_session(tmp_path)
    async with Driver(session) as driver:
        driver.type("\x16")
        await driver.settle()
    assert "No image on the clipboard." in driver.transcript_text()
    assert session.prompt.text == ""


async def test_an_unforeseen_paste_failure_is_reported(
    hx_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(**_: object) -> None:
        raise OSError("scratch file not writable")

    monkeypatch.setattr(clipboard, "read_image", broken)
    session = build_session(tmp_path)
    async with Driver(session) as driver:
        driver.type("\x16")
        await driver.settle()
    assert "Could not attach the image: scratch file not writable" in driver.transcript_text()


async def test_windows_hands_powershell_the_target_path_through_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``-Command`` folds trailing arguments into the script, so ``$args`` is empty."""
    seen: list[tuple[list[str], dict[str, str] | None]] = []

    async def capture(
        command: list[str], *, env: dict[str, str] | None = None
    ) -> tuple[bool, bytes]:
        seen.append((command, env))
        assert env is not None
        Path(env["HX_CLIPBOARD_OUT"]).write_bytes(png())
        return True, b"image\r\n"

    monkeypatch.setattr(clipboard, "_capture", capture)
    copied = await clipboard.read_image(platform="win32", env={})

    assert copied is not None and copied.data == png()
    [(command, _)] = seen
    assert command[-1] == clipboard._WINDOWS_READ_IMAGE
    assert "$env:HX_CLIPBOARD_OUT" in command[-1]


def test_a_copied_file_too_large_is_refused_before_it_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hx.core import images

    big = tmp_path / "huge.tiff"
    big.write_bytes(b"x" * 2048)
    monkeypatch.setattr(images, "MAX_SOURCE_BYTES", 1024)
    monkeypatch.setattr(Path, "read_bytes", lambda _: pytest.fail("read the whole file"))
    with pytest.raises(clipboard.ClipboardError, match="too large"):
        clipboard._copied_file(big)


async def test_a_dragged_in_file_is_attached(hx_home: Path, tmp_path: Path) -> None:
    (tmp_path / "mock.png").write_bytes(png())
    session = build_session(tmp_path)
    seeing(session)
    async with Driver(session) as driver:
        driver.type(f"\x1b[200~{tmp_path / 'mock.png'}\x1b[201~")
        await driver.settle()
    assert session.prompt.text == "[Image #1] "
    # Named by its token, and still known by the file it came from.
    [image] = session.prompt.images_in(session.prompt.text)
    assert image.caption() == "Image #1: mock.png"


async def test_a_file_that_is_not_an_image_is_reported(hx_home: Path, tmp_path: Path) -> None:
    (tmp_path / "fake.png").write_bytes(b"not really")
    session = build_session(tmp_path)
    async with Driver(session) as driver:
        driver.type(f"\x1b[200~{tmp_path / 'fake.png'}\x1b[201~")
        await driver.settle()
    assert "Could not attach fake.png: not a readable image" in driver.transcript_text()
    assert session.prompt.text == ""


async def test_attaching_to_a_text_only_model_warns(
    hx_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_read(**_: object) -> clipboard.ClipboardImage:
        return clipboard.ClipboardImage(png())

    monkeypatch.setattr(clipboard, "read_image", fake_read)
    session = build_session(tmp_path)
    async with Driver(session) as driver:
        driver.type("\x16")
        await driver.settle()
    assert "does not accept images" in driver.transcript_text()
    # Attached anyway: the transcript keeps it for a model that can see it.
    assert session.prompt.text == "[Image #1] "


async def test_a_queued_message_keeps_its_images(hx_home: Path, tmp_path: Path) -> None:
    import asyncio
    from collections.abc import Sequence

    session = build_session(tmp_path)
    seeing(session)
    release = asyncio.Event()
    seen: list[list[str]] = []
    run_turn = session.loop.run

    async def held(text: str, images: Sequence[ImageBlock] = ()) -> object:
        seen.append([image.label for image in images])
        if text == "one":
            await release.wait()
        return await run_turn(text, images)

    session.loop.run = held  # type: ignore[method-assign]
    async with Driver(session) as driver:
        driver.type("one\r")
        await driver.settle()
        session.prompt.attach_image(PICTURE)
        driver.type("\r")
        await driver.settle()
        release.set()
        await driver.settle(rounds=60)
    assert seen == [[], ["Image #1"]]


async def test_a_resumed_session_redraws_its_images(hx_home: Path, tmp_path: Path) -> None:
    session = build_session(tmp_path)
    labelled = replace(PICTURE, label="Image #2")
    session.loop.session.append(user_message("old [Image #2]", [labelled]))
    async with Driver(session) as driver:
        session._replay_transcript()
        await driver.settle()
    assert "▣ Image #2 · 1440x900" in driver.transcript_text()
    assert session.prompt.attach_image(PICTURE).label == "Image #3"
