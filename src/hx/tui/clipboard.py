"""The system clipboard, from inside a terminal: copying text out, and reading
an image in.

There is no one way to do this, so this does what pi does and tries them in a
deliberate order:

1. The platform's own clipboard command, first, so the terminal cannot race the
   write. On Linux the tool depends on the session (Wayland, X11, Termux), so
   the candidates are chosen from the environment rather than guessed.
2. OSC 52, which asks the terminal to do it. Always attempted over SSH, where
   the platform command would write to the *remote* clipboard and the user is
   looking at their local one, and as the fallback when step 1 found nothing.

``App.copy_to_clipboard`` is OSC 52 only, and macOS Terminal ignores OSC 52 -
which is exactly the case ``pbcopy`` covers, and why step 1 exists.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

#: Terminals cut the escape sequence off somewhere past this, and a silently
#: truncated clipboard is worse than a refusal.
MAX_OSC52_ENCODED = 100_000

_TIMEOUT = 5.0


class ClipboardError(RuntimeError):
    """Nothing available on this machine could take the text."""


def is_remote_session(env: dict[str, str] | None = None) -> bool:
    environ = env if env is not None else dict(os.environ)
    return any(environ.get(name) for name in ("SSH_CONNECTION", "SSH_CLIENT", "MOSH_CONNECTION"))


def commands_for(platform: str, env: dict[str, str] | None = None) -> list[list[str]]:
    """Clipboard commands to try, best first, for this platform and session."""
    environ = env if env is not None else dict(os.environ)
    if platform == "darwin":
        return [["pbcopy"]]
    if platform == "win32":
        return [["clip"]]

    candidates: list[list[str]] = []
    if environ.get("TERMUX_VERSION"):
        candidates.append(["termux-clipboard-set"])
    if environ.get("WAYLAND_DISPLAY"):
        candidates.append(["wl-copy"])
    if environ.get("DISPLAY"):
        candidates.append(["xclip", "-selection", "clipboard"])
        candidates.append(["xsel", "--clipboard", "--input"])
    return candidates


async def _run(command: list[str], text: str) -> bool:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        return False
    try:
        await asyncio.wait_for(process.communicate(text.encode("utf-8")), timeout=_TIMEOUT)
    except (TimeoutError, OSError):
        process.kill()
        return False
    return process.returncode == 0


def osc52_sequence(text: str) -> str | None:
    """The escape sequence for ``text``, or None when it is too long to send."""
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    if len(encoded) > MAX_OSC52_ENCODED:
        return None
    return f"\x1b]52;c;{encoded}\a"


async def copy_text(
    text: str,
    *,
    write_osc52: object = None,
    platform: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Copy ``text``, returning the mechanism that took it.

    ``write_osc52`` is the app's own OSC 52 writer (``App.copy_to_clipboard``),
    passed in rather than imported so this module stays testable without a
    running Textual app.
    """
    if not text:
        raise ClipboardError("nothing to copy")

    system = platform or sys.platform
    used = ""
    for command in commands_for(system, env):
        if await _run(command, text):
            used = command[0]
            break

    remote = is_remote_session(env)
    if (not used or remote) and write_osc52 is not None and osc52_sequence(text) is not None:
        write_osc52(text)  # type: ignore[operator]
        used = used or "OSC 52"

    if not used:
        raise ClipboardError("no clipboard tool available (tried pbcopy/wl-copy/xclip and OSC 52)")
    return used


def format_size(text: str) -> str:
    """``1.2 kB`` - what the copy notice reports, so a silent no-op is visible."""
    size = len(text.encode("utf-8"))
    if size < 1000:
        return f"{size} B"
    if size < 1_000_000:
        return f"{size / 1000:.1f} kB"
    return f"{size / 1_000_000:.1f} MB"


# -- reading an image -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClipboardImage:
    data: bytes
    name: str | None = None
    """The file's name when a file was copied rather than a picture."""


_MACOS_READ_IMAGE = """\
on run argv
    try
        set copiedFile to the clipboard as «class furl»
        return "file:" & POSIX path of copiedFile
    end try
    try
        set imageData to the clipboard as «class PNGf»
    on error
        try
            set imageData to the clipboard as TIFF picture
        on error
            return "none"
        end try
    end try
    set outFile to open for access (POSIX file (item 1 of argv)) with write permission
    set eof outFile to 0
    write imageData to outFile
    close access outFile
    return "image"
end run
"""
"""A copied file first: Finder puts the file's icon on the clipboard beside its
path, and pasting a copied screenshot file should attach the screenshot, not a
picture of a document icon."""

