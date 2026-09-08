"""MCP registration, driven against a real server subprocess."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from hx.config import load_settings
from hx.mcp.manager import MCPManager, MCPServerConfig, load_configs, remove_config, save_config
from hx.tools.base import ToolContext
from hx.tools.registry import ToolRegistry

SERVER = Path(__file__).parent / "fixtures" / "echo_server.py"


def config(name: str = "echo", mode: str | None = None, timeout: float = 15.0) -> MCPServerConfig:
    args = [str(SERVER)] + ([mode] if mode else [])
    return MCPServerConfig(
        name=name, transport="stdio", command=sys.executable, args=tuple(args), timeout=timeout
    )


@pytest.fixture()
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        cwd=tmp_path,
        session_id="s",
        tool_use_id="t1",
        settings=load_settings(tmp_path),
        emit_progress=lambda _chunk: None,
    )


async def test_tools_are_namespaced_per_server(hx_home: Path) -> None:
    manager = MCPManager([config("alpha"), config("beta")])
    registry = ToolRegistry()
    try:
        statuses = await manager.connect_all()
        await manager.register_tools(registry)

        assert all(status.connected for status in statuses)
        assert registry.names() == [
            "mcp__alpha__add",
            "mcp__alpha__echo",
            "mcp__beta__add",
            "mcp__beta__echo",
        ]
    finally:
        await manager.close_all()


async def test_registration_order_is_deterministic(hx_home: Path) -> None:
    """The fixture returns its tools reversed; a server whose order varies must
    not shift the cached prefix."""
    orders = []
    for _ in range(2):
        manager = MCPManager([config("b"), config("a")])
        registry = ToolRegistry()
        try:
            await manager.connect_all()
            await manager.register_tools(registry)
            orders.append(registry.names())
        finally:
            await manager.close_all()

    assert orders[0] == orders[1]
    assert orders[0] == sorted(orders[0])


async def test_a_failing_server_does_not_abort_startup(hx_home: Path) -> None:
    manager = MCPManager([config("good"), config("broken", "--crash")])
    registry = ToolRegistry()
    try:
        statuses = {status.name: status for status in await manager.connect_all()}
        await manager.register_tools(registry)

        assert statuses["good"].connected
        assert not statuses["broken"].connected
        assert statuses["broken"].error
        assert any(name.startswith("mcp__good__") for name in registry.names())
    finally:
        await manager.close_all()


async def test_a_hanging_server_times_out_instead_of_blocking(hx_home: Path) -> None:
    """One unresponsive server must not hold the session hostage at startup."""
    manager = MCPManager([config("slow", "--hang", timeout=1.0)])
    try:
        status = (await manager.connect_all())[0]
        assert not status.connected
        assert "did not respond" in (status.error or "")
    finally:
        await manager.close_all()


async def test_non_json_on_stdout_is_tolerated(hx_home: Path) -> None:
    """A server that prints a banner is buggy, not fatal."""
    manager = MCPManager([config("chatty", "--noise")])
    try:
        assert (await manager.connect_all())[0].connected
    finally:
        await manager.close_all()


async def test_a_server_without_tools_registers_nothing(hx_home: Path) -> None:
    manager = MCPManager([config("bare", "--no-tools")])
    registry = ToolRegistry()
    try:
        status = (await manager.connect_all())[0]
        await manager.register_tools(registry)
        assert status.connected
        assert status.tool_count == 0
        assert registry.names() == []
    finally:
        await manager.close_all()


async def test_calling_a_tool_returns_its_text(hx_home: Path, ctx: ToolContext) -> None:
    manager = MCPManager([config("echo")])
    registry = ToolRegistry()
    try:
        await manager.connect_all()
        await manager.register_tools(registry)

        echoed = await registry.call("mcp__echo__echo", {"message": "round trip"}, ctx)
        assert echoed.content == "round trip"

        added = await registry.call("mcp__echo__add", {"a": 2, "b": 3}, ctx)
        assert added.content == "5.0"
    finally:
        await manager.close_all()


async def test_a_tool_error_reaches_the_model_as_an_error(hx_home: Path, ctx: ToolContext) -> None:
    manager = MCPManager([config("echo")])
    registry = ToolRegistry()
    try:
        await manager.connect_all()
        await manager.register_tools(registry)
        tool = registry.get("mcp__echo__echo")
        tool.definition.name = "missing"  # type: ignore[attr-defined]

        result = await registry.call("mcp__echo__echo", {"message": "x"}, ctx)
        assert result.is_error
    finally:
        await manager.close_all()


def test_project_config_overrides_the_user_one(project: Path, hx_home: Path) -> None:
    (hx_home / "mcp.json").write_text(
        json.dumps({"mcpServers": {"shared": {"command": "user-cmd"}}})
    )
    (project / ".hx" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"shared": {"command": "project-cmd"}}})
    )

    configs = load_configs(project)
    assert len(configs) == 1
    assert configs[0].command == "project-cmd"


def test_a_broken_config_file_is_ignored_not_fatal(project: Path) -> None:
    (project / ".hx" / "mcp.json").write_text("{not json")
    assert load_configs(project) == []


def test_add_and_remove_round_trip(project: Path) -> None:
    path = save_config(
        MCPServerConfig(name="demo", transport="stdio", command="echo", args=("hi",)), project
    )
    assert path.is_file()
    assert [c.name for c in load_configs(project)] == ["demo"]

    assert remove_config("demo", project)
    assert load_configs(project) == []
    assert not remove_config("demo", project)


def test_url_entries_default_to_the_http_transport(project: Path) -> None:
    (project / ".hx" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"remote": {"url": "https://example.com/mcp"}}})
    )
    assert load_configs(project)[0].transport == "http"
