"""Todo sidebar and subagent rows.

Both answer "what is this turn actually doing", one at plan level and one at
worker level. The sidebar appears on its own when there is a plan to show and
is toggled with Ctrl+T; the rows appear only while a subagent is running.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

from rich.console import RenderableType
from rich.text import Text
from textual.widgets import Static

from hx.tui.theme import THEME
from hx.tui.widgets.working import FRAMES

MARKERS: dict[str, tuple[str, str]] = {
    "completed": ("✓", "success"),
    "in_progress": ("▸", "accent"),
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
            return Text("no todos", style=THEME.fg("dim"))

        done = sum(1 for todo in self.todos if todo.get("status") == "completed")
        body = Text("Todos ", style=THEME.fg("accent", bold=True))
        body.append(f"{done}/{len(self.todos)}\n", style=THEME.fg("dim"))
        for todo in self.todos:
            status = str(todo.get("status", "pending"))
            marker, role = MARKERS.get(status, MARKERS["pending"])
            # active_form is optional; falling through to content keeps a todo
            # from rendering as the literal string "None".
            content = str(todo.get("content") or "")
            label = str(todo.get("active_form") or content) if status == "in_progress" else content
            body.append(f"{marker} ", style=THEME.fg(role))
            style = {
                "completed": f"strike {THEME.fg('dim')}",
                "in_progress": THEME.fg("text"),
            }.get(status, THEME.fg("muted"))
            body.append(f"{label}\n", style=style)
        return body


class SubagentRows(Static):
    """Live rows for running subagents: type, description and elapsed time."""

    REFRESH_INTERVAL: ClassVar[float] = 0.1

    def __init__(self) -> None:
        super().__init__(id="subagents")
        self.rows: dict[str, dict[str, Any]] = {}
        self._frame = 0

    def on_mount(self) -> None:
        self.set_interval(self.REFRESH_INTERVAL, self._tick)

    def _tick(self) -> None:
        """Repaint only while something is running; an idle spinner is a lie
        about work being done, and a wasted frame besides."""
        if not any(not row["done"] for row in self.rows.values()):
            return
        self._frame = (self._frame + 1) % len(FRAMES)
        self.refresh()

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
            body.append(f"{FRAMES[self._frame]} ", style=THEME.fg("accent"))
            body.append(f"{row['type']} ", style=THEME.fg("text", bold=True))
            body.append(f"{row['description']} ", style=THEME.fg("muted"))
            body.append(f"{elapsed:.0f}s\n", style=THEME.fg("dim"))
        # Text.rstrip mutates in place and returns None.
        body.rstrip()
        return body
