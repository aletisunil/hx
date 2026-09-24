"""Signing in to a remote MCP server from inside a session.

Typed the way a user types it, against the OAuth fixture server, and read back
off the emulated screen.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from hx.auth.oauth import browser
from hx.mcp.manager import MCPManager, MCPServerConfig
from tests.mcp.fixtures.oauth_server import OAuthMCPServer
from tests.mcp.test_oauth import Browser
from tests.tui.support import Driver, build_session


@pytest.fixture()
def server() -> Iterator[OAuthMCPServer]:
    running = OAuthMCPServer().start()
    yield running
    running.stop()


@pytest.fixture()
def user(monkeypatch: pytest.MonkeyPatch) -> Browser:
    person = Browser()
    monkeypatch.setattr(browser, "open_browser", person)
    return person


async def until(driver: Driver, check: Callable[[], bool]) -> None:
    for _ in range(300):
        if check():
            return
        await driver.settle(2)
    raise AssertionError(f"never happened; screen:\n{driver.screen_text()}")


async def test_mcp_login_signs_in_and_hands_the_model_the_tools(
    hx_home: Path, tmp_path: Path, server: OAuthMCPServer, user: Browser
) -> None:
    manager = MCPManager(
        [MCPServerConfig(name="jira", transport="http", url=server.url, timeout=10.0)]
    )
    app = build_session(tmp_path, mcp=manager)
    await manager.connect_all()
    await manager.register_tools(app.loop.tools)
    try:
        async with Driver(app) as driver:
            driver.type("/mcp\r")
            await until(driver, lambda: "/mcp login jira" in driver.screen_text())
            assert "jira" in driver.screen_text()
            assert "needs sign-in" in driver.screen_text()

            driver.type("/mcp login\r")
            await until(driver, lambda: "2 tools available" in driver.screen_text())

            assert "Signed in to jira: 2 tools available." in driver.screen_text()
            names = [schema["name"] for schema in app.loop.tools.schemas()]
            assert names == ["mcp__jira__echo", "mcp__jira__unlock"]
            assert len(user.opened) == 1
    finally:
        await manager.close_all()


async def test_mcp_login_names_the_servers_when_it_cannot_guess(
    hx_home: Path, tmp_path: Path, server: OAuthMCPServer
) -> None:
    manager = MCPManager(
        [
            MCPServerConfig(name="one", transport="http", url=server.url, timeout=10.0),
            MCPServerConfig(name="two", transport="http", url=server.url, timeout=10.0),
        ]
    )
    app = build_session(tmp_path, mcp=manager)
    await manager.connect_all()
    try:
        async with Driver(app) as driver:
            driver.type("/mcp login\r")
            await until(driver, lambda: "Servers: one, two" in driver.screen_text())
    finally:
        await manager.close_all()


async def test_mcp_reconnect_reports_a_server_that_cannot_be_built(
    hx_home: Path, tmp_path: Path
) -> None:
    """A stdio entry with no command raised straight out of the command."""
    manager = MCPManager([MCPServerConfig(name="broken", transport="stdio", timeout=5.0)])
    app = build_session(tmp_path, mcp=manager)
    await manager.connect_all()
    try:
        async with Driver(app) as driver:
            driver.type("/mcp reconnect broken\r")
            await until(driver, lambda: "unavailable" in driver.screen_text())
            assert "stdio transport needs a command" in driver.screen_text()
    finally:
        await manager.close_all()
