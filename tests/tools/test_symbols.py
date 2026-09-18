"""Symbols tool: outline, definition and references over tree-sitter."""

from __future__ import annotations

from pathlib import Path

import pytest

from hx.config import load_settings
from hx.tools.base import ToolContext, ToolError
from hx.tools.symbols import SymbolsTool, available

pytestmark = pytest.mark.skipif(not available(), reason="tree-sitter not installed")

PYTHON = '''\
class Greeter:
    def greet(self, name):
        return f"hello {name}"


def greet(name):
    """greet is also a module-level function."""
    return Greeter().greet(name)


def unrelated():
    # greet appears in this comment and must not match
    return "greet"
'''

GO = """\
package main

type Server struct{}

func (s *Server) Start() error {
	return nil
}

func main() {
	s := Server{}
	_ = s.Start()
}
"""


@pytest.fixture()
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        cwd=tmp_path,
        session_id="test-session",
        tool_use_id="t1",
        settings=load_settings(tmp_path),
        emit_progress=lambda _chunk: None,
    )


@pytest.fixture()
def project(ctx: ToolContext) -> Path:
    (ctx.cwd / "app.py").write_text(PYTHON)
    (ctx.cwd / "server.go").write_text(GO)
    return ctx.cwd


async def test_outline_lists_nested_definitions(ctx: ToolContext, project: Path) -> None:
    result = await SymbolsTool().run({"mode": "outline", "file_path": "app.py"}, ctx)
    assert "class Greeter" in result.content
    assert "  function greet" in result.content
    assert "function unrelated" in result.content


async def test_outline_reports_line_spans(ctx: ToolContext, project: Path) -> None:
    result = await SymbolsTool().run({"mode": "outline", "file_path": "app.py"}, ctx)
    assert "[1-3]" in result.content


async def test_outline_handles_go(ctx: ToolContext, project: Path) -> None:
    result = await SymbolsTool().run({"mode": "outline", "file_path": "server.go"}, ctx)
    assert "type Server" in result.content
    assert "method Start" in result.content


async def test_definition_finds_both_definitions(ctx: ToolContext, project: Path) -> None:
    result = await SymbolsTool().run({"mode": "definition", "symbol": "greet"}, ctx)
    assert result.content.count("app.py:") == 2


async def test_references_ignore_comments_and_strings(ctx: ToolContext, project: Path) -> None:
    """The point of parsing instead of grepping."""
    result = await SymbolsTool().run({"mode": "references", "symbol": "greet"}, ctx)
    assert "must not match" not in result.content
    assert 'return "greet"' not in result.content
    assert "Greeter().greet(name)" in result.content


async def test_references_exclude_the_definition_name(ctx: ToolContext, project: Path) -> None:
    refs = await SymbolsTool().run({"mode": "references", "symbol": "unrelated"}, ctx)
    assert "0 hits" in refs.summary


async def test_search_can_be_scoped_to_one_file(ctx: ToolContext, project: Path) -> None:
    result = await SymbolsTool().run(
        {"mode": "definition", "symbol": "Start", "file_path": "server.go"}, ctx
    )
    assert "server.go:" in result.content


async def test_limit_truncates(ctx: ToolContext, project: Path) -> None:
    result = await SymbolsTool().run({"mode": "references", "symbol": "greet", "limit": 1}, ctx)
    assert "stopped at 1 hits" in result.content


async def test_unsupported_extension_is_a_tool_error(ctx: ToolContext) -> None:
    (ctx.cwd / "notes.txt").write_text("nothing to parse")
    with pytest.raises(ToolError, match="unsupported language"):
        await SymbolsTool().run({"mode": "outline", "file_path": "notes.txt"}, ctx)


async def test_missing_symbol_argument_is_a_tool_error(ctx: ToolContext) -> None:
    with pytest.raises(ToolError, match="needs symbol"):
        await SymbolsTool().run({"mode": "definition"}, ctx)


async def test_no_hits_is_not_an_error(ctx: ToolContext, project: Path) -> None:
    result = await SymbolsTool().run({"mode": "definition", "symbol": "nowhere"}, ctx)
    assert not result.is_error
    assert "No definition" in result.content
