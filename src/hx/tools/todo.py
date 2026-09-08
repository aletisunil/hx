"""Todo and plan tracking.

The list is state, not conversation: it is rendered into context by a late
injector every turn (see ``hx.core.lateinject``) rather than accumulating as
tool results, so an updated list does not push the previous six copies of
itself through the cache.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from hx.core.lateinject import Injection
from hx.tools.base import Tool, ToolContext, ToolError, ToolResult

DESCRIPTION = """Create and update a structured task list for the current work.

Use it for multi-step tasks. Exactly one item may be `in_progress` at a time;
mark items completed as soon as they are done, not in a batch at the end."""


class TodoStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


@dataclass(slots=True)
class Todo:
    content: str
    status: TodoStatus = TodoStatus.PENDING
    active_form: str = ""
    """Present-continuous phrasing shown while the item is in progress."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "status": self.status.value,
            "active_form": self.active_form,
        }


@dataclass(slots=True)
class TodoList:
    items: list[Todo] = field(default_factory=list)

    def replace(self, items: list[Todo]) -> None:
        """Raises :class:`ToolError` if more than one item is in progress."""
        in_progress = [item for item in items if item.status is TodoStatus.IN_PROGRESS]
        if len(in_progress) > 1:
            raise ToolError(
                f"{len(in_progress)} items are in_progress. Exactly one may be in progress at a time."
            )
        self.items = list(items)

    def render(self) -> str:
        """Markdown checklist for late injection."""
        marks = {
            TodoStatus.COMPLETED: "[x]",
            TodoStatus.IN_PROGRESS: "[~]",
            TodoStatus.PENDING: "[ ]",
        }
        lines = ["Current todo list (keep it up to date as you work):"]
        lines += [f"{marks[item.status]} {item.content}" for item in self.items]
        return "\n".join(lines)

    def is_empty(self) -> bool:
        return not self.items

    def as_dicts(self) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self.items]


class TodoWriteTool(Tool):
    name = "TodoWrite"
    description = DESCRIPTION
    mutating = False

    def __init__(self, todos: TodoList, bus: Any = None) -> None:
        self.todos = todos
        self.bus = bus

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": [s.value for s in TodoStatus],
                            },
                            "active_form": {
                                "type": "string",
                                "description": "Present continuous, e.g. 'Running the tests'",
                            },
                        },
                        "required": ["content", "status"],
                    },
                }
            },
            "required": ["todos"],
        }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Replaces the list wholesale and publishes ``TodosUpdated`` for the sidebar."""
        items = [_parse_todo(raw) for raw in params["todos"]]
        self.todos.replace(items)

        if self.bus is not None:
            from hx.core.events import TodosUpdated

            self.bus.publish(TodosUpdated(todos=self.todos.as_dicts()))

        done = sum(1 for item in items if item.status is TodoStatus.COMPLETED)
        return ToolResult(
            # The list itself is late-injected every turn, so echoing it back
            # here would put a second, immediately stale copy in the transcript.
            content=f"Todo list updated: {done}/{len(items)} complete.",
            summary=f"{done}/{len(items)} done",
        )


def _parse_todo(raw: dict[str, Any]) -> Todo:
    try:
        status = TodoStatus(str(raw.get("status", "pending")))
    except ValueError as exc:
        raise ToolError(f"unknown todo status {raw.get('status')!r}") from exc
    content = str(raw.get("content", "")).strip()
    if not content:
        raise ToolError("every todo needs content")
    return Todo(content=content, status=status, active_form=str(raw.get("active_form", "")))


def todo_injector(todos: TodoList) -> Any:
    """Late injector emitting the current list, or ``None`` when empty."""

    def inject() -> Injection | None:
        if todos.is_empty():
            return None
        return Injection(source="todos", text=todos.render(), priority=10)

    return inject
