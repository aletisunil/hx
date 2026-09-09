"""Copying to the system clipboard from inside a terminal.

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
import os
import sys

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
