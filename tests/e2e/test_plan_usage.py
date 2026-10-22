"""How much of a subscription's usage windows is spent, on the status bar.

A subscription turn has no per-token price, so the cost slot used to say only
``sub``. The question a subscriber asks instead is how much of the plan's
windows - five hours, seven days - is gone, and when it comes back. ChatGPT
says it on every response, in ``x-codex-*`` headers; Claude Code says it in
``rate_limit_event`` frames.

What could go wrong, and the test that holds each one:

- A session with no answer yet shows ``0%``, a claim nobody made.
  -> test_chatgpt_usage_climbs_and_changes_colour
- The field stays one colour as the window fills, so it never warns.
  -> test_chatgpt_usage_climbs_and_changes_colour
- A reply without the headers, or with a value that is not a number, wipes
  what was known, or crashes the turn.
  -> test_a_reply_that_says_nothing_keeps_what_was_known
- The limit reached is retried three times, says nothing about when it
  resets, and leaves the bar at the last figure under 100%.
  -> test_the_chatgpt_limit_reached_is_shown_full_and_not_retried
- A window that resets while the user is idle keeps its old figure on screen.
  -> test_a_window_that_resets_while_idle_drops_to_zero
- Another route's model shows ChatGPT's figures as its own; coming back loses
  them until the next turn; /clear forgets an account's state with the session.
  -> test_the_field_follows_the_route
- A renewal time that is wrong, or in a form that differs from one window to
  the next.
  -> test_chatgpt_usage_climbs_and_changes_colour
- A narrow terminal cuts the line mid-number, or pushes the model off.
  -> test_a_narrow_terminal_shortens_the_line_step_by_step
- Claude's figures come in ``unifiedWindows`` and are missed; or an older
  Claude Code, which gives one window per frame and no figure below the
  warning, has a frame replace the others or a guessed figure shown.
  -> test_claude_windows_come_from_rate_limit_frames
"""

from __future__ import annotations

import time
from typing import Any

from tests.e2e import stub as openrouter
from tests.e2e.conftest import HX, FakeClaude
from tests.e2e.stub import CodexReply, codex_limits

CODEX = "openai-codex/gpt-5.6-terra"

MUTED = "808080"
WARNING = "f0c674"
ERROR = "cc6666"
"""The dark theme's ``muted``, ``warning`` and ``error`` roles, as pyte reports them."""


def sign_in_to_chatgpt(hx: HX) -> dict[str, str]:
    """A stored ChatGPT login, and the environment that runs the Codex route."""
    hx.sign_in_codex()
    return {"HX_MODEL": CODEX}


def bottom(term: Any) -> str:
    """The stats line, with the model at its end."""
    return term.lines()[-1]


def usage(term: Any) -> str:
    """The line above the stats: the usage windows, when there is one."""
    return term.lines()[-2].strip()


def usage_is(text: str) -> Any:
    return lambda screen: screen.splitlines()[-2].strip() == text


def colour_of(term: Any, text: str) -> str:
    """The foreground of the first cell of ``text`` on the usage line."""
    row = term.rows - 2
    column = term.lines()[-2].index(text)
    with term._lock:
        return str(term.screen.buffer[row][column].fg)


def renews(epoch: float, *, short: bool = False) -> str:
    """When a window renews, as the line spells it: the time within a day,
    ``Mon Oct 26 04:00 am`` beyond it - or ``Mon Oct 26`` where room is short."""
    when = time.localtime(epoch)
    clock = time.strftime("%I:%M %p", when).lower()
    if epoch - time.time() < 86_400:
        return clock
    day = f"{time.strftime('%a %b', when)} {when.tm_mday}"
    return day if short else f"{day} {clock}"


def test_chatgpt_usage_climbs_and_changes_colour(hx: HX) -> None:
    """Three turns on a ChatGPT plan: the usage line appears with the first answer, and
    its five-hour figure turns warning, then error, as the window fills. Each window says
    when it renews. /cost says the same in words."""
    env = sign_in_to_chatgpt(hx)
    five_hours = int(time.time()) + 4 * 3600 + 1800
    week = int(time.time()) + 4 * 86_400
    windows = {"primary_resets_at": five_hours, "secondary_resets_at": week}
    hx.stub.codex_script(
        CodexReply("First.", limits=codex_limits(primary=30, secondary=12, **windows)),
        CodexReply("Second.", limits=codex_limits(primary=80, secondary=40, **windows)),
        CodexReply("Third.", limits=codex_limits(primary=95, secondary=60, **windows)),
    )
    term = hx.tui(env=env)
    term.wait_for(lambda s: "gpt-5.6-terra (sub)" in s.splitlines()[-1])
    assert " sub " in bottom(term) + " "
    assert "%" not in "".join(term.lines()[-3:])
    term.snapshot("no turn yet: nothing is claimed")

    term.submit("one")
    line = f"5h 30% (renews {renews(five_hours)}) | 7d 12% (renews {renews(week)})"
    term.wait_for(usage_is(line))
    assert colour_of(term, "5h 30%") == MUTED
    assert colour_of(term, "7d 12%") == MUTED
    # The usage line says it, so the stats line no longer has to.
    assert " sub " not in bottom(term) + " "
    term.snapshot("a third of the five hours spent")

    term.submit("two")
    term.wait_for(usage_is(line.replace("30%", "80%").replace("12%", "40%")))
    assert colour_of(term, "5h 80%") == WARNING
    assert colour_of(term, "7d 40%") == MUTED
    term.snapshot("past three quarters: a warning")

    term.submit("three")
    term.wait_for(usage_is(line.replace("30%", "95%").replace("12%", "60%")))
    assert colour_of(term, "5h 95%") == ERROR
    assert colour_of(term, "7d 60%") == MUTED
    # The model and its route are still the bottom line's end.
    assert bottom(term).rstrip().endswith("gpt-5.6-terra (sub) · medium")
    term.snapshot("past nine tenths: an error")

    term.submit("/cost")
    screen = term.wait_for("ChatGPT plan")
    flat = " ".join(screen.split())
    assert f"5-hour window: 95% used, renews {renews(five_hours)}" in flat
    assert f"7-day window: 60% used, renews {renews(week)}" in flat
    term.snapshot("/cost names the windows")
    assert len(hx.stub.codex_requests) == 3


