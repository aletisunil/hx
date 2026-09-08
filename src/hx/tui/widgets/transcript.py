"""Transcript pane: streaming markdown plus collapsible tool blocks."""

from __future__ import annotations

from typing import Any, ClassVar

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static

from hx.core.loop import _brief

MAX_TOOL_PREVIEW_LINES = 12
"""Lines of live tool output kept on screen before the block starts scrolling
its own tail. The full output is always available via Ctrl+R."""


class MessageBlock(Static):
    """One assistant or user message. Markdown is re-rendered on a tick, not per delta."""

    def __init__(self, role: str, text: str = "") -> None:
        super().__init__()
        self.role = role
        self.buffer = text
        self._dirty = True

    def append(self, text: str) -> None:
        self.buffer += text
        self._dirty = True

    def render(self) -> RenderableType:
        self._dirty = False
        if self.role == "user":
            return Text(f"> {self.buffer}", style="bold")
        if self.role == "thinking":
            return Text(self.buffer, style="dim italic")
        return Markdown(self.buffer or "")

    @property
    def dirty(self) -> bool:
        return self._dirty


class ToolBlock(Static):
    """A single tool call: header, live output, then a collapsed summary."""

    def __init__(self, name: str, params: dict[str, Any]) -> None:
        super().__init__()
        self.tool_name = name
        self.params = params
        self.output = ""
        self.summary: str | None = None
        self.is_error = False
        self.expanded = False

    def append(self, chunk: str) -> None:
        self.output += chunk

    def finish(self, summary: str, is_error: bool) -> None:
        self.summary = summary
        self.is_error = is_error

    def render(self) -> RenderableType:
        marker = "✗" if self.is_error else ("●" if self.summary else "○")
        style = "red" if self.is_error else ("green" if self.summary else "yellow")
        header = Text.assemble(
            (f"{marker} ", style),
            (self.tool_name, "bold"),
            (f"({_brief(self.params)})", "dim"),
        )
        if self.summary and not self.expanded:
            header.append(f"  {self.summary}", style="dim")
            return header

        body = self.output
        if not self.expanded:
            lines = body.splitlines()
            if len(lines) > MAX_TOOL_PREVIEW_LINES:
                hidden = len(lines) - MAX_TOOL_PREVIEW_LINES
                body = "\n".join([f"… {hidden} earlier lines", *lines[-MAX_TOOL_PREVIEW_LINES:]])
        return Group(header, Text(body, style="dim")) if body else header


class Notice(Static):
    """A system notice: compaction, model switch, degraded sandbox, errors."""

    STYLES: ClassVar[dict[str, str]] = {
        "info": "dim",
        "warning": "yellow",
        "error": "bold red",
        "success": "green",
    }

    def __init__(self, text: str, level: str = "info") -> None:
        super().__init__()
        self.text = text
        self.level = level

    def render(self) -> RenderableType:
        return Text(self.text, style=self.STYLES.get(self.level, "dim"))


class Transcript(VerticalScroll):
    """Scrollback for the conversation.

    Follows the tail only while the user is already at the bottom - yanking the
    view down mid-scroll while they are reading earlier output is the fastest
    way to make a TUI feel hostile.
    """

    def __init__(self) -> None:
        super().__init__(id="transcript")
        self._current: MessageBlock | None = None
        self._thinking: MessageBlock | None = None
        self._tools: dict[str, ToolBlock] = {}

    def _follow(self) -> None:
        if self.is_vertical_scroll_end:
            self.scroll_end(animate=False)

    def _mount_block(self, widget: Static) -> None:
        at_end = self.is_vertical_scroll_end
        self.mount(widget)
        if at_end:
            self.scroll_end(animate=False)

    def add_user_message(self, text: str) -> None:
        self._current = None
        self._thinking = None
        self._mount_block(MessageBlock("user", text))

    def start_assistant_message(self) -> None:
        self._current = None
        self._thinking = None

    def append_delta(self, text: str) -> None:
        if self._current is None:
            self._current = MessageBlock("assistant")
            self._mount_block(self._current)
        self._current.append(text)
        self._current.refresh(layout=True)
        self._follow()

    def append_thinking(self, text: str) -> None:
        if self._thinking is None:
            self._thinking = MessageBlock("thinking")
            self._mount_block(self._thinking)
        self._thinking.append(text)
        self._thinking.refresh(layout=True)
        self._follow()

    def add_tool_block(self, tool_use_id: str, name: str, params: dict[str, Any]) -> None:
        self._current = None
        block = ToolBlock(name, params)
        self._tools[tool_use_id] = block
        self._mount_block(block)

    def update_tool_block(self, tool_use_id: str, chunk: str) -> None:
        block = self._tools.get(tool_use_id)
        if block is None:
            return
        block.append(chunk)
        block.refresh(layout=True)
        self._follow()

    def finish_tool_block(self, tool_use_id: str, summary: str, is_error: bool) -> None:
        """Collapse to a one-line summary; Ctrl+R expands the last one."""
        block = self._tools.get(tool_use_id)
        if block is None:
            return
        block.finish(summary, is_error)
        block.refresh(layout=True)
        self._follow()

    def toggle_last_tool(self) -> None:
        if not self._tools:
            return
        block = list(self._tools.values())[-1]
        block.expanded = not block.expanded
        block.refresh(layout=True)
        self._follow()

    def add_notice(self, text: str, level: str = "info") -> None:
        """System notices: compaction, model switch, sandbox degraded."""
        self._current = None
        self._mount_block(Notice(text, level))
