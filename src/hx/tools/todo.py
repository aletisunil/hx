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

from hx.tools.base import Tool, ToolContext, ToolResult

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


@dataclass(slots=True)
class TodoList:
    items: list[Todo] = field(default_factory=list)

    def replace(self, items: list[Todo]) -> None:
        """Raises :class:`~hx.tools.base.ToolError` if more than one item is in progress."""
        raise NotImplementedError

    def render(self) -> str:
        """Markdown checklist for late injection."""
        raise NotImplementedError

    def is_empty(self) -> bool:
        raise NotImplementedError


class TodoWriteTool(Tool):
    name = "TodoWrite"
    description = DESCRIPTION
    mutating = False

    def __init__(self, todos: TodoList, bus: Any) -> None:
        raise NotImplementedError

    def schema(self) -> dict[str, Any]:
        raise NotImplementedError

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Replaces the list wholesale and publishes ``TodosUpdated`` for the sidebar."""
        raise NotImplementedError


def todo_injector(todos: TodoList) -> Any:
    """Late injector emitting the current list, or ``None`` when empty."""
    raise NotImplementedError
