"""Messages typed while a turn runs: the queue, /queue, and steering from it."""

from __future__ import annotations

import json
import threading

from tests.e2e.conftest import HX
from tests.e2e.stub import Reply, Stub, say


def test_queue_lists_steers_and_drains_in_order(hx: HX, stub: Stub) -> None:
    """Three messages queued behind a running turn: /queue lists them oldest first,
    /queue steer 2 sends the middle one into the running turn, the head runs as its
    own turn once that ends, and alt+enter on an empty prompt steers the last one."""
    first, rename = threading.Event(), threading.Event()
    stub.script(
        Reply(text="Refactoring the parser, step one of", hold=first, hold_after=20),
        say("Tests added for the parser."),
        Reply(text="Renaming Parser to Reader across", hold=rename, hold_after=16),
        say("Docs updated for Reader."),
    )
    term = hx.tui()
    term.submit("refactor the parser")
    term.wait_for("Refactoring the pars")

    term.submit("also rename Parser to Reader")
    term.submit("then add tests")
    term.submit("and update the docs")
    term.wait_for("⧗3 queued")
    term.snapshot("three messages queued behind the running turn")
    assert len(stub.requests) == 1, "nothing queued may reach the model yet"

    term.submit("/queue")
    screen = term.wait_for("Queued, oldest first:")
    assert "1. also rename Parser to Reader" in screen
    assert "2. then add tests" in screen
    assert "3. and update the docs" in screen
    term.snapshot("/queue lists them oldest first")

    term.submit("/queue steer 2")
    term.wait_for("Tests added for the parser.")
    steered = stub.requests[1]
    assert steered.last_user_text().endswith("then add tests")
    assert "also rename Parser to Reader" not in json.dumps(steered.messages)

    # The steered turn ended on its own, so the head of the queue runs next.
    term.wait_for("Renaming Parser")
    drained = stub.requests[2]
    assert drained.last_user_text().endswith("also rename Parser to Reader")
    term.wait_for("⧗1 queued")
    term.snapshot("head of the queue running, one left")

    term.press("alt+enter")
    term.wait_for("Docs updated for Reader.")
    term.wait_gone("queued")
    term.settle()
    term.snapshot("queue empty after steering the last message")
    last = stub.requests[3]
    assert last.last_user_text().endswith("and update the docs")

    term.submit("/queue")
    term.wait_for("Nothing queued.")
    first.set()
    rename.set()
    assert len(stub.requests) == 4, "every message reached the model exactly once"


def test_queue_clear_drops_what_was_waiting(hx: HX, stub: Stub) -> None:
    """/queue clear drops the waiting messages; the turn then ends without sending them."""
    hold = threading.Event()
    stub.script(Reply(text="Looking through the logs for", hold=hold, hold_after=12))
    term = hx.tui()
    term.submit("find the crash")
    term.wait_for("Looking thro")
    term.submit("check the auth module too")
    term.submit("and the cache layer")
    term.wait_for("⧗2 queued")

    term.submit("/queue clear")
    term.wait_for("Dropped 2 queued message(s).")
    term.wait_gone("⧗2 queued")
    term.snapshot("queue cleared while the turn runs")

    hold.set()
    term.wait_for("Looking through the logs for")
    term.settle(quiet=0.6)
    assert len(stub.requests) == 1, "cleared messages must never be sent"
    term.submit("/queue clear")
    term.wait_for("Nothing queued.")


def test_interrupt_keeps_the_queue_until_the_next_turn_ends(hx: HX, stub: Stub) -> None:
    """esc stops the turn without draining the queue into a new one, says what is
    still held, and the held message runs after the next turn the user starts."""
    hold = threading.Event()
    stub.script(
        Reply(text="Migrating the schema, table one", hold=hold, hold_after=14),
        say("Rolled back cleanly."),
        say("Indexes rebuilt."),
    )
    term = hx.tui()
    term.submit("migrate the schema")
    term.wait_for("Migrating the")
    term.submit("then rebuild the indexes")
    term.wait_for("⧗1 queued")

    term.press("esc")
    term.wait_for("1 message(s) still queued · /queue to see them")
    term.settle(quiet=0.6)
    term.snapshot("interrupted with a message still queued")
    assert len(stub.requests) == 1, "an interrupt must not start the queued turn"
    assert "⧗1 queued" in term.text()
    hold.set()

    term.submit("roll it back")
    term.wait_for("Rolled back cleanly.")
    term.wait_for("Indexes rebuilt.")
    term.settle()
    assert stub.requests[1].last_user_text().endswith("roll it back")
    assert stub.requests[2].last_user_text().endswith("then rebuild the indexes")
    assert "⧗" not in term.text(), "the status bar no longer counts a queue"


def test_enter_while_busy_steer_swaps_the_keys(hx: HX, stub: Stub) -> None:
    """With tui.enterWhileBusy set to "steer", enter steers the running turn and
    alt+enter queues behind it - what the prompt's placeholder says."""
    settings = hx.project / ".hx" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"tui": {"enterWhileBusy": "steer"}}))
    java, python = threading.Event(), threading.Event()
    stub.script(
        Reply(text="Writing it in Java, starting with", hold=java, hold_after=17),
        Reply(text="Switching to Python, writing main", hold=python, hold_after=18),
        say("Docstring added."),
    )
    term = hx.tui()
    term.submit("write a script")
    term.wait_for("Writing it in Ja")
    term.wait_for("Enter steers")
    term.snapshot("placeholder names the swapped keys")

    term.submit("use python instead")
    term.wait_for("Switching to Pyth")
    assert stub.requests[1].last_user_text().endswith("use python instead")

    term.type("and add a docstring")
    term.press("alt+enter")
    term.wait_for("⧗1 queued")
    term.snapshot("alt+enter queued behind the running turn")
    assert len(stub.requests) == 2, "alt+enter must queue, not steer"

    python.set()
    term.wait_for("Docstring added.")
    assert stub.requests[2].last_user_text().endswith("and add a docstring")
    java.set()
