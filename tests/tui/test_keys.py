"""The keybinding registry: defaults, overrides, conflicts, and the names the UI shows."""

from __future__ import annotations

import json
from pathlib import Path

from hx.keys import KEYMAP, action_name, bindings_for, display_key, load_keymap


def test_defaults_resolve_to_keys() -> None:
    keymap = load_keymap(overrides={})
    assert keymap.keys_for("app.interrupt") == ("escape",)
    assert keymap.keys_for("app.tools.expand") == ("ctrl+o", "ctrl+r")
    assert keymap.problems == []


def test_a_user_override_wins() -> None:
    keymap = load_keymap(overrides={"app.tools.expand": "ctrl+e"})
    assert keymap.keys_for("app.tools.expand") == ("ctrl+e",)
    assert keymap.problems == []


def test_an_override_may_be_a_list() -> None:
    keymap = load_keymap(overrides={"app.message.copy": ["ctrl+x", "alt+c"]})
    assert keymap.keys_for("app.message.copy") == ("ctrl+x", "alt+c")


def test_an_override_file_is_read(hx_home: Path) -> None:
    (hx_home / "keybindings.json").write_text(json.dumps({"app.todos.toggle": "ctrl+g"}))
    keymap = load_keymap()
    assert keymap.keys_for("app.todos.toggle") == ("ctrl+g",)


def test_a_broken_override_file_is_reported_not_fatal(hx_home: Path) -> None:
    (hx_home / "keybindings.json").write_text("{not json")
    keymap = load_keymap()
    # Defaults survive, and the user is told why their file did nothing.
    assert keymap.keys_for("app.todos.toggle") == ("ctrl+t",)
    assert any("keybindings.json" in problem for problem in keymap.problems)


def test_an_unknown_action_is_reported() -> None:
    keymap = load_keymap(overrides={"app.nope": "ctrl+g"})
    assert any("app.nope" in problem for problem in keymap.problems)


def test_two_actions_on_one_key_is_a_conflict() -> None:
    keymap = load_keymap(overrides={"app.todos.toggle": "ctrl+p"})
    assert any("ctrl+p" in problem for problem in keymap.problems)


def test_the_shipped_defaults_do_not_conflict() -> None:
    """The one that matters: a shipped conflict would be a silently dead key."""
    assert KEYMAP.problems == []


def test_display_names_are_what_a_terminal_user_writes() -> None:
    assert display_key("escape") == "esc"
    assert display_key("pageup") == "pgup"
    assert display_key("ctrl+o") == "ctrl+o"


def test_text_lists_every_key_and_primary_lists_one() -> None:
    keymap = load_keymap(overrides={})
    assert keymap.text("app.tools.expand") == "ctrl+o/ctrl+r"
    assert keymap.primary("app.tools.expand") == "ctrl+o"


def test_action_names_flatten_to_textual_handlers() -> None:
    assert action_name("app.transcript.previousPrompt") == "transcript_previous_prompt"
    assert action_name("tui.editor.yankPop") == "editor_yank_pop"


def test_bindings_carry_every_key_for_an_action() -> None:
    binding = bindings_for("app.tools.expand")[0]
    assert binding.key == "ctrl+o,ctrl+r"
    assert binding.action == "tools_expand"


def test_help_lists_keys_from_the_registry() -> None:
    """The point of the registry: no second, hand-maintained copy of the keys."""
    from hx.tui.commands import help_keys

    rows = dict(help_keys())
    assert rows["esc"] == KEYMAP.description("app.interrupt")
    assert "ctrl+o/ctrl+r" in rows


def test_a_priority_action_shares_its_key_without_that_being_a_conflict() -> None:
    """``ctrl+c`` copies a transcript selection and otherwise clears the prompt.

    That is a chain, not a collision: the priority action skips when it has
    nothing to do and the key carries on. The conflict check exists to catch a
    key that silently does nothing, which is the opposite.
    """
    keymap = load_keymap(overrides={})

    assert keymap.keys_for("app.selection.copy") == ("ctrl+c",)
    assert keymap.keys_for("app.clear") == ("ctrl+c",)
    assert keymap.problems == []


def test_two_priority_actions_on_one_key_still_conflict() -> None:
    """Whichever loses never runs, which is the thing worth reporting."""
    from hx.keys import KeyBinding, _conflicts

    bindings = {
        "a.one": KeyBinding("a.one", ("ctrl+c",), "one", priority=True),
        "a.two": KeyBinding("a.two", ("ctrl+c",), "two", priority=True),
    }
    keys = {action: binding.default_keys for action, binding in bindings.items()}

    assert _conflicts(keys, bindings) == ["ctrl+c is bound to a.one and a.two"]


def test_the_priority_flag_reaches_textual() -> None:
    """Without it the binding is checked after the focused widget's own, and
    the prompt - a TextArea that binds ctrl+c - is always the focused widget."""
    assert bindings_for("app.selection.copy")[0].priority is True
    assert bindings_for("app.clear")[0].priority is False


def test_the_prompt_spells_the_steer_key_the_way_help_does() -> None:
    """One key, two names: ``alt`` on Linux, ``option`` on a Mac keyboard.

    The placeholder used to hard-code ``alt+enter``, so on macOS - where
    ``display_key`` writes ``option`` - the prompt and ``/help`` named the same
    key differently, and a rebind left the placeholder pointing at a key that
    no longer steered.
    """
    from hx.tui.widgets.input import running_placeholder

    steer = KEYMAP.primary("tui.input.steer")

    assert f"{steer} steers" in running_placeholder()
    assert f"{steer} queues" in running_placeholder(enter_steers=True)
