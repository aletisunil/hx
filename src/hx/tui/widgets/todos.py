"""Todo sidebar. Toggled with Ctrl+T, auto-shown when the list is non-empty."""

from __future__ import annotations

import time
from typing import Any, ClassVar

from rich.console import RenderableType
from rich.text import Text
from textual.widgets import Static

MARKERS: dict[str, tuple[str, str]] = {
    "completed": ("✓", "green"),
    "in_progress": ("▸", "bold yellow"),
    "pending": ("○", "dim"),
}


class TodoSidebar(Static):
    def __init__(self) -> None:
        super().__init__(id="todos")
        self.todos: list[dict[str, Any]] = []

    def update_todos(self, todos: list[Any]) -> None:
        self.todos = [t if isinstance(t, dict) else vars(t) for t in todos]
        # The list is only worth screen space when there is something on it.
        self.set_visible(bool(self.todos))
        self.refresh(layout=True)

    def set_visible(self, visible: bool) -> None:
        self.set_class(visible, "visible")

    def render(self) -> RenderableType:
        if not self.todos:
            return Text("no todos", style="dim")
        body = Text()
        body.append("Todos\n", style="bold")
        for todo in self.todos:
            status = str(todo.get("status", "pending"))
            marker, style = MARKERS.get(status, MARKERS["pending"])
            label = todo.get("active_form") if status == "in_progress" else todo.get("content")
            body.append(f"{marker} ", style=style)
            body.append(f"{label}\n", style="strike dim" if status == "completed" else "")
        return body


class SubagentRows(Static):
    """Live rows for running subagents: type, description, elapsed, tool count."""

    REFRESH_INTERVAL: ClassVar[float] = 1.0

    def __init__(self) -> None:
        super().__init__(id="subagents")
        self.rows: dict[str, dict[str, Any]] = {}

    def on_mount(self) -> None:
        self.set_interval(self.REFRESH_INTERVAL, self.refresh)

    def start(self, subagent_id: str, agent_type: str, description: str) -> None:
        self.rows[subagent_id] = {
            "type": agent_type,
            "description": description,
            "started": time.monotonic(),
            "done": False,
            "error": False,
        }
        self.refresh(layout=True)

    def finish(self, subagent_id: str, is_error: bool) -> None:
        row = self.rows.get(subagent_id)
        if row is None:
            return
        row["done"] = True
        row["error"] = is_error
        self.refresh(layout=True)

    def render(self) -> RenderableType:
        active = {k: v for k, v in self.rows.items() if not v["done"]}
        if not active:
            return Text("")
        body = Text()
        for row in active.values():
            elapsed = time.monotonic() - row["started"]
            body.append("⠿ ", style="yellow")
            body.append(f"{row['type']} ", style="bold")
            body.append(f"{row['description']} ", style="dim")
            body.append(f"{elapsed:.0f}s\n", style="dim")
        return body
