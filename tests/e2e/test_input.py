"""Typing into the prompt: newlines, the keyboard protocols, and pasting.

Shift+enter only exists if the terminal is asked for it. Each test here says
which terminal it runs against - see :data:`tests.e2e.terminal.KEYBOARDS` - and
that terminal sends the extended codes only once HX has negotiated them, as a
real one does. The last tests drive a real tmux, the one terminal emulator
that can be run headless and that implements extended keys itself.
"""

from __future__ import annotations

import html
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable

import pytest

from tests.e2e.conftest import HX
from tests.e2e.report import Step
from tests.e2e.stub import Stub, say
from tests.e2e.terminal import KEYBOARDS, Terminal

TABLE_ROWS = 40
TABLE = "\n".join(
    [
        "| id | service | status |",
        "|---|---|---|",
        *(
            f"| {n} | svc-{n:02} | {'degraded' if n % 7 == 0 else 'ok'} |"
            for n in range(1, TABLE_ROWS - 1)
        ),
    ]
)
"""A 40-line markdown table: large enough that pasting it collapses."""


def _no_request(stub: Stub) -> None:
    time.sleep(0.3)  # long enough for a wrongly-submitted draft to reach the stub
    assert not stub.requests, "the draft was sent when it should have gained a line"


# -- shift+enter, per terminal -------------------------------------------------


def test_shift_enter_in_a_kitty_protocol_terminal(hx: HX, stub: Stub) -> None:
    """In a kitty-protocol terminal (kitty, Ghostty, WezTerm, iTerm2) shift+enter
    adds lines to a draft; enter sends the whole table, which the transcript draws
    as a table; leaving pops the protocol off the terminal's stack."""
    stub.script(say("Two services, one degraded."))
    term = hx.tui(keyboard="kitty")
    assert term.kitty_stacks["main"] == [5], "disambiguate + alternate keys pushed"
    assert term.modify_other_keys == 0, "no fallback when kitty answered"

    rows = ["| service | status |", "|---|---|", "| api | ok |", "| worker | degraded |"]
    for index, row in enumerate(rows):
        term.type(row)
        if index < len(rows) - 1:
            term.press("shift+enter")
    term.wait_for("| worker | degraded |")
    term.snapshot("four lines typed with shift+enter")
    screen = term.text()
    for row in rows:
        assert f" {row}" in screen, "each row is its own line of the draft"
    _no_request(stub)

    term.press("enter")
    term.wait_for("Two services, one degraded.")
    term.settle()
    term.snapshot("the table as sent")
    (request,) = stub.requests
    assert request.last_user_text().endswith("\n".join(rows))
    screen = term.text()
    assert "│ worker  │ degraded │" in screen, "the sent table is drawn as a table"
    assert "|---|" not in screen

    term.press("ctrl+d")
    assert term.wait_exit() == 0
    assert term.kitty_stacks == {"main": [], "alt": []}, "everything pushed was popped"


def test_shift_enter_falls_back_to_modify_other_keys(hx: HX, stub: Stub) -> None:
    """A terminal without the kitty protocol answers only the device-attributes
    query, and HX switches to xterm's modifyOtherKeys: shift+enter still adds a
    line, ctrl and alt keys still work, and the mode is turned off on exit."""
    stub.script(say("Noted both lines."))
    term = hx.tui(keyboard="xterm")
    assert term.modify_other_keys == 2
    assert "ESC[>4;2m" in term.keyboard_log

    term.type("first line")
    term.press("shift+enter")
    term.type("second wrong")
    term.press("ctrl+w")  # arrives as CSI 27;5;119~
    term.type("line")
    term.wait_for(lambda screen: " second line" in screen and "wrong" not in screen)
    _no_request(stub)
    term.press("ctrl+a", "ctrl+k")  # both through modifyOtherKeys too
    term.wait_gone("second line")
    term.type("second line")
    term.press("enter")
    term.wait_for("Noted both lines.")
    term.settle()
    term.snapshot("both lines in the transcript")
    assert stub.requests[0].last_user_text().endswith("first line\nsecond line")
    lines = term.lines()
    assert " first line" in lines and " second line" in lines, "typed lines stay lines"

    term.press("ctrl+d")
    assert term.wait_exit() == 0
    assert term.modify_other_keys == 0, "modifyOtherKeys turned off again"


