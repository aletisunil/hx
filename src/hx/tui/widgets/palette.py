"""Modal pickers: slash commands, model selection, session resume."""

from __future__ import annotations

import time
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from hx.core.usage import format_tokens


class FilteredPicker(ModalScreen[str | None]):
    """A filter box over a list of options.

    Typing narrows, Enter picks, Escape cancels. Subclasses supply the rows and
    the value each row resolves to.
    """

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "dismiss_none", "Cancel")]

    def __init__(self, title: str, placeholder: str = "Filter…") -> None:
        super().__init__()
        self.title_text = title
        self.placeholder = placeholder

    def rows(self, query: str) -> list[tuple[str, str]]:
        """Return ``(value, label)`` pairs matching ``query``."""
        raise NotImplementedError

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Static(self.title_text, id="picker-title")
            yield Input(placeholder=self.placeholder, id="picker-filter")
            yield OptionList(id="picker-options")

    def on_mount(self) -> None:
        self._refresh("")
        self.query_one("#picker-filter", Input).focus()

    def _refresh(self, query: str) -> None:
        options = self.query_one("#picker-options", OptionList)
        options.clear_options()
        for value, label in self.rows(query):
            options.add_option(Option(label, id=value))

    def on_input_changed(self, event: Input.Changed) -> None:
        self._refresh(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        options = self.query_one("#picker-options", OptionList)
        index = options.highlighted if options.highlighted is not None else 0
        if options.option_count:
            self.dismiss(options.get_option_at_index(index).id)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class CommandPalette(FilteredPicker):
    """Fuzzy-filtered list of slash commands."""

    def __init__(self, commands: list[Any]) -> None:
        super().__init__("Commands", "Filter commands…")
        self.commands = commands

    def rows(self, query: str) -> list[tuple[str, str]]:
        needle = query.strip().lstrip("/").lower()
        return [
            (command.name, f"/{command.name:<14} {command.summary}")
            for command in self.commands
            if needle in command.name.lower()
        ]


class ModelPicker(FilteredPicker):
    """Model chooser.

    Each row shows id, context window, prompt/completion price per Mtok and
    whether the model supports prompt caching - switching to a model without
    caching has a real and otherwise invisible cost.
    """

    def __init__(self, models: list[Any], current: str) -> None:
        super().__init__("Select model", "Filter models…")
        self.models = models
        self.current = current

    def rows(self, query: str) -> list[tuple[str, str]]:
        needle = query.strip().lower()
        matches = [m for m in self.models if needle in f"{m.id} {m.name}".lower()]
        return [(model.id, self._label(model)) for model in matches[:200]]

    def _label(self, model: Any) -> str:
        marker = "●" if model.id == self.current else " "
        prompt = model.pricing.prompt * 1_000_000
        completion = model.pricing.completion * 1_000_000
        cache = {"explicit": "cache✓", "implicit": "cache~", "none": "cache✗"}[model.cache_mode]
        return (
            f"{marker} {model.id:<44} {format_tokens(model.context_window):>7} "
            f"${prompt:>6.2f}/${completion:<6.2f} {cache}"
        )


class SessionPicker(FilteredPicker):
    """Resume a previous session in this directory."""

    def __init__(self, sessions: list[Any]) -> None:
        super().__init__("Resume session", "Filter sessions…")
        self.sessions = sessions

    def rows(self, query: str) -> list[tuple[str, str]]:
        needle = query.strip().lower()
        rows: list[tuple[str, str]] = []
        for meta in self.sessions:
            label = (
                f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(meta.updated_at))}  "
                f"{meta.message_count:>4} msgs  {meta.title or meta.session_id}"
            )
            if needle in label.lower():
                rows.append((meta.session_id, label))
        return rows
