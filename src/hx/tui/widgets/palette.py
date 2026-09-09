"""Modal pickers: slash commands, model selection, session resume."""

from __future__ import annotations

import time
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from hx.core.usage import format_tokens
from hx.providers.models import match_models
from hx.tui.fuzzy import filter_items
from hx.tui.theme import THEME
from hx.tui.widgets.rule import Rule

#: Marks the row Enter will take, the way pi marks a selection. A highlight
#: colour alone is ambiguous on a terminal that renders it faintly.
CURSOR = "› "  # noqa: RUF001 - the marker pi uses, not a greater-than
PAD = "  "


class FilteredPicker(ModalScreen[str | None]):
    """A filter box over a list of options.

    Typing narrows, Enter picks, Escape cancels. Subclasses supply the rows and
    the value each row resolves to.

    The filter box holds focus so typing always reaches it, which means the
    arrow keys never reach the list on their own - ``Input`` does not bind them,
    so they would bubble past the list and die. They are bound here and
    forwarded, and a row is kept highlighted at all times: a list where nothing
    is selected gives no clue what Enter is about to pick.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "dismiss_none", "Cancel"),
        ("down", "cursor_down", "Next"),
        ("up", "cursor_up", "Previous"),
        ("pagedown", "page_down", "Page down"),
        ("pageup", "page_up", "Page up"),
    ]

    def __init__(self, title: str, placeholder: str = "Filter…", initial: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.placeholder = placeholder
        self.initial = initial
        self._labels: list[tuple[str, Text]] = []
        self._highlighted: int | None = None

    def rows(self, query: str) -> list[tuple[str, Text]]:
        """Return ``(value, label)`` pairs matching ``query``."""
        raise NotImplementedError

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Static(self.title_text, id="picker-title")
            yield Input(placeholder=self.placeholder, id="picker-filter")
            yield Rule(id="picker-rule")
            yield OptionList(id="picker-options")

    def on_mount(self) -> None:
        filter_box = self.query_one("#picker-filter", Input)
        # Pre-filled so a query typed as `/model opus` stays visible and editable
        # rather than silently narrowing a list the user cannot see the reason for.
        filter_box.value = self.initial
        filter_box.cursor_position = len(self.initial)
        self._refresh(self.initial)
        filter_box.focus()

    def _refresh(self, query: str) -> None:
        options = self.query_one("#picker-options", OptionList)
        options.clear_options()
        self._labels = self.rows(query)
        for index, (value, label) in enumerate(self._labels):
            options.add_option(Option(self._decorate(label, index == 0), id=value))
        options.highlighted = 0 if options.option_count else None

    def _decorate(self, label: Text, selected: bool) -> Text:
        row = Text(CURSOR if selected else PAD, style=THEME.fg("accent"))
        row.append_text(label.copy())
        if selected:
            row.stylize(THEME.fg("text", bold=True), len(CURSOR))
        return row

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        """Move the cursor marker with the highlight.

        Only the two rows that changed are redrawn, so this stays cheap on a
        two-hundred-row model list.
        """
        options = event.option_list
        new = event.option_index
        old = self._highlighted
        self._highlighted = new
        for index, selected in ((old, False), (new, True)):
            if index is None or not (0 <= index < len(self._labels)):
                continue
            options.replace_option_prompt_at_index(
                index, self._decorate(self._labels[index][1], selected)
            )

    def on_input_changed(self, event: Input.Changed) -> None:
        self._refresh(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        options = self.query_one("#picker-options", OptionList)
        if not options.option_count:
            return
        index = options.highlighted if options.highlighted is not None else 0
        self.dismiss(options.get_option_at_index(index).id)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    @property
    def _options(self) -> OptionList:
        return self.query_one("#picker-options", OptionList)

    def action_cursor_down(self) -> None:
        self._options.action_cursor_down()

    def action_cursor_up(self) -> None:
        self._options.action_cursor_up()

    # Textual leaves the page actions untyped; the cursor ones are typed.
    def action_page_down(self) -> None:
        self._options.action_page_down()  # type: ignore[no-untyped-call]

    def action_page_up(self) -> None:
        self._options.action_page_up()  # type: ignore[no-untyped-call]

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class CommandPalette(FilteredPicker):
    """Fuzzy-filtered list of slash commands."""

    def __init__(self, commands: list[Any]) -> None:
        super().__init__("Commands", "Filter commands…")
        self.commands = commands

    def rows(self, query: str) -> list[tuple[str, Text]]:
        # Fuzzy over name *and* summary: in a palette the user is often looking
        # for a capability ("cost") rather than a command name they know.
        matches = filter_items(
            self.commands,
            query.strip().lstrip("/"),
            key=lambda command: f"{command.name} {command.summary}",
        )
        rows: list[tuple[str, Text]] = []
        for command in matches:
            label = Text(f"/{command.name:<14}", style=THEME.fg("text"))
            label.append(command.summary, style=THEME.fg("muted"))
            rows.append((command.name, label))
        return rows


class ModelPicker(FilteredPicker):
    """Model chooser.

    Each row shows id, context window, prompt/completion price per Mtok and
    whether the model supports prompt caching - switching to a model without
    caching has a real and otherwise invisible cost.
    """

    LIMIT: ClassVar[int] = 200

    def __init__(self, models: list[Any], current: str, initial: str = "") -> None:
        super().__init__("Select model", "Filter models…", initial)
        self.models = models
        self.current = current

    def rows(self, query: str) -> list[tuple[str, Text]]:
        # The same matcher `/model <query>` uses, so typing here narrows exactly
        # the way typing there does.
        matches = match_models(self.models, query)
        return [(model.id, self._label(model)) for model in matches[: self.LIMIT]]

    def _label(self, model: Any) -> Text:
        current = model.id == self.current
        prompt = model.pricing.prompt * 1_000_000
        completion = model.pricing.completion * 1_000_000
        cache_mode = str(model.cache_mode)
        cache = {"explicit": "cache✓", "implicit": "cache~", "none": "cache✗"}[cache_mode]
        cache_role = {"explicit": "success", "implicit": "warning", "none": "dim"}[cache_mode]

        label = Text("● " if current else "  ", style=THEME.fg("accent"))
        label.append(f"{model.id:<44}", style=THEME.fg("text", bold=current))
        label.append(f"{format_tokens(model.context_window):>7} ", style=THEME.fg("muted"))
        label.append(f"${prompt:>6.2f}/${completion:<6.2f} ", style=THEME.fg("dim"))
        label.append(cache, style=THEME.fg(cache_role))
        return label


class SessionPicker(FilteredPicker):
    """Resume a previous session in this directory."""

    def __init__(self, sessions: list[Any]) -> None:
        super().__init__("Resume session", "Filter sessions…")
        self.sessions = sessions

    def rows(self, query: str) -> list[tuple[str, Text]]:
        def plain(meta: Any) -> str:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(meta.updated_at))
            return f"{when} {meta.message_count} {meta.title or meta.session_id}"

        rows: list[tuple[str, Text]] = []
        for meta in filter_items(self.sessions, query, key=plain):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(meta.updated_at))
            label = Text(f"{when}  ", style=THEME.fg("dim"))
            label.append(f"{meta.message_count:>4} msgs  ", style=THEME.fg("muted"))
            label.append(meta.title or meta.session_id, style=THEME.fg("text"))
            rows.append((meta.session_id, label))
        return rows
