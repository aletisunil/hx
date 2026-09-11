"""Keybindings as one registry, so a key is described in exactly one place.

Before this, the same shortcut was written out in three: the Textual
``BINDINGS`` list, the ``/help`` text, and the README. They drifted, as three
copies of anything do. Now every key has an id, a default, and a description
here; the app builds its bindings from it, and ``/help``, the hints bar and the
expand affordances all render their key names through :func:`key_text`.

Users override defaults in ``~/.hx/keybindings.json``::

    {"app.tools.expand": "ctrl+r", "app.message.copy": ["ctrl+x", "alt+c"]}
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from hx.paths import keybindings_file

if TYPE_CHECKING:  # pragma: no cover - typing only
    from textual.binding import Binding


@dataclass(frozen=True, slots=True)
class KeyBinding:
    """One named action and the keys that trigger it."""

    id: str
    default_keys: tuple[str, ...]
    description: str
    #: Shown in the compact hints bar under the prompt.
    hint: str | None = None
    priority: bool = False
    """Checked before the focused widget's own bindings.

    For a key the prompt would otherwise swallow. The action is expected to
    raise ``SkipAction`` when it has nothing to do, so the key carries on to
    whatever would have handled it.
    """


def _wsl_style(env: dict[str, str] | None = None) -> bool:
    """True under WSL, where several terminals swallow the ctrl+shift chords.

    HX targets macOS and Linux, so WSL is the only variant worth carrying;
    pi does the same thing for the same reason.
    """
    environ = env if env is not None else dict(os.environ)
    return bool(environ.get("WSL_DISTRO_NAME") or environ.get("WSL_INTEROP"))


def _defaults() -> dict[str, KeyBinding]:
    wsl = _wsl_style()
    previous_prompt = ("ctrl+up",) if wsl else ("ctrl+up", "ctrl+shift+up")
    next_prompt = ("ctrl+down",) if wsl else ("ctrl+down", "ctrl+shift+down")

    bindings = [
        # Turn control.
        KeyBinding("app.interrupt", ("escape",), "Cancel or abort", hint="interrupt"),
        # Ahead of the prompt, which is a TextArea and binds ctrl+c to its own
        # copy: with focus in the prompt - where it always is - a selection in
        # the transcript was never what ctrl+c copied. Skips to the prompt's
        # copy, and then to app.clear, when nothing in the transcript is
        # selected.
        KeyBinding(
            "app.selection.copy",
            ("ctrl+c",),
            "Copy the selected text",
            priority=True,
        ),
        KeyBinding("app.clear", ("ctrl+c",), "Clear the prompt (twice to exit)"),
        KeyBinding("app.exit", ("ctrl+d",), "Exit when the prompt is empty"),
        KeyBinding("app.suspend", ("ctrl+z",), "Suspend to the background"),
        # Session and mode.
        KeyBinding("app.mode.cycle", ("shift+tab",), "Cycle permission mode"),
        KeyBinding("app.commands", ("ctrl+p",), "Open the command palette"),
        KeyBinding("app.model.select", ("ctrl+l",), "Open the model picker"),
        KeyBinding("app.todos.toggle", ("ctrl+t",), "Toggle the todo sidebar"),
        # Transcript.
        KeyBinding("app.tools.expand", ("ctrl+o", "ctrl+r"), "Expand tool output", hint="expand"),
        KeyBinding("app.message.copy", ("ctrl+x",), "Copy the selected message"),
        KeyBinding("app.transcript.pageUp", ("pageup",), "Scroll the transcript up"),
        KeyBinding("app.transcript.pageDown", ("pagedown",), "Scroll the transcript down"),
        KeyBinding("app.transcript.top", ("ctrl+home",), "Jump to the start"),
        KeyBinding("app.transcript.bottom", ("ctrl+end",), "Jump to the end"),
        KeyBinding("app.transcript.previousPrompt", previous_prompt, "Previous user message"),
        KeyBinding("app.transcript.nextPrompt", next_prompt, "Next user message"),
        # Prompt editing. Textual's TextArea supplies the rest of the readline
        # set; these are the ones it does not bind, plus the ones we move.
        KeyBinding("tui.input.submit", ("enter",), "Send the message"),
        KeyBinding("tui.input.steer", ("alt+enter",), "Steer the running turn"),
        KeyBinding("tui.input.newLine", ("shift+enter", "ctrl+j"), "Insert a newline"),
        KeyBinding("tui.input.complete", ("tab",), "Accept or cycle a completion"),
        KeyBinding("tui.editor.cursorLeft", ("ctrl+b",), "Move left one character"),
        KeyBinding("tui.editor.cursorRight", ("ctrl+f",), "Move right one character"),
        KeyBinding("tui.editor.cursorWordLeft", ("alt+b",), "Move left one word"),
        KeyBinding("tui.editor.cursorWordRight", ("alt+f",), "Move right one word"),
        KeyBinding("tui.editor.deleteWordForward", ("alt+d",), "Delete the word ahead"),
        KeyBinding("tui.editor.yank", ("ctrl+y",), "Yank the last kill"),
        KeyBinding("tui.editor.yankPop", ("alt+y",), "Cycle back through kills"),
        KeyBinding("tui.editor.redo", ("ctrl+shift+z",), "Redo"),
    ]
    return {binding.id: binding for binding in bindings}


def _normalize(value: object) -> tuple[str, ...] | None:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return None


@dataclass
class Keymap:
    """Resolved keys per action, plus whatever went wrong resolving them."""

    bindings: dict[str, KeyBinding]
    keys: dict[str, tuple[str, ...]]
    problems: list[str] = field(default_factory=list)

    def keys_for(self, action: str) -> tuple[str, ...]:
        return self.keys.get(action, ())

    def text(self, action: str) -> str:
        """``ctrl+o/ctrl+r`` - every key for an action, for ``/help``."""
        return "/".join(display_key(key) for key in self.keys_for(action))

    def primary(self, action: str) -> str:
        """Just the first key. What a compact hint should say.

        Listing the aliases is right in a reference and wrong in a hint: a hint
        has to be scannable, and ``ctrl+o/ctrl+r expand`` reads as two things.
        """
        keys = self.keys_for(action)
        return display_key(keys[0]) if keys else ""

    def description(self, action: str) -> str:
        binding = self.bindings.get(action)
        return binding.description if binding else action


#: Textual's key names spelled the way a terminal user writes them. Only the
#: names that differ; everything else is already what a person would type.
_DISPLAY = {
    "escape": "esc",
    "pageup": "pgup",
    "pagedown": "pgdn",
    "delete": "del",
}


def display_key(key: str) -> str:
    """``escape`` -> ``esc``, and ``alt`` -> ``option`` on macOS, as pi does."""
    parts = []
    for part in key.split("+"):
        part = _DISPLAY.get(part, part)
        if part == "alt" and sys.platform == "darwin":
            part = "option"
        parts.append(part)
    return "+".join(parts)


def _conflicts(
    keys: dict[str, tuple[str, ...]],
    bindings: dict[str, KeyBinding] | None = None,
) -> list[str]:
    """Actions sharing a key. Reported, never silently resolved.

    Two actions on one key is normally a mistake in a config file, and the one
    that loses is decided by dict order - which is not something a user can
    reason about, so say so instead.

    The exception is a ``priority`` action, which is *meant* to share: it runs
    first and skips when it has nothing to do, handing the key to whatever
    would have had it. That is a chain, not a collision - the point of this
    check is a key that silently does nothing, and a chain is the opposite. Two
    priority actions on one key is still a conflict: the loser never runs.
    """
    bindings = bindings or {}

    def is_priority(action: str) -> bool:
        binding = bindings.get(action)
        return binding.priority if binding else False

    owners: dict[str, list[str]] = defaultdict(list)
    for action, action_keys in keys.items():
        for key in action_keys:
            owners[key].append(action)

    problems: list[str] = []
    for key, actions in sorted(owners.items()):
        contenders = [action for action in actions if not is_priority(action)]
        prioritised = [action for action in actions if is_priority(action)]
        clashing = contenders if len(prioritised) <= 1 else actions
        if len(clashing) > 1:
            problems.append(f"{key} is bound to {' and '.join(clashing)}")
    return problems


def load_keymap(overrides: dict[str, object] | None = None) -> Keymap:
    """Defaults, with ``~/.hx/keybindings.json`` layered on top.

    A broken override file costs the user their overrides, not their ability to
    start HX: the reason lands in ``problems`` for the app to show as a notice.
    """
    bindings = _defaults()
    keys = {action: binding.default_keys for action, binding in bindings.items()}
    problems: list[str] = []

    if overrides is None:
        path = keybindings_file()
        overrides = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                problems.append(f"{path}: {exc}")
                loaded = {}
            if isinstance(loaded, dict):
                overrides = loaded
            elif loaded:
                problems.append(f"{path}: expected a JSON object of action -> key")

    for action, value in overrides.items():
        if action not in bindings:
            problems.append(f"unknown action {action!r} in keybindings")
            continue
        normalized = _normalize(value)
        if normalized is None:
            problems.append(f"{action}: expected a key or list of keys")
            continue
        keys[action] = normalized

    problems.extend(_conflicts(keys, bindings))
    return Keymap(bindings=bindings, keys=keys, problems=problems)


#: The keymap for this process. Resolved once: rebinding mid-session would
#: leave Textual's already-registered bindings pointing at the old keys.
KEYMAP = load_keymap()


def key_text(action: str) -> str:
    """Every key bound to ``action``, joined with ``/``."""
    return KEYMAP.text(action)


def primary_key(action: str) -> str:
    """The first key bound to ``action``, for compact hints."""
    return KEYMAP.primary(action)


def key_hint(action: str, description: str | None = None) -> tuple[str, str]:
    """``(key, description)`` for the hints bar, already joined for display."""
    return key_text(action), description or KEYMAP.description(action)


def bindings_for(*actions: str) -> list[Binding]:
    """Textual bindings for the given actions.

    The action name Textual dispatches to is the id with dots and camelCase
    flattened - ``app.transcript.pageUp`` becomes ``transcript_page_up`` - so a
    new keybinding needs a matching ``action_*`` method and nothing else.
    """
    from textual.binding import Binding

    bindings: list[Binding] = []
    for action in actions:
        binding = KEYMAP.bindings[action]
        keys = KEYMAP.keys_for(action)
        if not keys:
            continue
        bindings.append(
            Binding(
                ",".join(keys),
                action_name(action),
                binding.description,
                show=False,
                priority=binding.priority,
            )
        )
    return bindings


def action_name(action: str) -> str:
    """``app.transcript.pageUp`` -> ``transcript_page_up``."""
    parts = action.split(".")
    if parts and parts[0] in {"app", "tui"}:
        parts = parts[1:]
    flattened = "_".join(parts)
    out: list[str] = []
    for char in flattened:
        if char.isupper():
            out.append("_")
            out.append(char.lower())
        else:
            out.append(char)
    return "".join(out)