def test_legacy_terminal_newline_fallbacks(hx: HX, stub: Stub) -> None:
    """Apple's Terminal cannot send shift+enter at all. A trailing backslash then
    enter, or ctrl+j, adds the line instead; a backslash anywhere else is text,
    and enter after it still sends - as does enter after an escaped one."""
    stub.script(say("Got the path."), say("Got the lines."), say("Got the regex."))
    term = hx.tui(keyboard="legacy")
    assert term.kitty_stacks["main"] == [] and term.modify_other_keys == 0

    term.type("C:\\temp\\logs is the path")
    term.press("enter")
    term.wait_for("Got the path.")
    assert stub.requests[0].last_user_text().endswith("C:\\temp\\logs is the path")

    term.type("one\\")
    term.press("enter")
    term.type("two")
    term.press("ctrl+j")
    term.type("three")
    term.wait_for(" three")
    term.snapshot("three lines from backslash-enter and ctrl+j")
    assert " one" in term.text() and " two" in term.text()
    assert "one\\" not in term.text(), "the backslash is consumed"
    time.sleep(0.3)
    assert len(stub.requests) == 1

    term.press("enter")
    term.wait_for("Got the lines.")
    assert stub.requests[1].last_user_text().endswith("one\ntwo\nthree")

    # A backslash escapes itself, so a message can still end in one.
    term.type("the regex ends in \\\\")
    term.press("enter")
    term.wait_for("Got the regex.")
    assert stub.requests[2].last_user_text().endswith("the regex ends in \\")


def test_shift_enter_survives_fullscreen_and_suspend(hx: HX, stub: Stub) -> None:
    """The alternate screen keeps its own kitty stack, so /fullscreen pushes the
    flags there too; suspending pops everything and resuming pushes it back.
    Shift+enter works on both screens and after a suspend, and nothing is left on
    either stack. Suspend has no key by default - ctrl+z is undo - so it is bound
    the way a user would, in keybindings.json, spelled with cmd."""
    stub.script(say("Three lines received."))
    (hx.hx_home / "keybindings.json").write_text('{"app.suspend": "cmd+s"}')
    term = hx.tui(keyboard="kitty")
    term.submit("/fullscreen on")
    term.wait_for(lambda _: term.alt_screen)
    term.settle()
    assert term.kitty_stacks == {"main": [5], "alt": [5]}

    term.type("on the alternate screen")
    term.press("shift+enter")
    term.type("still one draft")
    term.wait_for(" still one draft")
    _no_request(stub)

    term.press("ctrl+c")  # clear the draft before leaving fullscreen
    term.wait_gone("still one draft")
    term.submit("/fullscreen off")
    term.wait_for(lambda _: not term.alt_screen)
    term.settle()
    assert term.kitty_stacks == {"main": [5], "alt": []}

    popped_before = term.keyboard_log.count("ESC[<u")
    term.press("cmd+s")
    # Under a pty with no job-control shell the stop is a no-op, so HX comes
    # straight back - through the same pop, stop and push a real suspend takes.
    term.wait_for(lambda _: term.keyboard_log.count("ESC[<u") > popped_before)
    term.settle()
    assert term.keyboard_log[-2:] == ["ESC[<u", "ESC[>5u"], "popped, then pushed on resume"
    assert term.kitty_stacks["main"] == [5]
    if term.alive:
        os.kill(term.pid, signal.SIGCONT)

    term.type("after resume")
    term.press("shift+enter")
    term.type("second")
    term.press("shift+enter")
    term.type("third")
    term.press("enter")
    term.wait_for("Three lines received.")
    assert stub.requests[0].last_user_text().endswith("after resume\nsecond\nthird")

    term.press("ctrl+d")
    assert term.wait_exit() == 0
    assert term.kitty_stacks == {"main": [], "alt": []}


