"""The inline approval prompt.

An approval is a decision the user has to be able to make from what is on
screen, and to check afterwards. These are the two halves of that.
"""

from __future__ import annotations

import asyncio
import io

import pytest
from rich.console import Console

from hx.permissions.engine import GrantScope, PermissionAnswer, PermissionRequest
from hx.tui.widgets.permission import MAX_DETAIL_LINES, PermissionPrompt


def _plain(prompt: PermissionPrompt, width: int = 90) -> str:
    console = Console(width=width, record=True, force_terminal=False, file=io.StringIO())
    console.print(prompt.render())
    return console.export_text()


def _request(**kwargs: object) -> PermissionRequest:
    base: dict[str, object] = {
        "tool_name": "Bash",
        "specifier": "rm -rf build",
        "params": {},
        "mutating": True,
        "description": "Bash(rm -rf build)",
        "detail": "rm -rf build",
    }
    return PermissionRequest(**{**base, **kwargs})  # type: ignore[arg-type]


def test_a_pending_prompt_shows_the_command_and_the_choices() -> None:
    rendered = _plain(PermissionPrompt(_request()))
    assert "Permission needed" in rendered
    assert "rm -rf build" in rendered
    for key in ("y allow once", "s session", "a always", "n deny"):
        assert key in rendered


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        (GrantScope.ONCE, "allowed once"),
        (GrantScope.SESSION, "allowed for this session"),
        (GrantScope.ALWAYS, "always allowed"),
    ],
)
def test_an_answered_prompt_collapses_into_a_record(scope: GrantScope, expected: str) -> None:
    """The block is the receipt. `always` wrote a rule to a file, and a session
    that cannot show which grants it made cannot be audited afterwards."""
    prompt = PermissionPrompt(_request())
    prompt.resolve(PermissionAnswer(True, scope))

    rendered = _plain(prompt)
    assert expected in rendered
    assert "rm -rf build" in rendered
    # The question is over; leaving the keys on screen would advertise a lie.
    assert "allow once" not in rendered


def test_a_denial_records_itself_too() -> None:
    prompt = PermissionPrompt(_request())
    prompt.resolve(PermissionAnswer(allowed=False))
    assert "denied" in _plain(prompt)


def test_the_first_answer_is_the_one_that_counts() -> None:
    """The decision was already acted on by the time a second key arrives."""
    prompt = PermissionPrompt(_request())
    prompt.resolve(PermissionAnswer(True, GrantScope.ONCE))
    prompt.resolve(PermissionAnswer(allowed=False))
    assert prompt.answer is not None
    assert prompt.answer.allowed


async def test_the_answer_reaches_whoever_is_waiting() -> None:
    future: asyncio.Future[PermissionAnswer] = asyncio.get_running_loop().create_future()
    prompt = PermissionPrompt(_request(), future)
    prompt.resolve(PermissionAnswer(True, GrantScope.SESSION))
    assert (await future).scope is GrantScope.SESSION


def test_an_interrupted_prompt_says_nobody_answered_it() -> None:
    """A block that goes on offering keys which reach nothing is worse than one
    that admits the turn ended underneath it."""
    prompt = PermissionPrompt(_request())
    prompt.abandon()

    rendered = _plain(prompt)
    assert prompt.answered
    assert prompt.answer is None
    assert "interrupted" in rendered
    assert "allow once" not in rendered


def test_a_long_diff_is_clipped_until_asked_for() -> None:
    """A 600-line diff is worth reading before approving and not worth leaving in
    the scrollback forever."""
    body = "\n".join(f"+line {n}" for n in range(MAX_DETAIL_LINES + 10))
    prompt = PermissionPrompt(_request(detail=f"--- a.py\n+++ a.py\n@@ -0 +1 @@\n{body}"))

    clipped = _plain(prompt)
    assert "more lines" in clipped
    assert f"line {MAX_DETAIL_LINES + 5}" not in clipped

    prompt.action_toggle_detail()
    assert f"line {MAX_DETAIL_LINES + 5}" in _plain(prompt)


def test_an_answered_prompt_cannot_be_expanded() -> None:
    """The detail belongs to the decision; re-opening it after the fact would
    show a diff that has already been applied as though it were pending."""
    prompt = PermissionPrompt(_request(detail="--- a.py\n+++ a.py\n@@ -1 +1 @@\n+x"))
    prompt.resolve(PermissionAnswer(True, GrantScope.ONCE))
    prompt.action_toggle_detail()
    assert not prompt.expanded


def test_a_prompt_with_no_detail_falls_back_to_the_specifier() -> None:
    rendered = _plain(PermissionPrompt(_request(detail="")))
    assert "rm -rf build" in rendered


def test_a_subagent_prompt_names_who_is_asking() -> None:
    """An approval whose origin is unclear is not an informed approval."""
    prompt = PermissionPrompt(_request(origin="explore subagent"))
    assert "explore subagent" in _plain(prompt)
