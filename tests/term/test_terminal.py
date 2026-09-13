"""Owning the terminal, and - the part that matters - giving it back.

Every mode HX turns on has to come off again on every path out of the process.
When it does not, the user lands in a shell with no echo and no cursor, and
the only fix is typing ``reset`` blind. These run the real thing under a pty
and read back what the terminal would have received.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from hx.term.terminal import DEFAULT_SIZE, FakeTerminal, ProcessTerminal

REPO = Path(__file__).resolve().parents[2]

MARKER = "up and running"

CHILD = """
import asyncio, os, sys
sys.path.insert(0, {repo!r})
from hx.term.terminal import ProcessTerminal

async def main():
    term = ProcessTerminal()
    term.start(lambda data: None, lambda: None)
    term.set_mouse(True)
    term.write({marker!r} + "\\r\\n")
    await asyncio.sleep(0.02)
    how = {how!r}
    if how == "clean":
        term.stop()
    elif how == "exit":
        sys.exit(3)
    elif how == "exception":
        raise RuntimeError("boom")
    elif how == "signal":
        os.kill(os.getpid(), {signum})
        await asyncio.sleep(1)

asyncio.run(main())
"""

#: Turning a mode on is one write that cannot fail; turning it off has to
#: happen on every exit path. Each of these is what "off" looks like.
OFF_SEQUENCES = {
    "cursor shown": "\x1b[?25h",
    "bracketed paste off": "\x1b[?2004l",
    "mouse off": "\x1b[?1000l",
    "attributes reset": "\x1b[0m",
}


def _run_under_pty(how: str, signum: int = 0) -> str:
    """Run a child that dies the given way; return everything the pty saw.

    The pty is allocated here and handed to :mod:`subprocess` rather than using
    :func:`pty.fork`, which forks without exec'ing and is unsafe - and warns -
    in a process that has threads running, as the test suite does.
    """
    source = CHILD.format(repo=str(REPO / "src"), marker=MARKER, how=how, signum=signum)
    controller, follower = os.openpty()
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", source],
            stdin=follower,
            stdout=follower,
            stderr=follower,
            start_new_session=True,
            close_fds=True,
        )
        # The parent's copy has to go, or the read below never sees EOF.
        os.close(follower)
        follower = -1

        chunks = []
        try:
            while True:
                data = os.read(controller, 65536)
                if not data:
                    break
                chunks.append(data)
        except OSError:
            pass
        process.wait(timeout=30)
        return b"".join(chunks).decode("utf-8", errors="replace")
    finally:
        os.close(controller)
        if follower != -1:
            os.close(follower)


def _after_startup(output: str) -> str:
    """Only what was written once the app was up counts as a restore.

    The same sequences appear at startup, where they are the sanitize pass
    cleaning up after a previous session rather than this one tidying up.
    """
    _, _, tail = output.partition(MARKER)
    return tail


@pytest.mark.parametrize(
    ("how", "signum"),
    [
        ("clean", 0),
        ("exit", 0),
        ("exception", 0),
        ("signal", 15),  # SIGTERM
        ("signal", 1),  # SIGHUP
    ],
    ids=["clean exit", "sys.exit", "unhandled exception", "SIGTERM", "SIGHUP"],
)
@pytest.mark.parametrize("mode", list(OFF_SEQUENCES), ids=list(OFF_SEQUENCES))
def test_the_terminal_is_handed_back_on_every_exit_path(how: str, signum: int, mode: str) -> None:
    tail = _after_startup(_run_under_pty(how, signum))
    assert OFF_SEQUENCES[mode] in tail, f"{mode} was left on after {how}"


def test_a_kill_cannot_be_caught_but_the_next_start_cleans_up_after_it() -> None:
    """SIGKILL runs no handler, by definition - not here and not in any other
    framework. What is available is making the next session sanitize first, so
    the fix for a terminal left broken by a kill is to run ``hx`` again."""
    killed = _run_under_pty("signal", 9)
    assert "\x1b[?25h" not in _after_startup(killed), "a kill cannot restore anything"

    fresh = _run_under_pty("clean")
    startup, _, _ = fresh.partition(MARKER)
    for name, sequence in OFF_SEQUENCES.items():
        assert sequence in startup, f"a new session does not turn {name} off first"


def test_the_alternate_screen_is_never_entered() -> None:
    """The transcript is the terminal's own scrollback. That is the point of
    the renderer, and it is lost the moment anything switches screens."""
    for how in ("clean", "exception"):
        assert "\x1b[?1049h" not in _run_under_pty(how)


@pytest.fixture
def detached() -> Iterator[ProcessTerminal]:
    """A terminal over real handles that are not a tty.

    pytest replaces ``sys.stdin`` with a pseudofile that has no descriptor, and
    :class:`ProcessTerminal` needs one to read from and to measure.
    """
    with open(os.devnull) as stdin:
        yield ProcessTerminal(stdin=stdin, stdout=io.StringIO())


def test_restore_is_idempotent(detached: ProcessTerminal) -> None:
    """It is reached from atexit, from the exception hook and from a signal,
    so it will routinely be called more than once."""
    detached.restore()
    detached.restore()


def test_size_falls_back_when_there_is_no_terminal(detached: ProcessTerminal) -> None:
    """A pipe or a CI log has no size, and rendering into zero columns divides
    by it."""
    assert detached.size == DEFAULT_SIZE


def test_a_zero_column_size_is_treated_as_missing(
    detached: ProcessTerminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal mid-resize, or a pty that has not been sized yet."""
    monkeypatch.setattr("os.get_terminal_size", lambda _fd: os.terminal_size((0, 0)))
    assert detached.size == DEFAULT_SIZE


def test_the_fake_terminal_records_what_it_was_given() -> None:
    fake = FakeTerminal(100, 30)
    assert fake.size == (100, 30)
    fake.write("a")
    fake.write("b")
    assert fake.output == "ab"

    seen: list[str] = []
    resized: list[int] = []
    fake.start(seen.append, lambda: resized.append(1))
    fake.feed("x")
    fake.resize(40, 10)
    assert seen == ["x"]
    assert resized == [1]
    assert fake.size == (40, 10)

    fake.stop()
    assert fake.restored