_WINDOWS_READ_IMAGE = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "$i=[Windows.Forms.Clipboard]::GetImage();"
    "if($i){$i.Save($env:HX_CLIPBOARD_OUT,[Drawing.Imaging.ImageFormat]::Png);'image'}"
    "else{'none'}"
)
"""The target path comes in through the environment: ``-Command`` folds every
argument after it into the script text, so ``$args`` is always empty there."""


async def read_image(
    *, platform: str | None = None, env: dict[str, str] | None = None
) -> ClipboardImage | None:
    """The image on the clipboard, or ``None`` when it holds none.

    Raises:
        ClipboardError: when the clipboard cannot be read at all - over SSH,
            where the clipboard the user copied into is on another machine, or
            with no clipboard tool installed.
    """
    system = platform or sys.platform
    if is_remote_session(env):
        raise ClipboardError(
            "the clipboard is on your own machine, not this one - "
            "copy the image file over and paste its path instead"
        )
    if system == "darwin":
        return await _read_macos()
    if system == "win32":
        return await _read_windows()
    return await _read_linux(dict(os.environ) if env is None else env)


async def _read_macos() -> ClipboardImage | None:
    with _scratch_file() as target:
        ok, out = await _capture(["osascript", "-e", _MACOS_READ_IMAGE, str(target)])
        if not ok:
            raise ClipboardError("osascript could not read the clipboard")
        answer = out.decode("utf-8", "replace").strip()
        if answer.startswith("file:"):
            return _copied_file(Path(answer.removeprefix("file:")))
        if answer == "image":
            return ClipboardImage(target.read_bytes())
    return None


async def _read_windows() -> ClipboardImage | None:
    with _scratch_file() as target:
        ok, out = await _capture(
            ["powershell", "-NoProfile", "-STA", "-Command", _WINDOWS_READ_IMAGE],
            env={**os.environ, "HX_CLIPBOARD_OUT": str(target)},
        )
        if not ok:
            raise ClipboardError("PowerShell could not read the clipboard")
        if out.decode("utf-8", "replace").strip() == "image":
            return ClipboardImage(target.read_bytes())
    return None


async def _read_linux(env: dict[str, str]) -> ClipboardImage | None:
    """``wl-paste`` on Wayland, ``xclip`` on X11: list what the clipboard
    offers, then ask for a file list or the first image type in it."""
    if env.get("WAYLAND_DISPLAY"):
        list_types, fetch = ["wl-paste", "--list-types"], ["wl-paste", "--no-newline", "--type"]
    elif env.get("DISPLAY"):
        list_types = ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"]
        fetch = ["xclip", "-selection", "clipboard", "-o", "-t"]
    else:
        raise ClipboardError("no clipboard tool available (tried wl-paste and xclip)")

    ok, out = await _capture(list_types)
    if not ok:
        raise ClipboardError(f"{list_types[0]} could not read the clipboard")
    offered = out.decode("utf-8", "replace").split()

    if "text/uri-list" in offered:
        ok, uris = await _capture([*fetch, "text/uri-list"])
        if ok:
            for line in uris.decode("utf-8", "replace").splitlines():
                if line.startswith("file://"):
                    from urllib.parse import unquote, urlparse

                    copied = _copied_file(Path(unquote(urlparse(line.strip()).path)))
                    if copied is not None:
                        return copied
    kind = next(
        (t for t in ("image/png", *offered) if t in offered and t.startswith("image/")), None
    )
    if kind is None:
        return None
    ok, data = await _capture([*fetch, kind])
    return ClipboardImage(data) if ok and data else None


def _copied_file(path: Path) -> ClipboardImage | None:
    """A file copied in the file manager, when it is an image."""
    from hx.core.images import MAX_SOURCE_BYTES, is_image_path

    if not is_image_path(path):
        return None
    try:
        # Checked before reading: the whole file lands in memory otherwise,
        # only for the decoder to refuse it.
        if path.stat().st_size > MAX_SOURCE_BYTES:
            raise ClipboardError(f"{path.name} is too large to attach")
        return ClipboardImage(path.read_bytes(), name=path.name)
    except OSError as exc:
        raise ClipboardError(f"cannot read {path}: {exc.strerror or exc}") from None


async def _capture(command: list[str], *, env: dict[str, str] | None = None) -> tuple[bool, bytes]:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        return False, b""
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=_TIMEOUT)
    except (TimeoutError, OSError):
        process.kill()
        return False, b""
    return process.returncode == 0, out


@contextlib.contextmanager
def _scratch_file() -> Iterator[Path]:
    """A path for a clipboard tool to write the image to, removed afterwards."""
    handle, name = tempfile.mkstemp(prefix="hx-clipboard-", suffix=".img")
    os.close(handle)
    path = Path(name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)