def test_a_reply_that_says_nothing_keeps_what_was_known(hx: HX) -> None:
    """A response without the headers changes nothing; one whose five-hour figure is not
    a number keeps the last five-hour figure and still takes the seven-day one."""
    env = sign_in_to_chatgpt(hx)
    malformed = codex_limits(primary=0, secondary=15)
    malformed["x-codex-primary-used-percent"] = "n/a"
    hx.stub.codex_script(
        CodexReply("Known.", limits=codex_limits(primary=30, secondary=12)),
        CodexReply("Silent.", limits={}),
        CodexReply("Garbled.", limits=malformed),
    )
    term = hx.tui(env=env)
    term.submit("one")
    term.wait_for(lambda s: "5h 30% (renews" in s and "7d 12% (renews" in s)

    term.submit("two")
    term.wait_for("Silent.")
    term.settle()
    assert usage(term).startswith("5h 30% (renews") and "| 7d 12% (renews" in usage(term)
    term.snapshot("a reply without headers keeps the line")

    term.submit("three")
    term.wait_for("Garbled.")
    term.wait_for(lambda s: "| 7d 15% (renews" in s.splitlines()[-2])
    assert usage(term).startswith("5h 30% (renews")
    assert "error" not in term.text().lower()
    term.snapshot("a garbled figure is ignored, the good one taken")


def test_the_chatgpt_limit_reached_is_shown_full_and_not_retried(hx: HX) -> None:
    """The backend refuses with 429 ``usage_limit_reached`` and the headers at 100%. One
    request, not four; the notice says when it resets; the line shows the window full."""
    env = sign_in_to_chatgpt(hx)
    resets = int(time.time()) + 2 * 3600
    hx.stub.codex_script(
        CodexReply(
            status=429,
            error={
                "type": "usage_limit_reached",
                "message": "The usage limit has been reached",
                "plan_type": "plus",
                "resets_at": resets,
            },
            limits=codex_limits(primary=100, secondary=48, primary_resets_at=resets),
        ),
    )
    term = hx.tui(env=env)
    term.submit("hello")
    screen = term.wait_for("usage limit is reached")
    flat = " ".join(screen.split())
    assert (
        f"Your ChatGPT plan's 5-hour usage limit is reached. It resets at {renews(resets)}." in flat
    )
    term.wait_for(
        lambda s: (
            s.splitlines()[-2].strip().startswith(f"5h 100% (renews {renews(resets)}) | 7d 48%")
        )
    )
    assert colour_of(term, "5h 100%") == ERROR
    term.snapshot("the limit reached")
    assert len(hx.stub.codex_requests) == 1


def test_a_window_that_resets_while_idle_drops_to_zero(hx: HX) -> None:
    """The five-hour window resets a few seconds after the turn. With nothing typed, the
    line moves to 0% on its own; the seven-day window, not reset, keeps its figure."""
    env = sign_in_to_chatgpt(hx)
    week = int(time.time()) + 6 * 86_400
    hx.stub.codex_script(
        CodexReply(
            "Done.",
            limits=codex_limits(
                primary=92,
                secondary=33,
                primary_resets_at=int(time.time()) + 4,
                secondary_resets_at=week,
            ),
        ),
    )
    term = hx.tui(env=env)
    term.submit("one")
    term.wait_for(lambda s: s.splitlines()[-2].strip().startswith("5h 92% (renews"))
    assert colour_of(term, "5h 92%") == ERROR
    term.snapshot("nearly spent, a few seconds before the reset")

    term.wait_for(usage_is(f"5h 0% | 7d 33% (renews {renews(week)})"), timeout=10)
    assert colour_of(term, "5h 0%") == MUTED
    term.snapshot("the window reset with the user idle")


