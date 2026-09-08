"""Todos, and why they are late-injected rather than left in the transcript."""

from __future__ import annotations

from pathlib import Path

import pytest

from hx.config import load_settings
from hx.core.lateinject import InjectionRegistry
from hx.core.messages import user_message
from hx.tools.base import ToolContext, ToolError
from hx.tools.todo import Todo, TodoList, TodoStatus, TodoWriteTool, todo_injector


@pytest.fixture()
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        cwd=tmp_path,
        session_id="s",
        tool_use_id="t1",
        settings=load_settings(tmp_path),
        emit_progress=lambda _chunk: None,
    )


def test_only_one_item_may_be_in_progress() -> None:
    todos = TodoList()
    with pytest.raises(ToolError, match="in_progress"):
        todos.replace(
            [
                Todo("a", TodoStatus.IN_PROGRESS),
                Todo("b", TodoStatus.IN_PROGRESS),
            ]
        )


def test_render_marks_each_state() -> None:
    todos = TodoList()
    todos.replace(
        [
            Todo("done thing", TodoStatus.COMPLETED),
            Todo("doing thing", TodoStatus.IN_PROGRESS, "Doing thing"),
            Todo("later thing", TodoStatus.PENDING),
        ]
    )
    rendered = todos.render()
    assert "[x] done thing" in rendered
    assert "[~] doing thing" in rendered
    assert "[ ] later thing" in rendered


async def test_tool_does_not_echo_the_list_back(ctx: ToolContext) -> None:
    """The list is injected fresh every turn; echoing it here would leave a
    second, immediately stale copy in the transcript."""
    todos = TodoList()
    result = await TodoWriteTool(todos).run(
        {"todos": [{"content": "write tests", "status": "pending"}]}, ctx
    )
    assert "write tests" not in result.content
    assert "0/1 complete" in result.content


async def test_tool_publishes_for_the_sidebar(ctx: ToolContext) -> None:
    from hx.core.events import EventBus, TodosUpdated

    published: list[object] = []
    bus = EventBus()

    class Recorder(EventBus):
        def publish(self, event: object) -> None:
            published.append(event)

    await TodoWriteTool(TodoList(), Recorder()).run(
        {"todos": [{"content": "x", "status": "completed"}]}, ctx
    )
    assert isinstance(published[0], TodosUpdated)
    assert bus is not None


async def test_bad_status_is_reported_clearly(ctx: ToolContext) -> None:
    with pytest.raises(ToolError, match="unknown todo status"):
        await TodoWriteTool(TodoList()).run({"todos": [{"content": "x", "status": "nearly"}]}, ctx)


def test_injector_is_silent_while_the_list_is_empty() -> None:
    assert todo_injector(TodoList())() is None


def test_updating_todos_replaces_rather_than_accumulates() -> None:
    """Six turns of updates must leave one copy in context, not six."""
    todos = TodoList()
    registry = InjectionRegistry()
    registry.register("todos", todo_injector(todos))

    messages = [user_message("go")]
    for step in range(6):
        todos.replace([Todo(f"step {step}", TodoStatus.IN_PROGRESS)])
        messages = registry.apply(messages)

    text = messages[-1].text()
    assert text.count("<hx-reminder>") == 1
    assert "step 5" in text
    assert "step 4" not in text
