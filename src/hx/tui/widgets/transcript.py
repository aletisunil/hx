"""Transcript pane: streaming markdown, per-tool blocks, system notices.

The visual grammar, top to bottom, is deliberately the one pi uses: the user's
turn sits in a tinted block so the eye can find where each exchange starts,
assistant prose is markdown in the theme's colours, and every tool call is a
card whose tint says pending / done / failed without a word being read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from rich.console import Group, RenderableType
from rich.markdown import Heading, Markdown
from rich.padding import Padding
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static

from hx.tui.renderers import ToolCall, renderer_for
from hx.tui.theme import THEME, syntax_style


class _PlainHeading(Heading):
    """Markdown headings as coloured text, not centred boxes.

    Rich centres ``#`` headings inside a panel by default, which in a chat
    transcript reads as a banner interrupting the answer rather than as a
    section title.
    """

    def __rich_console__(self, console: Any, options: Any) -> Any:
        text = self.text
        text.justify = "left"
        text.stylize(THEME.fg("md_heading", bold=True))
        if self.tag == "h1":
            yield text
        else:
            yield text


class _ThemedMarkdown(Markdown):
    """Markdown with HX's heading renderer.

    ``elements`` is a class attribute on Rich's Markdown, so it is overridden on
    a subclass rather than mutated per instance - patching the instance would
    reach through to every Markdown Rich renders.
    """

    elements: ClassVar[dict[str, Any]] = {**Markdown.elements, "heading_open": _PlainHeading}


def markdown(source: str) -> Markdown:
    """Assistant prose, themed to match the rest of the app."""
    style = syntax_style()
    return _ThemedMarkdown(
        source,
        code_theme=style,
        inline_code_theme=style,
        style=THEME.fg("text"),
        hyperlinks=True,
    )


class MessageBlock(Static):
    """One assistant, user or thinking message.

    Markdown is re-rendered from the accumulated buffer rather than per delta:
    a partial fenced block is not parseable, so incremental parsing would flick
    between two layouts on every token.
    """

    def __init__(self, role: str, text: str = "") -> None:
        super().__init__()
        self.role = role
        self.buffer = text
        self.add_class(f"role-{role}")

    def append(self, text: str) -> None:
        self.buffer += text

    def render(self) -> RenderableType:
        if self.role == "user":
            return Padding(
                Text(self.buffer, style=THEME.on("text", "user_bg")),
                (0, 1),
                style=THEME.bg("user_bg"),
            )
        if self.role == "thinking":
            return Padding(Text(self.buffer, style=THEME.fg("thinking", italic=True)), (0, 1))
        return Padding(markdown(self.buffer or ""), (0, 1))


class ToolBlock(Static):
    """One tool call, drawn by that tool's own renderer.

    The block is tinted by outcome - pending, succeeded, failed - because that
    is the question a reader asks of a scrollback full of tool calls, and a
    colour answers it faster than a glyph does.
    """

    STATE_STYLES: ClassVar[dict[str, tuple[str, str]]] = {
        "running": ("tool_pending_bg", "accent"),
        "done": ("tool_success_bg", "success"),
        "error": ("tool_error_bg", "error"),
    }

    def __init__(self, name: str, params: dict[str, Any], cwd: Path) -> None:
        super().__init__()
        self.tool_name = name
        self.params = params
        self.cwd = cwd
        self.output = ""
        self.summary: str | None = None
        self.metadata: dict[str, Any] = {}
        self.duration_ms = 0.0
        self.is_error = False
        self.expanded = False

    def append(self, chunk: str) -> None:
        self.output += chunk

    def on_click(self) -> None:
        """Clicking a block expands it, the same as the expand key does.

        The output is right there under the pointer; making the user find a
        keystroke to see the rest of it is a needless step.
        """
        self.expanded = not self.expanded
        self.refresh(layout=True)

    def finish(
        self,
        summary: str,
        is_error: bool,
        metadata: dict[str, Any] | None = None,
        duration_ms: float = 0.0,
    ) -> None:
        self.summary = summary
        self.is_error = is_error
        self.metadata = metadata or {}
        self.duration_ms = duration_ms

    @property
    def state(self) -> str:
        if self.is_error:
            return "error"
        return "done" if self.summary is not None else "running"

    def _call(self) -> ToolCall:
        return ToolCall(
            name=self.tool_name,
            params=self.params,
            cwd=self.cwd,
            output=self.output,
            summary=self.summary or "",
            metadata=self.metadata,
            is_error=self.is_error,
            finished=self.summary is not None,
            duration_ms=self.duration_ms,
            expanded=self.expanded,
        )

    def render(self) -> RenderableType:
        """A tinted block, not a boxed one.

        A border around every tool call turns a busy turn into a stack of
        rectangles; the tint carries the same state information and leaves the
        content as the loudest thing on screen.
        """
        call = self._call()
        renderer = renderer_for(self.tool_name)
        background, edge = self.STATE_STYLES[self.state]

        marker = {"running": "○", "done": "●", "error": "✗"}[self.state]
        header = Text(f"{marker} ", style=THEME.fg(edge))
        header.append_text(renderer.header(call))

        body = renderer.body(call)
        content: RenderableType = Group(header, body) if body is not None else header
        return Padding(content, (0, 1), style=THEME.bg(background))


class Notice(Static):
    """A system notice: compaction, model switch, degraded sandbox, errors."""

    LEVELS: ClassVar[dict[str, str]] = {
        "info": "muted",
        "warning": "warning",
        "error": "error",
        "success": "success",
    }

    def __init__(self, text: str, level: str = "info") -> None:
        super().__init__()
        self.text = text
        self.level = level
        self.add_class(f"notice-{level}")

    def render(self) -> RenderableType:
        role = self.LEVELS.get(self.level, "muted")
        bullet = {"error": "✗", "warning": "!", "success": "✓"}.get(self.level, "·")
        body = Text(f"{bullet} ", style=THEME.fg(role, bold=self.level == "error"))
        body.append(self.text, style=THEME.fg(role))
        return body


class Transcript(VerticalScroll):
    """Scrollback for the conversation.

    Follows the tail only while the user is already at the bottom - yanking the
    view down mid-scroll while they are reading earlier output is the fastest
    way to make a TUI feel hostile.
    """

    def __init__(self, cwd: Path | None = None) -> None:
        super().__init__(id="transcript")
        self.cwd = cwd or Path.cwd()
        self._current: MessageBlock | None = None
        self._thinking: MessageBlock | None = None
        self._tools: dict[str, ToolBlock] = {}
        self._order: list[str] = []
        self.expanded = False
        #: Block the keyboard is pointing at, or None while following the tail.
        self.cursor: Static | None = None

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
        block = ToolBlock(name, params, self.cwd)
        block.expanded = self.expanded
        self._tools[tool_use_id] = block
        self._order.append(tool_use_id)
        self._mount_block(block)

    def update_tool_block(self, tool_use_id: str, chunk: str) -> None:
        block = self._tools.get(tool_use_id)
        if block is None:
            return
        block.append(chunk)
        block.refresh(layout=True)
        self._follow()

    def finish_tool_block(
        self,
        tool_use_id: str,
        summary: str,
        is_error: bool,
        metadata: dict[str, Any] | None = None,
        duration_ms: float = 0.0,
    ) -> None:
        """Settle the block into its final tint and one-line summary."""
        block = self._tools.get(tool_use_id)
        if block is None:
            return
        block.finish(summary, is_error, metadata, duration_ms)
        block.refresh(layout=True)
        self._follow()

    def toggle_expanded(self) -> bool:
        """Expand tool output: the cursored block alone, or all of them.

        With no cursor the key means "all", because every collapsed block
        advertises the same shortcut and toggling only the newest would make
        that hint a lie on each block above it. Once the user has moved the
        cursor they have named a block, so the key applies to that one.
        Returns the resulting state of whatever was toggled.
        """
        cursored = self.cursor
        if isinstance(cursored, ToolBlock):
            cursored.expanded = not cursored.expanded
            cursored.refresh(layout=True)
            return cursored.expanded

        self.expanded = not self.expanded
        for block in self._tools.values():
            block.expanded = self.expanded
            block.refresh(layout=True)
        self._follow()
        return self.expanded

    # -- Keyboard navigation ------------------------------------------------
    #
    # Focus stays in the prompt the whole time, as it does in pi: the reader
    # scrolls and steps through messages without ever losing the ability to
    # start typing. So these are driven by app-level bindings, not by focus.

    def page_up(self) -> None:
        self.scroll_page_up(animate=False)

    def page_down(self) -> None:
        self.scroll_page_down(animate=False)

    def scroll_to_top(self) -> None:
        self.scroll_home(animate=False)

    def scroll_to_bottom(self) -> None:
        """Back to the tail, and following it again."""
        self.set_cursor(None)
        self.scroll_end(animate=False)

    def _navigable(self) -> list[Static]:
        """Blocks the cursor stops on: the two halves of an exchange.

        Tool blocks and notices are skipped. Stepping through forty of them to
        reach the previous question is exactly the scrolling this replaces.
        """
        return [
            child
            for child in self.children
            if isinstance(child, MessageBlock) and child.role in {"user", "assistant"}
        ]

    def set_cursor(self, block: Static | None) -> None:
        if self.cursor is not None:
            self.cursor.remove_class("cursored")
        self.cursor = block
        if block is not None:
            block.add_class("cursored")
            self.scroll_to_widget(block, animate=False, top=True)

    def move_cursor(self, delta: int) -> Static | None:
        """Step to the next or previous message, and show it.

        Starting from the bottom, so the first press goes to the last message
        rather than the first one - which is where the reader just was.
        """
        blocks = self._navigable()
        if not blocks:
            return None
        if self.cursor is None or self.cursor not in blocks:
            index = len(blocks) - 1 if delta < 0 else 0
        else:
            index = blocks.index(self.cursor) + delta
            index = max(0, min(len(blocks) - 1, index))
        self.set_cursor(blocks[index])
        return blocks[index]

    def cursored_text(self) -> str | None:
        """Text of the cursored block, falling back to the last message.

        The fallback is what makes the copy key useful without navigating
        first: the thing a user most often wants is the answer just given.
        """
        if isinstance(self.cursor, MessageBlock):
            return self.cursor.buffer
        for child in reversed(list(self.children)):
            if isinstance(child, MessageBlock) and child.role == "assistant":
                return child.buffer
        return None

    def add_notice(self, text: str, level: str = "info") -> None:
        """System notices: compaction, model switch, sandbox degraded."""
        self._current = None
        self._mount_block(Notice(text, level))

    def clear_all(self) -> None:
        self._current = None
        self._thinking = None
        self.cursor = None
        self._tools.clear()
        self._order.clear()
        self.remove_children()