@pytest.mark.parametrize("keyboard", KEYBOARDS)
def test_undo_and_redo_keys(hx: HX, stub: Stub, keyboard: str) -> None:
    """ctrl+z undoes in every terminal and no longer suspends HX; ctrl+_ still
    undoes too. ctrl+shift+z redoes where the terminal can tell it from ctrl+z,
    and cmd+z / cmd+shift+z work in a kitty-protocol terminal, which passes cmd
    through as super. A legacy terminal sends ctrl+shift+z as ctrl+z itself."""
    stub.script(say("Undo worked."))
    term = hx.tui(keyboard=keyboard)
    # The key list says so, and has no row for suspend, which has no key.
    term.press("ctrl+o")
    term.wait_for("Undo")
    listed = {line.split()[-1]: line.split()[0] for line in term.lines() if line.strip()}
    assert listed["Undo"].startswith("ctrl+z/") and listed["Redo"].startswith("ctrl+shift+z/")
    assert "Suspend" not in term.text()
    pops = term.keyboard_log.count("ESC[<u")
    term.type("keep this")
    term.press("ctrl+w")
    term.wait_gone("keep this")
    term.press("ctrl+z")
    term.wait_for(" keep this")
    assert term.alive and term.keyboard_log.count("ESC[<u") == pops, "undid, not suspended"

    if keyboard != "legacy":
        term.press("ctrl+shift+z")
        term.wait_gone("keep this")
        term.press("ctrl+_")
        term.wait_for(" keep this")
    if keyboard == "kitty":
        term.press("cmd+shift+z")
        term.wait_gone("keep this")
        term.press("cmd+z")
        term.wait_for(" keep this")
    term.snapshot(f"undone in a {keyboard} terminal")

    term.press("enter")
    term.wait_for("Undo worked.")
    assert stub.requests[0].last_user_text().endswith("keep this")


# -- soft wrap -----------------------------------------------------------------


def _draft(term: Terminal) -> list[str]:
    """The draft's rows: what lies between the prompt's two rules."""
    lines = term.lines()
    rules = [i for i, line in enumerate(lines) if line.startswith("────")]
    top, bottom = rules[-2], rules[-1]
    return [line[1:] for line in lines[top + 1 : bottom]]


def test_draft_wraps_at_spaces(hx: HX, stub: Stub) -> None:
    """A long draft wraps between words, not through them. A word that exactly
    fills a row leaves its space hanging past the edge, with the cursor still
    drawn on it; a word longer than a row still breaks; and editing mid-draft
    reflows around the cursor and sends exactly what was typed."""
    stub.script(say("Wrapped."))
    term = hx.tui(columns=40)  # 38 cells of draft between the paddings
    # The welcome above it wraps the same way rather than being cut off.
    assert [line.strip() for line in term.lines()[1:3]] == [
        "An agent harness. Ask a question, or",
        "start with /help.",
    ]
    sentence = "Postgres is a relational store with strong consistency and mature tooling"
    term.type(sentence)
    term.wait_for("tooling")
    term.snapshot("a sentence wrapped between words")
    rows = _draft(term)
    assert rows == [
        "Postgres is a relational store with",
        "strong consistency and mature tooling",
    ]

    # Clearing and typing a 38-letter word, then more: the space the break
    # falls on hangs past the edge rather than starting the next row.
    term.press("ctrl+c")
    term.wait_gone("tooling")
    full = "x" * 38
    term.type(f"{full} next")
    term.wait_for(" next")
    assert _draft(term) == [full, "next"]
    term.press("left", "left", "left", "left")
    term.settle()
    assert term.cursor() == (1, rows_at(term, "next")), "before 'next', start of its row"
    term.press("left")
    term.settle()
    assert term.cursor() == (39, rows_at(term, full)), "on the hanging space, past the edge"
    term.snapshot("the cursor on a hanging space")

    # Typing there joins the word with no space left to break at: it breaks hard.
    term.type("!")
    term.wait_for("! next")
    assert _draft(term) == [full, "! next"]

    # A URL longer than a row breaks inside it, the only place it can.
    term.press("ctrl+c")
    term.wait_gone("next")
    url = "see https://example.com/" + "a" * 40
    term.type(url)
    term.wait_for("aaaa")
    assert _draft(term) == ["see", "https://example.com/" + "a" * 18, "a" * 22]

    term.press("ctrl+a", "right", "right", "right")
    term.type(" also")
    term.wait_for("see also")
    assert _draft(term)[0] == "see also"
    term.press("enter")
    term.wait_for("Wrapped.")
    assert stub.requests[0].last_user_text().endswith("see also https://example.com/" + "a" * 40)


