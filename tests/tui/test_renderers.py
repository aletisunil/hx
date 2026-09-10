"""Per-tool presentation.

These assert on what a user would read on screen - the verb, the path, the
diff - rather than on which renderer class produced it.
"""

from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

from hx.tui.renderers import (
    ToolCall,
    count_changes,
    display_path,
    expand_hint,
    render_diff,
    renderer_for,
)
from hx.tui.theme import THEME

DIFF = """\
--- a/fetcher.py
+++ b/fetcher.py
@@ -5,4 +5,4 @@
     async with httpx.AsyncClient() as client:
-        response = await client.get(url)
+        response = await _with_retry(client, url)
         response.raise_for_status()
"""


def _plain(renderable: object, width: int = 100) -> str:
    console = Console(width=width, record=True, force_terminal=False, file=io.StringIO())
    console.print(renderable)
    return console.export_text()


def _call(name: str, params: dict[str, object], **kwargs: object) -> ToolCall:
    return ToolCall(name=name, params=params, cwd=Path("/proj"), **kwargs)  # type: ignore[arg-type]


def test_a_read_is_named_by_its_file_not_its_arguments() -> None:
    """``Read(file_path='/proj/src/a.py')`` is a dict repr; ``read src/a.py``
    is what happened."""
    call = _call("Read", {"file_path": "/proj/src/a.py"}, summary="read 15 lines", finished=True)
    header = renderer_for("Read").header(call).plain
    assert header.startswith("read src/a.py")
    assert "file_path" not in header


def test_a_read_window_is_shown_as_a_line_range() -> None:
    call = _call("Read", {"file_path": "/proj/a.py", "offset": 10, "limit": 20})
    assert ":10-29" in renderer_for("Read").header(call).plain


def test_a_collapsed_read_shows_no_body() -> None:
    """The model read the file; the user did not ask to."""
    call = _call("Read", {"file_path": "/proj/a.py"}, output="     1\tx = 1", finished=True)
    assert renderer_for("Read").body(call) is None
    assert (
        renderer_for("Read").body(
            _call(
                "Read",
                {"file_path": "/proj/a.py"},
                output="     1\tx = 1",
                finished=True,
                expanded=True,
            )
        )
        is not None
    )


def test_a_command_is_shown_as_a_command() -> None:
    call = _call("Bash", {"command": "pytest -q", "timeout": 120})
    header = renderer_for("Bash").header(call).plain
    assert header.startswith("$ pytest -q")
    assert "timeout 120s" in header


def test_command_output_keeps_the_tail_and_says_what_it_hid() -> None:
    """The end of a command's output is the part that says whether it worked."""
    call = _call(
        "Bash",
        {"command": "pytest -q"},
        output="\n".join(f"line {i}" for i in range(30)),
        finished=True,
        duration_ms=4213.0,
    )
    body = _plain(renderer_for("Bash").body(call))
    assert "line 29" in body
    assert "line 0" not in body
    assert "more lines" in body and expand_hint() in body
    assert "Took 4.2s" in body


def test_an_edit_shows_its_diff_without_being_expanded() -> None:
    """The diff is the change; hiding it behind a keystroke hides the edit."""
    call = _call("Edit", {"file_path": "/proj/fetcher.py"}, metadata={"diff": DIFF}, finished=True)
    renderer = renderer_for("Edit")
    assert "+1 -1" in renderer.header(call).plain
    body = _plain(renderer.body(call))
    assert "-    6         response = await client.get(url)" in body
    assert "+    6         response = await _with_retry(client, url)" in body


def test_diff_lines_carry_the_file_line_numbers() -> None:
    """Numbered from the hunk header, so a reviewer can find the line again."""
    body = _plain(render_diff(DIFF))
    assert "     5     async with httpx.AsyncClient() as client:" in body
    assert "@@" not in body


def test_a_long_diff_says_how_much_it_left_out() -> None:
    diff = "@@ -1,40 +1,40 @@\n" + "\n".join(f"+line {i}" for i in range(40))
    body = _plain(render_diff(diff, max_lines=10))
    assert "more diff lines" in body


def test_only_the_words_that_changed_are_marked() -> None:
    """A one-token change on a long line is invisible if the whole line is red."""
    marked = render_diff(DIFF)
    styles = {str(span.style) for span in marked.spans}
    assert any("bold" in style for style in styles)


def test_a_todo_write_renders_the_plan_not_its_json() -> None:
    call = _call(
        "TodoWrite",
        {
            "todos": [
                {"content": "Add retry", "status": "completed", "active_form": "Adding retry"},
                {"content": "Write test", "status": "in_progress", "active_form": "Writing test"},
            ]
        },
        summary="1/2 done",
        finished=True,
    )
    body = _plain(renderer_for("TodoWrite").body(call))
    assert "✓ Add retry" in body
    assert "▸ Writing test" in body


def test_grep_does_not_name_the_directory_we_are_already_in() -> None:
    call = _call("Grep", {"pattern": "TODO", "path": "/proj"}, summary="0 results", finished=True)
    assert renderer_for("Grep").header(call).plain.strip() == "grep TODO  0 results"

    elsewhere = _call("Grep", {"pattern": "TODO", "path": "/proj/src"})
    assert "in src" in renderer_for("Grep").header(elsewhere).plain


def test_an_unknown_tool_still_gets_a_readable_header() -> None:
    """MCP servers and extensions bring tools HX has never heard of."""
    call = _call("mcp__notion__search", {"query": "roadmap", "page_size": 10})
    header = renderer_for("mcp__notion__search").header(call).plain
    assert "mcp__notion__search" in header
    assert "query='roadmap'" in header


def test_paths_are_shown_the_way_a_user_names_them() -> None:
    assert display_path("/proj/src/a.py", Path("/proj")) == "src/a.py"
    assert display_path(str(Path.home() / "notes.md"), Path("/proj")) == "~/notes.md"
    assert display_path("/etc/hosts", Path("/proj")) == "/etc/hosts"
    assert display_path(None, Path("/proj")) == ""


def test_change_counts_ignore_the_file_headers() -> None:
    assert count_changes(DIFF) == (1, 1)


def test_renderers_follow_the_active_palette() -> None:
    """A theme switch has to move the tool blocks too, not just the chrome."""
    call = _call("Bash", {"command": "ls"})
    THEME.use("dark")
    dark = str(renderer_for("Bash").header(call).spans)
    THEME.use("light")
    light = str(renderer_for("Bash").header(call).spans)
    THEME.use("dark")
    assert dark != light
