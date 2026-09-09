"""Clipboard: the right tool per platform, and OSC 52 where it is the only option."""

from __future__ import annotations

from typing import Any

import pytest

from hx.tui import clipboard
from hx.tui.clipboard import ClipboardError, commands_for, copy_text, format_size, osc52_sequence


def _fake_runner(succeeds: set[str], log: list[list[str]]) -> Any:
    async def run(command: list[str], text: str) -> bool:
        log.append(command)
        return command[0] in succeeds

    return run


def test_macos_uses_pbcopy() -> None:
    assert commands_for("darwin") == [["pbcopy"]]


def test_linux_picks_the_tool_for_the_session() -> None:
    wayland = commands_for("linux", {"WAYLAND_DISPLAY": "wayland-0"})
    assert wayland == [["wl-copy"]]

    x11 = commands_for("linux", {"DISPLAY": ":0"})
    assert [command[0] for command in x11] == ["xclip", "xsel"]

    termux = commands_for("linux", {"TERMUX_VERSION": "0.118"})
    assert termux == [["termux-clipboard-set"]]


def test_a_bare_linux_session_has_no_command() -> None:
    """Nothing to run, so OSC 52 has to carry it - which is the SSH case."""
    assert commands_for("linux", {}) == []


async def test_the_platform_command_is_tried_first(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[list[str]] = []
    monkeypatch.setattr(clipboard, "_run", _fake_runner({"pbcopy"}, log))
    written: list[str] = []

    used = await copy_text("hello", write_osc52=written.append, platform="darwin", env={})

    assert used == "pbcopy"
    assert log == [["pbcopy"]]
    # Not over SSH, and the command worked, so the terminal is left alone.
    assert written == []


async def test_ssh_always_also_sends_osc52(monkeypatch: pytest.MonkeyPatch) -> None:
    """The platform command would write to the wrong machine's clipboard."""
    monkeypatch.setattr(clipboard, "_run", _fake_runner({"pbcopy"}, []))
    written: list[str] = []

    await copy_text(
        "hello",
        write_osc52=written.append,
        platform="darwin",
        env={"SSH_CONNECTION": "1.2.3.4 22 5.6.7.8 22"},
    )

    assert written == ["hello"]


async def test_osc52_carries_it_when_no_command_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clipboard, "_run", _fake_runner(set(), []))
    written: list[str] = []

    used = await copy_text("hello", write_osc52=written.append, platform="darwin", env={})

    assert used == "OSC 52"
    assert written == ["hello"]


async def test_failure_is_raised_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clipboard, "_run", _fake_runner(set(), []))
    with pytest.raises(ClipboardError):
        await copy_text("hello", write_osc52=None, platform="darwin", env={})


async def test_nothing_to_copy_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ClipboardError):
        await copy_text("", write_osc52=None, platform="darwin", env={})


def test_oversize_payloads_skip_osc52() -> None:
    """A terminal truncates past this, and half a clipboard is worse than none."""
    assert osc52_sequence("x" * 10) is not None
    assert osc52_sequence("x" * (clipboard.MAX_OSC52_ENCODED)) is None


async def test_an_oversize_payload_still_uses_the_platform_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clipboard, "_run", _fake_runner({"pbcopy"}, []))
    written: list[str] = []
    huge = "x" * clipboard.MAX_OSC52_ENCODED

    used = await copy_text(huge, write_osc52=written.append, platform="darwin", env={})

    assert used == "pbcopy"
    assert written == []


def test_sizes_are_reported_in_units_a_human_reads() -> None:
    assert format_size("x" * 12) == "12 B"
    assert format_size("x" * 1500) == "1.5 kB"