def rows_at(term: Terminal, text: str) -> int:
    """The screen row the draft line ``text`` is drawn on."""
    return next(y for y, line in enumerate(term.lines()) if line[1:] == text)


# -- pasting -------------------------------------------------------------------


@pytest.mark.parametrize("newline", ["\r", "\r\n", "\n"], ids=["cr", "crlf", "lf"])
def test_multiline_paste_keeps_its_lines(hx: HX, stub: Stub, newline: str) -> None:
    """A small multi-line paste lands as lines of the draft, whichever line break
    the terminal sends - iTerm2, Apple's Terminal and tmux send CR."""
    stub.script(say("Checklist received."))
    term = hx.tui()
    term.type("todo: ")
    term.paste("- [ ] write tests\n- [ ] fix bug\n- [x] ship", newline=newline)
    term.wait_for(" - [x] ship")
    term.snapshot(f"paste with {newline!r} line breaks")
    lines = term.lines()
    assert any(line.strip() == "todo: - [ ] write tests" for line in lines)
    assert any(line.strip() == "- [ ] fix bug" for line in lines)
    assert "Pasted text" not in term.text()

    term.press("enter")
    term.wait_for("Checklist received.")
    assert (
        stub.requests[0]
        .last_user_text()
        .endswith("todo: - [ ] write tests\n- [ ] fix bug\n- [x] ship")
    )


def test_large_paste_collapses_and_is_sent_whole(hx: HX, stub: Stub) -> None:
    """A 40-line table pasted between two typed lines collapses to one token.
    What is sent is the table itself; the transcript draws it as a table; and the
    message recalled from history still expands to the table when sent again."""
    stub.script(say("svc-07, svc-14, svc-21, svc-28 and svc-35 are degraded."), say("Same again."))
    term = hx.tui()
    token = f"[Pasted text #1 +{TABLE_ROWS} lines]"
    term.type("which are degraded?")
    term.press("shift+enter")
    term.paste(TABLE + "\n")
    term.press("shift+enter")
    term.type("answer briefly")
    term.wait_for("answer briefly")
    term.snapshot("the table collapsed to a token")
    lines = term.lines()
    start = lines.index(" which are degraded?")
    # The table's own trailing newline is inside the token, out of sight.
    assert lines[start + 1 : start + 3] == [f" {token}", " answer briefly"]
    assert "svc-" not in term.text(), "none of the table is in the draft"

    term.press("enter")
    term.wait_for("are degraded.")
    term.settle()
    term.snapshot("the expanded table in the transcript")
    sent = stub.requests[0].last_user_text()
    assert sent.endswith(f"which are degraded?\n{TABLE}\n\nanswer briefly")
    assert "│ 7  │ svc-07  │ degraded │" in term.scrollback(), "drawn as a table"
    assert "|---|" not in term.scrollback()

    term.press("up")
    term.wait_for(token)
    term.press("enter")
    term.wait_for("Same again.")
    assert stub.requests[1].last_user_text().endswith(sent)


WIDE_TABLE = "\n".join(
    [
        "| Option | What it does | Trade-offs | Pick it |",
        "|---|---|---|---|",
        *(
            f"| store-{n} | Relational store number {n} with strong consistency and mature"
            f" tooling for migrations | Needs a running server; see"
            f" [notes](https://example.com/store-{n}) before scaling | **Yes** for app {n} |"
            for n in range(1, 11)
        ),
    ]
)
"""A 12-line table whose cells are far wider than any terminal column."""


