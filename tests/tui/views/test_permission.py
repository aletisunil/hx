"""The approval prompt, and every defect the rewrite was meant to fix."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hx.permissions.engine import GrantScope, PermissionAnswer, PermissionRequest
from hx.term.width import strip_ansi
from hx.tui import paint
from hx.tui.limits import PREVIEW_LINES
from hx.tui.views.permission import CHOICES, PermissionPrompt
from tests.term.conftest import assert_lines_fit, plain

CWD = Path("/project")


@pytest.fixture(autouse=True)
def _pinned_colors() -> None:
    paint.set_color_mode("truecolor")


def bash(detail: str = "rm -rf build/", **extra: object) -> PermissionRequest:
    return PermissionRequest(
        tool_name="Bash",
        specifier=detail,
        params={"command": detail},
        mutating=True,
        description="run a shell command",
        detail=detail,
        detail_kind="command",
        **extra,  # type: ignore[arg-type]
    )


def prompt(request: PermissionRequest | None = None) -> PermissionPrompt:
    return PermissionPrompt(request or bash(), cwd=CWD)


# -- the shape --------------------------------------------------------------


def test_it_is_framed_by_rules_not_boxed() -> None:
    """A rectangle around every section turns a dialog into a stack of them."""
    lines = plain(prompt().render(70))
    assert set(lines[0].strip()) == {"─"}
    assert set(lines[-1].strip()) == {"─"}
    assert not any(ch in "".join(lines) for ch in "┌┐└┘├┤│")


def test_it_has_no_background_at_all() -> None:
    """The old prompt wore the tint of a running tool call, so the most
    consequential block on screen had the weight of the most routine one."""
    assert not any("48;2;" in line for line in prompt().render(70))


def test_every_option_is_on_its_own_row() -> None:
    rows = _option_rows(prompt())
    assert len(rows) == len(CHOICES)
    for (_key, _scope, label, _note), row in zip(CHOICES, rows, strict=True):
        assert label in row


def test_the_selected_row_carries_the_only_cursor() -> None:
    lines = [line for line in plain(prompt().render(70)) if "allow" in line or "deny" in line]
    assert sum(1 for line in lines if "→" in line) == 1
    assert lines[0].strip().startswith("→")


# -- the audited defects ----------------------------------------------------


def test_answering_does_not_shift_the_text_sideways() -> None:
    """Pending and answered used to be padded differently, so answering moved
    the block one column left."""
    block = prompt()
    # Rules span the full width and carry no padding, so only the text lines
    # are comparable.
    pending = [line for line in plain(block.render(70)) if line.strip() and "─" not in line]
    # Option rows sit further in - that is their gutter - so the block's left
    # edge is the smallest indent among them.
    left_edge = min(len(line) - len(line.lstrip()) for line in pending)
    assert left_edge == 1

    block.resolve(PermissionAnswer(True, GrantScope.ONCE))
    answered = plain(block.render(70))
    assert len(answered[0]) - len(answered[0].lstrip()) == left_edge


def test_always_states_its_consequence_in_words() -> None:
    """It writes a rule to a file. Saying so beats colouring it, because an
    option styled to stand out is one the eye learns to go to."""
    line = next(line for line in plain(prompt().render(90)) if "always allow" in line)
    assert ".hx/settings.local.json" in line


def test_no_option_is_emphasised_for_being_dangerous() -> None:
    """Weight follows selection only.

    The old prompt painted "allow once" green and "deny" red, which marks the
    permissive answer as the safe-looking one - exactly backwards for a prompt
    whose job is to make the user read. Those two are the extremes, so if they
    render identically nothing is being styled for its consequences.
    """
    block = prompt()

    def styling_of(key: str) -> frozenset[str]:
        """That option's row, matched on key and label together.

        Matching on the key alone hits the padding at the end of a longer row -
        "allow for this session" ends in "n  " too.
        """
        label = next(lbl for k, _scope, lbl, _note in CHOICES if k == key)
        needle = f"{key}  {label}"
        return _roles(next(line for line in block.render(90) if needle in strip_ansi(line)))

    block.selected = 0  # "allow once" selected, "deny" not
    block.invalidate()
    allow_selected, deny_unselected = styling_of("y"), styling_of("n")

    block.selected = 3  # the other way round
    block.invalidate()
    allow_unselected, deny_selected = styling_of("y"), styling_of("n")

    assert allow_selected == deny_selected, "the two extremes differ when selected"
    assert allow_unselected == deny_unselected, "the two extremes differ when not selected"
    assert allow_selected != allow_unselected, "selection is not marked at all"


def _roles(line: str) -> frozenset[str]:
    import re

    return frozenset(re.findall(r"\x1b\[38;2;\d+;\d+;\d+m", line))


def _styled_option_rows(block: PermissionPrompt) -> list[str]:
    """The four option rows, in order, still carrying their styling.

    Matched on "<key>  <label>", which no other line contains - the hint line
    mentions "deny" too, but never as "n  deny".
    """
    rendered = block.render(90)
    rows = []
    for key, _scope, label, _note in CHOICES:
        needle = f"{key}  {label}"
        rows.extend(line for line in rendered if needle in strip_ansi(line))
    return rows


def _option_rows(block: PermissionPrompt) -> list[str]:
    return [strip_ansi(row).rstrip() for row in _styled_option_rows(block)]


def test_escape_and_expand_are_advertised() -> None:
    """Both were bound by the old prompt and named nowhere."""
    hints = plain(prompt().render(90))[-3]
    assert "esc deny" in hints
    assert "expand" in hints


def test_a_command_keeps_the_prompt_character_it_will_run_with() -> None:
    """The transcript shows "$ cmd"; the approval used to show a bare command,
    so the user agreed to a differently-shaped thing than the one that ran."""
    assert any("$ rm -rf build/" in line for line in plain(prompt().render(70)))


def test_a_diff_is_drawn_as_a_diff() -> None:
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n same\n-before\n+after\n"
    request = PermissionRequest(
        tool_name="Edit",
        specifier="x.py",
        params={"file_path": "x.py"},
        mutating=True,
        description="edit",
        detail=diff,
        detail_kind="diff",
    )
    shown = " ".join(plain(PermissionPrompt(request, cwd=CWD).render(70)))
    assert "before" in shown and "after" in shown


def test_front_matter_is_not_mistaken_for_a_diff() -> None:
    """Sniffing a leading "---" fed YAML to a painter that strips exactly those
    lines, so the user was asked to approve a blank space."""
    content = "---\ntitle: a note\n---\n\nthe body"
    request = PermissionRequest(
        tool_name="Write",
        specifier="note.md",
        params={"file_path": "note.md", "content": content},
        mutating=True,
        description="write",
        detail=content,
        detail_kind="text",
    )
    shown = " ".join(plain(PermissionPrompt(request, cwd=CWD).render(70)))
    assert "title: a note" in shown
    assert "the body" in shown


def test_a_long_detail_is_clipped_once_against_one_number() -> None:
    """It used to be clipped twice, against two unrelated constants, and the
    count of what was hidden was measured on the wrong set of lines."""
    detail = "\n".join(f"line {index}" for index in range(40))
    request = bash()
    request.detail = detail
    request.detail_kind = "text"
    lines = plain(PermissionPrompt(request, cwd=CWD).render(70))
    note = next(line for line in lines if "more line" in line)
    assert f"{40 - PREVIEW_LINES} more lines" in note


def test_expanding_shows_the_rest() -> None:
    request = bash()
    request.detail = "\n".join(f"line {index}" for index in range(40))
    request.detail_kind = "text"
    block = PermissionPrompt(request, cwd=CWD)
    collapsed = len(block.render(70))
    block.handle_input("ctrl+o", "")
    assert len(block.render(70)) > collapsed


# -- answering --------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "allowed", "scope"),
    [
        ("y", True, GrantScope.ONCE),
        ("s", True, GrantScope.SESSION),
        ("a", True, GrantScope.ALWAYS),
        ("n", False, GrantScope.ONCE),
    ],
)
def test_a_single_key_answers_immediately(key: str, allowed: bool, scope: GrantScope) -> None:
    """Nobody should have to navigate to say yes.

    Fed the way the decoder actually delivers a printable key - as "text" with
    the character in data - because asserting on the key name alone passed
    while the real thing did nothing.
    """
    block = prompt()
    assert block.handle_input("text", key) is True
    assert block.answer is not None
    assert block.answer.allowed is allowed
    if allowed:
        assert block.answer.scope is scope


def test_arrows_and_enter_work_for_anyone_who_wants_to_read_first() -> None:
    block = prompt()
    block.handle_input("down", "")
    block.handle_input("enter", "")
    assert block.answer is not None
    assert block.answer.scope is GrantScope.SESSION


def test_selection_wraps() -> None:
    block = prompt()
    block.handle_input("up", "")
    assert block.selected == len(CHOICES) - 1


def test_escape_denies() -> None:
    block = prompt()
    block.handle_input("escape", "")
    assert block.answer is not None and block.answer.allowed is False


def test_a_second_key_cannot_overwrite_the_decision_already_acted_on() -> None:
    block = prompt()
    block.handle_input("text", "y")
    assert block.handle_input("text", "n") is False
    assert block.answer is not None and block.answer.allowed is True


def test_answering_resolves_whoever_is_waiting() -> None:
    async def scenario() -> PermissionAnswer:
        future: asyncio.Future[PermissionAnswer] = asyncio.get_running_loop().create_future()
        block = PermissionPrompt(bash(), future, cwd=CWD)
        block.handle_input("text", "s")
        return await future

    answer = asyncio.run(scenario())
    assert answer.allowed and answer.scope is GrantScope.SESSION


def test_an_abandoned_prompt_refuses_on_behalf_of_nobody() -> None:
    """Whoever is blocked has to be released, and the only answer safe to give
    for a user who never answered is no."""

    async def scenario() -> PermissionAnswer:
        future: asyncio.Future[PermissionAnswer] = asyncio.get_running_loop().create_future()
        block = PermissionPrompt(bash(), future, cwd=CWD)
        block.abandon()
        return await future

    answer = asyncio.run(scenario())
    assert answer.allowed is False


def test_the_record_says_how_widely_it_was_granted() -> None:
    """A session that quietly accumulates always-rules should show its work."""
    block = prompt()
    block.handle_input("text", "a")
    assert "always allowed" in plain(block.render(70))[0]


def test_an_abandoned_prompt_claims_no_decision() -> None:
    block = prompt()
    block.abandon()
    line = plain(block.render(70))[0]
    assert "not answered" in line
    assert "allowed" not in line and "denied" not in line


def test_the_origin_is_named_when_it_is_not_the_main_conversation() -> None:
    block = PermissionPrompt(bash(origin="code-reviewer"), cwd=CWD)
    assert "requested by code-reviewer" in " ".join(plain(block.render(90)))


@pytest.mark.parametrize("width", [30, 50, 80, 120])
def test_it_honours_the_renderer_contract(width: int) -> None:
    assert_lines_fit(prompt(), width)
    answered = prompt()
    answered.resolve(PermissionAnswer(True, GrantScope.ALWAYS))
    assert_lines_fit(answered, width)
