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