def test_wide_pasted_table_wraps_rather_than_losing_text(hx: HX, stub: Stub) -> None:
    """A table too wide for the terminal keeps every word, in the echo of the
    user's own paste and in the model's reply: cells wrap inside their columns,
    a rule marks where each row starts, and a link or bold word stays in its
    own cell. Truncating the cells instead echoed back a table missing the
    text that was sent."""
    reply = "\n".join(WIDE_TABLE.split("\n")[:4])
    stub.script(say(f"Summary:\n\n{reply}"))
    term = hx.tui(columns=90, rows=40)
    term.type("compare these:")
    term.press("shift+enter")
    term.paste(WIDE_TABLE)
    term.wait_for("[Pasted text #1 +12 lines]")
    term.press("enter")
    term.wait_for("Summary:")
    term.settle()
    term.snapshot("a wide table wrapped in its columns")
    assert stub.requests[0].last_user_text().endswith(f"compare these:\n{WIDE_TABLE}")

    lines = term.scrollback().split("\n")
    assert all(len(line) <= term.columns for line in lines)
    tables = _tables(lines)
    assert len(tables) == 2, "the pasted table and the reply's, each drawn as a table"
    pasted, replied = tables
    # Every word of every cell is on screen, however the cells wrapped.
    words = " ".join(" ".join(line.split("│")) for line in pasted + replied).split()
    for n in range(1, 11):
        for word in (f"store-{n}", "mature", "migrations", "scaling", "notes", f"{n}"):
            assert word in words, f"{word!r} missing from the drawn tables"
    assert words.count("migrations") == 10 + 2, "ten pasted rows and two in the reply"
    # The header rule, then one between each pair of wrapped rows.
    assert sum(line.lstrip().startswith("├") for line in pasted) == 10
    assert sum(line.lstrip().startswith("├") for line in replied) == 2
    for table in tables:
        assert len({_bars(line) for line in table}) == 1, "columns misaligned"


def _tables(lines: list[str]) -> list[list[str]]:
    """Each run of consecutive grid lines on screen - one run per table."""
    tables: list[list[str]] = []
    run: list[str] = []
    for line in [*lines, ""]:
        if line.lstrip().startswith(("│", "├")):
            run.append(line)
        elif run:
            tables.append(run)
            run = []
    return tables


def _bars(line: str) -> tuple[int, ...]:
    """Where the column rules fall on a grid line."""
    return tuple(i for i, char in enumerate(line) if char in "│├┼┤")


def test_paste_token_behaves_as_one_character(hx: HX, stub: Stub) -> None:
    """The cursor steps over a paste token and cannot land inside it, backspace
    removes it whole, undo brings it back, and a token killed and yanked back
    still expands; two pastes are numbered apart and both are sent."""
    stub.script(say("Compared."))
    term = hx.tui()
    long_line = "x" * 1200
    first, second = f"[Pasted text #1 +{TABLE_ROWS} lines]", "[Pasted text #2 1200 chars]"
    term.paste(TABLE)
    term.type(" vs ")
    term.paste(long_line)
    term.wait_for(second)
    assert f" {first} vs {second}" in term.lines()

    # One press of left crosses the whole second token.
    term.press("left")
    term.type("!")
    term.wait_for(f" vs !{second}")
    term.press("backspace")
    term.wait_for(f" vs {second}")
    term.snapshot("cursor stepped over the token")

    # Backspace at the end takes the token whole, and undo puts it back.
    term.press("end", "backspace")
    term.wait_gone("Pasted text #2")
    assert f" {first} vs" in term.lines()
    term.press("ctrl+_")
    term.wait_for(second)

    # From the start, right lands after the first token, not inside it.
    term.press("ctrl+a", "right")
    term.type("<")
    term.wait_for(f" {first}< vs {second}")

    # Kill the line and yank it back: the tokens still stand for their pastes.
    term.press("ctrl+a", "ctrl+k")
    term.wait_gone("Pasted text")
    term.press("ctrl+y")
    term.wait_for(second)
    term.press("enter")
    term.wait_for("Compared.")
    assert stub.requests[0].last_user_text().endswith(f"{TABLE}< vs {long_line}")


# -- a real terminal: tmux -------------------------------------------------------