def test_the_field_follows_the_route(hx: HX) -> None:
    """ChatGPT's figures are ChatGPT's: an OpenRouter model shows its cost and no usage
    line, and switching back shows the line again before any turn. /clear starts a new
    session on the same account, so the figures stay."""
    env = sign_in_to_chatgpt(hx)
    hx.stub.codex_script(CodexReply("Codex here.", limits=codex_limits(primary=30, secondary=12)))
    term = hx.tui(env=env)
    term.submit("one")
    term.wait_for(lambda s: s.splitlines()[-2].strip().startswith("5h 30% (renews"))
    shown = usage(term)

    term.submit(f"/model {openrouter.SONNET}")
    term.wait_for(f"Model set to {openrouter.SONNET}")
    term.wait_for(lambda s: "claude-sonnet-4.5" in s.splitlines()[-1])
    assert "%" not in "".join(term.lines()[-3:])
    assert "$" in bottom(term)
    term.snapshot("a per-token model shows its cost")

    term.submit(f"/model {CODEX}")
    term.wait_for(f"Model set to {CODEX}")
    term.wait_for(usage_is(shown))
    term.snapshot("back on ChatGPT, the figures return")

    term.submit("/clear")
    term.settle()
    assert usage(term) == shown
    term.snapshot("/clear keeps the account's figures")


def test_a_narrow_terminal_shortens_the_line_step_by_step(hx: HX) -> None:
    """Too narrow for the whole line, the renewals lose their clock time, then go; on a
    terminal too narrow even for that, only the window nearer its limit stays. Nothing
    is cut mid-word, and the model keeps its place."""
    env = sign_in_to_chatgpt(hx)
    five_hours = int(time.time()) + 3 * 3600
    week = int(time.time()) + 5 * 86_400
    hx.stub.codex_script(
        CodexReply(
            "Ok.",
            limits=codex_limits(
                primary=20, secondary=91, primary_resets_at=five_hours, secondary_resets_at=week
            ),
        )
    )
    term = hx.tui(env=env, columns=56)
    term.submit("one")
    term.wait_for(usage_is(f"5h 20% ({renews(five_hours)}) | 7d 91% ({renews(week, short=True)})"))
    assert colour_of(term, "7d 91%") == ERROR
    assert bottom(term).rstrip().endswith("· medium")
    term.snapshot("56 columns: the renewal days without their times")

    term.resize(36, 30)
    term.wait_for(usage_is("5h 20% | 7d 91%"))
    term.snapshot("36 columns: the figures alone")

    term.resize(14, 30)
    term.wait_for(usage_is("7d 91%"))
    term.snapshot("14 columns: the fullest window only")


def test_claude_windows_come_from_rate_limit_frames(hx: HX, claude: FakeClaude) -> None:
    """Current Claude Code puts every window's figure in ``unifiedWindows`` on each
    frame, as seen live on 2026-10-22. An older one gives only the frame's own window,
    with no figure below its warning, so that window shows only when it renews. A
    rejected window is full. Each report updates its own windows and leaves the others."""
    claude.login()
    claude.use()
    five_hours = int(time.time()) + 3 * 3600
    week = int(time.time()) + 4 * 86_400
    claude.script(
        {
            # An older Claude Code: the frame's own window, and no figure.
            "text": "Plenty left.",
            "rate_limits": [
                {"status": "allowed", "rateLimitType": "five_hour", "resetsAt": five_hours},
            ],
        },
        {
            # The current one, shaped as the live frame was.
            "text": "Getting there.",
            "rate_limits": [
                {
                    "status": "allowed",
                    "resetsAt": five_hours,
                    "rateLimitType": "five_hour",
                    "overageStatus": "rejected",
                    "overageDisabledReason": "org_level_disabled",
                    "isUsingOverage": False,
                    "unifiedWindows": {
                        "five_hour": {"utilization": 0.29, "resetsAt": five_hours},
                        "seven_day": {"utilization": 0.8, "resetsAt": week},
                    },
                },
            ],
        },
        {
            "rate_limit": {"rateLimitType": "five_hour", "resetsAt": five_hours},
            "error": "Claude AI usage limit reached",
        },
    )
    term = hx.tui()
    term.wait_for(lambda s: "(sub)" in s.splitlines()[-1])
    assert "%" not in "".join(term.lines()[-3:]) and "renews" not in term.text()

    term.submit("one")
    term.wait_for(usage_is(f"5h (renews {renews(five_hours)})"))
    assert colour_of(term, "5h (renews") == MUTED
    term.snapshot("an older Claude Code: when the window renews")

    term.submit("two")
    term.wait_for(
        usage_is(f"5h 29% (renews {renews(five_hours)}) | 7d 80% (renews {renews(week)})")
    )
    assert colour_of(term, "5h 29%") == MUTED
    assert colour_of(term, "7d 80%") == WARNING
    term.snapshot("current Claude Code: every window's figure")

    term.submit("three")
    term.wait_for("five hour usage limit is reached")
    term.wait_for(
        usage_is(f"5h 100% (renews {renews(five_hours)}) | 7d 80% (renews {renews(week)})")
    )
    assert colour_of(term, "5h 100%") == ERROR
    term.snapshot("the five hours rejected")