TMUX = shutil.which("tmux") or next(
    (p for p in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux") if os.path.exists(p)), None
)
requires_tmux = pytest.mark.skipif(TMUX is None, reason="tmux is not installed")


def _tmux_has_key_format() -> bool:
    """``extended-keys-format`` arrived in tmux 3.5."""
    if TMUX is None:
        return False
    probe = subprocess.run(
        [
            TMUX,
            "-L",
            f"hx-e2e-probe-{os.getpid()}",
            "-f",
            "/dev/null",
            "start-server",
            ";",
            "show-options",
            "-g",
            "extended-keys-format",
        ],
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0


class Tmux:
    """``hx`` in a detached tmux server of its own, driven with send-keys."""

    def __init__(self, hx: HX, name: str) -> None:
        assert TMUX is not None
        self.hx = hx
        self.socket = f"hx-e2e-{os.getpid()}-{name}"
        self.env = hx.env()

    def __call__(self, *args: str, check: bool = True) -> str:
        result = subprocess.run(
            [TMUX, "-L", self.socket, "-f", "/dev/null", *args],
            capture_output=True,
            text=True,
            env={**self.env, "PATH": f"{os.path.dirname(TMUX)}{os.pathsep}{self.env['PATH']}"},
            timeout=15,
        )
        if check:
            assert result.returncode == 0, result.stderr
        return result.stdout

    def start(self, options: dict[str, str]) -> None:
        # The server starts with the session, so global options are set in
        # the same command, before hx reads a key.
        # The server takes its environment from this command, so hx inside it
        # gets the test's private HOME, HX_HOME and stub.
        hx_bin = shutil.which("hx", path=self.env["PATH"])
        assert hx_bin is not None
        chained = ["new-session", "-d", "-x", "100", "-y", "30", "-c", str(self.hx.project), hx_bin]
        for key, value in options.items():
            chained += [";", "set", "-g", key, value]
        self(*chained)

    def screen(self) -> str:
        return self("capture-pane", "-p")

    def wait_for(self, test: Callable[[str], bool] | str, timeout: float = 15) -> str:
        predicate = test if callable(test) else (lambda screen: test in screen)
        deadline = time.monotonic() + timeout
        while True:
            screen = self.screen()
            if predicate(screen):
                return screen
            if time.monotonic() > deadline:
                self.snapshot("timed out")
                raise AssertionError(f"timed out waiting for {test!r}. Screen:\n{screen}")
            time.sleep(0.1)

    def snapshot(self, label: str) -> None:
        text = self.screen()
        self.hx.record.steps.append(
            Step(
                "screen",
                f"tmux: {label}",
                text=text,
                html=f'<pre class="term">{html.escape(text)}</pre>',
            )
        )

    def close(self) -> None:
        self("kill-server", check=False)


@requires_tmux
@pytest.mark.skipif(not _tmux_has_key_format(), reason="tmux older than 3.5")
@pytest.mark.parametrize("key_format", ["xterm", "csi-u"])
def test_real_tmux_shift_enter_and_paste(hx: HX, stub: Stub, key_format: str) -> None:
    """Inside a real tmux with extended keys on, shift+enter adds a line (HX asks
    tmux for modifyOtherKeys) and a pasted table - sent by tmux with CR line
    breaks - collapses, then goes out whole with its newlines."""
    stub.script(say("Read the table."))
    tmux = Tmux(hx, key_format)
    try:
        tmux.start({"extended-keys": "on", "extended-keys-format": key_format})
        tmux.wait_for("Ask HX")
        time.sleep(0.3)

        tmux("send-keys", "-l", "status report:")
        tmux("send-keys", "S-Enter")
        tmux("send-keys", "-l", "see below ")
        tmux.wait_for(" see below")
        screen = tmux.screen()
        assert " status report:" in screen.splitlines()
        _no_request(stub)

        table = hx.root / "table.md"
        table.write_text(TABLE + "\n")
        tmux("load-buffer", str(table))
        tmux("paste-buffer", "-p")
        tmux.wait_for(f"[Pasted text #1 +{TABLE_ROWS} lines]")
        tmux.snapshot("draft: two lines and a collapsed paste")

        tmux("send-keys", "Enter")
        tmux.wait_for("Read the table.")
        tmux.snapshot("the table as sent")
        sent = stub.requests[0].last_user_text()
        assert sent.endswith(f"status report:\nsee below {TABLE}")
    finally:
        tmux.close()
