"""Remote MCP servers behind OAuth, end to end.

Driven against a real HTTP server on a real port (``fixtures/oauth_server.py``)
with the real loopback callback. The one thing stood in for is the person at
the browser: ``open_browser`` follows the authorize redirect to the callback,
which is what clicking "Accept" does.
"""

from __future__ import annotations

import asyncio
import json
import stat
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from hx.auth.oauth import browser
from hx.auth.store import AuthStore, OAuthCredential
from hx.mcp import oauth
from hx.mcp.client import SSEDecoder
from hx.mcp.manager import MCPManager, MCPServerConfig, load_configs, save_config
from hx.mcp.oauth import OAuthOptions, parse_www_authenticate
from hx.tools.registry import ToolRegistry
from tests.mcp.fixtures.oauth_server import OAuthMCPServer


class Browser:
    """The user: opens the authorize URL and lets its redirect land."""

    def __init__(self) -> None:
        self.opened: list[str] = []
        self.accept = True

    async def __call__(self, url: str) -> bool:
        self.opened.append(url)
        if not self.accept:
            return True
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            assert response.status_code == 302, response.text
            await client.get(response.headers["location"])
        return True


class Interaction:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.progress_lines: list[str] = []

    def show_url(self, url: str, instructions: str) -> None:
        self.urls.append(url)

    def show_device_code(self, user_code: str, verification_uri: str) -> None:
        raise AssertionError("no device code for MCP")

    def progress(self, message: str) -> None:
        self.progress_lines.append(message)

    async def prompt_paste(self, message: str) -> str:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


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


def remote(server: OAuthMCPServer, **overrides: Any) -> MCPServerConfig:
    fields: dict[str, Any] = {"name": "remote", "transport": "http", "url": server.url}
    fields.update(overrides)
    return MCPServerConfig(timeout=10.0, **fields)


@pytest.fixture()
async def session(hx_home: Path, server: OAuthMCPServer) -> AsyncIterator[Callable[..., Any]]:
    """Builds managers against the fixture and closes them all afterwards."""
    managers: list[MCPManager] = []

    async def start(config: MCPServerConfig | None = None) -> tuple[MCPManager, ToolRegistry]:
        manager = MCPManager([config or remote(server)])
        registry = ToolRegistry()
        managers.append(manager)
        await manager.connect_all()
        await manager.register_tools(registry)
        return manager, registry

    yield start
    for manager in managers:
        await manager.close_all()


async def eventually(check: Callable[[], bool]) -> None:
    """Poll: what changes is registry state and a server thread's flag, neither
    of which has an asyncio event to wait on."""
    async with asyncio.timeout(5.0):
        while not check():  # noqa: ASYNC110
            await asyncio.sleep(0.02)


def stored(server: OAuthMCPServer) -> OAuthCredential:
    credential = AuthStore().read(oauth.credential_id(server.url))
    assert isinstance(credential, OAuthCredential)
    return credential


# -- the flow a user sees ---------------------------------------------------------


async def test_a_server_wanting_sign_in_is_reported_as_such(session: Any, user: Browser) -> None:
    """Not a failure, and not a browser popping open at startup either."""
    manager, registry = await session()

    [status] = manager.status()
    assert not status.connected
    assert status.needs_login
    assert registry.names() == []
    assert user.opened == []


async def test_signing_in_brings_the_tools_up_without_a_restart(
    session: Any, server: OAuthMCPServer, user: Browser, hx_home: Path
) -> None:
    manager, registry = await session()
    interaction = Interaction()

    status = await manager.login("remote", interaction)

    assert status.connected and status.tool_count == 2
    assert registry.names() == ["mcp__remote__echo", "mcp__remote__unlock"]
    echoed = await registry.get("mcp__remote__echo").client.call_tool(  # type: ignore[attr-defined]
        "echo", {"message": "hello"}
    )
    assert echoed.text == "hello"

    # The flow the server checked: a registered client, PKCE, and a token
    # minted for this resource and no other.
    [authorize] = server.authorize_requests
    assert authorize["resource"] == server.url
    assert authorize["scope"] == "read offline_access"
    assert authorize["redirect_uri"].startswith("http://127.0.0.1:")
    assert interaction.urls == user.opened

    # Stored per server, not per name, and only the owner can read it.
    credential = stored(server)
    assert credential.extra["client_id"] in server.clients
    assert credential.extra["resource"] == server.url
    assert stat.S_IMODE((hx_home / "auth.json").stat().st_mode) == 0o600


async def test_signing_in_again_reuses_the_client_registration(
    session: Any, server: OAuthMCPServer, user: Browser
) -> None:
    manager, _ = await session()
    await manager.login("remote", Interaction())
    await manager.login("remote", Interaction())

    assert server.registrations == 1
    assert len(server.authorize_requests) == 2


async def test_a_new_session_uses_the_saved_sign_in(
    session: Any, server: OAuthMCPServer, user: Browser
) -> None:
    first, _ = await session()
    await first.login("remote", Interaction())
    await first.close_all()

    second, registry = await session()
    assert second.status()[0].connected
    assert "mcp__remote__echo" in registry.names()
    assert len(user.opened) == 1


async def test_the_query_string_does_not_change_which_sign_in_is_used(
    session: Any, server: OAuthMCPServer, user: Browser
) -> None:
    """``?tools=all`` changes what a server lists, not who it is."""
    first, _ = await session()
    await first.login("remote", Interaction())

    second, _ = await session(remote(server, url=f"{server.url}?tools=all"))
    assert second.status()[0].connected


# -- keeping the token alive -------------------------------------------------------


async def test_an_expired_token_is_refreshed_before_it_is_sent(
    session: Any, server: OAuthMCPServer, user: Browser
) -> None:
    manager, _ = await session()
    await manager.login("remote", Interaction())
    await manager.close_all()
    AuthStore().save(oauth.credential_id(server.url), replace(stored(server), expires=0.0))

    again, _ = await session()

    assert again.status()[0].connected
    assert server.refreshes == 1
    assert not stored(server).expired()


async def test_a_revoked_token_is_refreshed_after_the_401(
    session: Any, server: OAuthMCPServer, user: Browser
) -> None:
    """The server's word beats the stored expiry: it can revoke at any time."""
    manager, registry = await session()
    await manager.login("remote", Interaction())
    server.revoke_access()

    result = await registry.get("mcp__remote__echo").client.call_tool(  # type: ignore[attr-defined]
        "echo", {"message": "still here"}
    )

    assert result.text == "still here"
    assert server.refreshes == 1


async def test_a_dead_refresh_token_asks_for_sign_in_again(
    session: Any, server: OAuthMCPServer, user: Browser
) -> None:
    manager, _ = await session()
    await manager.login("remote", Interaction())
    await manager.close_all()
    server.revoke_all()

    again, _ = await session()

    [status] = again.status()
    assert status.needs_login
    assert "expired or was revoked" in (status.error or "")


async def test_concurrent_requests_spend_the_refresh_token_once(
    session: Any, server: OAuthMCPServer, user: Browser
) -> None:
    """A rotated refresh token is single use; a second refresh would sign the
    user out."""
    manager, registry = await session()
    await manager.login("remote", Interaction())
    server.revoke_access()
    client = registry.get("mcp__remote__echo").client  # type: ignore[attr-defined]

    results = await asyncio.gather(
        *(client.call_tool("echo", {"message": str(n)}) for n in range(5))
    )

    assert [r.text for r in results] == ["0", "1", "2", "3", "4"]
    assert server.refreshes == 1


async def test_a_short_lived_token_is_not_refreshed_on_every_request(
    session: Any, server: OAuthMCPServer, user: Browser
) -> None:
    """A token that lives less than the refresh margin was counted as expired
    the moment it arrived, so every request spent a refresh token first."""
    server.access_ttl = 120
    manager, registry = await session()
    await manager.login("remote", Interaction())
    client = registry.get("mcp__remote__echo").client  # type: ignore[attr-defined]

    for n in range(3):
        assert (await client.call_tool("echo", {"message": str(n)})).text == str(n)

    assert server.refreshes == 0


async def test_an_expired_server_session_is_started_again(
    session: Any, server: OAuthMCPServer, user: Browser
) -> None:
    manager, registry = await session()
    await manager.login("remote", Interaction())
    server.forget_sessions()
    before = server.initializes

    result = await registry.get("mcp__remote__echo").client.call_tool(  # type: ignore[attr-defined]
        "echo", {"message": "resumed"}
    )

    assert result.text == "resumed"
    assert server.initializes == before + 1


async def test_concurrent_calls_into_an_expired_session_start_one_new_one(
    session: Any, server: OAuthMCPServer, user: Browser
) -> None:
    """Every call that found the session gone used to initialize again - the
    second of them carrying the first one's new session id, which a server
    refuses."""
    manager, registry = await session()
    await manager.login("remote", Interaction())
    server.forget_sessions()
    before = server.initializes
    client = registry.get("mcp__remote__echo").client  # type: ignore[attr-defined]

    results = await asyncio.gather(
        *(client.call_tool("echo", {"message": str(n)}) for n in range(5))
    )

    assert [r.text for r in results] == ["0", "1", "2", "3", "4"]
    assert server.initializes == before + 1
    assert server.refused_initializes == 0


# -- changing tools ------------------------------------------------------------------


async def test_a_tool_list_change_in_a_reply_swaps_the_tools_in(
    session: Any, server: OAuthMCPServer, user: Browser
) -> None:
    manager, registry = await session()
    await manager.login("remote", Interaction())

    reply = await registry.get("mcp__remote__unlock").client.call_tool(  # type: ignore[attr-defined]
        "unlock", {}
    )

    assert reply.text == "unlocked"
    await eventually(lambda: registry.has("mcp__remote__secret"))
    assert manager.status()[0].tool_count == 3


async def test_a_tool_list_change_on_the_server_stream_swaps_the_tools_in(
    session: Any, server: OAuthMCPServer, user: Browser
) -> None:
    """Announced outside any request, on the GET stream the server holds open."""
    server.get_stream = True
    manager, registry = await session()
    await manager.login("remote", Interaction())
    await asyncio.sleep(0.2)  # the stream opens after `initialized`

    server.announce({"name": "late", "description": "", "inputSchema": {"type": "object"}})

    await eventually(lambda: registry.has("mcp__remote__late"))


async def test_the_server_stream_is_opened_again_for_a_new_session(
    session: Any, server: OAuthMCPServer, user: Browser
) -> None:
    """A session that expires while idle takes its stream with it; the stream
    for the session that replaces it has to be opened, or every later
    announcement is lost."""
    server.get_stream = True
    manager, registry = await session()
    await manager.login("remote", Interaction())
    await asyncio.sleep(0.2)

    server.forget_sessions()
    # The old stream ends, and reopening it under the dead session is refused.
    async with asyncio.timeout(10.0):
        while server.stream_refusals == 0:  # noqa: ASYNC110
            await asyncio.sleep(0.05)
    echoed = await registry.get("mcp__remote__echo").client.call_tool(  # type: ignore[attr-defined]
        "echo", {"message": "back"}
    )
    assert echoed.text == "back"
    await asyncio.sleep(0.2)

    server.announce({"name": "late", "description": "", "inputSchema": {"type": "object"}})

    await eventually(lambda: registry.has("mcp__remote__late"))


async def test_a_ping_from_the_server_is_answered(
    session: Any, server: OAuthMCPServer, user: Browser
) -> None:
    server.get_stream = True
    manager, _ = await session()
    await manager.login("remote", Interaction())
    await asyncio.sleep(0.2)

    server.ping_client()

    await eventually(server.pinged.is_set)


# -- opting out and signing out --------------------------------------------------------


async def test_an_authorization_header_in_the_config_turns_oauth_off(
    session: Any, server: OAuthMCPServer, user: Browser
) -> None:
    server.static_token = "api-token"
    good, registry = await session(remote(server, headers={"Authorization": "Bearer api-token"}))
    assert good.status()[0].connected
    assert "mcp__remote__echo" in registry.names()

    bad, _ = await session(remote(server, headers={"authorization": "Bearer wrong"}))
    [status] = bad.status()
    assert not status.connected
    assert not status.needs_login
    assert user.opened == []


async def test_signing_out_forgets_the_token_and_the_tools(
    session: Any, server: OAuthMCPServer, user: Browser
) -> None:
    manager, registry = await session()
    await manager.login("remote", Interaction())

    assert await manager.logout("remote")

    assert registry.names() == []
    assert manager.status()[0].needs_login
    assert AuthStore().read(oauth.credential_id(server.url)) is None


async def test_signing_out_of_a_server_with_its_own_header_leaves_it_alone(
    session: Any, server: OAuthMCPServer, user: Browser
) -> None:
    """There is no sign-in to forget, so there is nothing to disconnect."""
    server.static_token = "api-token"
    manager, registry = await session(remote(server, headers={"Authorization": "Bearer api-token"}))

    assert not await manager.logout("remote")

    [status] = manager.status()
    assert status.connected and not status.needs_login
    assert "mcp__remote__echo" in registry.names()


async def test_signing_out_of_a_public_server_leaves_it_connected(
    session: Any, server: OAuthMCPServer, user: Browser
) -> None:
    server.open_access = True
    manager, registry = await session()
    assert manager.status()[0].connected

    assert not await manager.logout("remote")

    [status] = manager.status()
    assert status.connected and not status.needs_login
    assert "mcp__remote__echo" in registry.names()


async def test_a_server_that_needs_no_sign_in_says_so(
    session: Any, server: OAuthMCPServer, user: Browser
) -> None:
    manager, _ = await session()
    server.authorized = lambda header: True  # type: ignore[method-assign]

    with pytest.raises(oauth.SignInNotRequired):
        await manager.login("remote", Interaction())
    assert user.opened == []


async def test_an_authorization_server_claiming_another_issuer_is_refused(
    session: Any, server: OAuthMCPServer, user: Browser
) -> None:
    """RFC 8414 section 3.3: metadata naming an issuer other than the one it
    was looked up for is not to be used - the mix-up defence."""
    server.metadata_issuer = "https://attacker.example/tenant"
    manager, _ = await session()

    with pytest.raises(browser.OAuthError, match=r"attacker\.example"):
        await manager.login("remote", Interaction())
    assert user.opened == []
    assert server.registrations == 0


def test_hx_mcp_login_reports_a_registration_reply_that_is_not_json(
    project: Path,
    server: OAuthMCPServer,
    user: Browser,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An error page with a 200 on it escaped as a traceback."""
    from hx.cli import main

    server.registration_reply = b"<html>\n<body>Bad gateway</body>\n</html>"
    save_config(remote(server), project)
    monkeypatch.chdir(project)

    assert main(["mcp", "login", "remote"]) == 1

    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "not JSON" in err
    assert user.opened == []


# -- the pieces ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (
            'Bearer resource_metadata="https://mcp.atlassian.com/.well-known/'
            'oauth-protected-resource/v2/mcp"',
            "https://mcp.atlassian.com/.well-known/oauth-protected-resource/v2/mcp",
        ),
        ('bearer error="invalid_token", resource_metadata="https://a/b"', "https://a/b"),
        ('Basic realm="x", Bearer resource_metadata="https://a/b"', "https://a/b"),
        ('Bearer realm="x", Basic resource_metadata="https://evil"', None),
        ("Bearer", None),
    ],
)
def test_the_bearer_challenge_is_read(header: str, expected: str | None) -> None:
    challenge = parse_www_authenticate(header)
    assert challenge is not None
    assert challenge.resource_metadata == expected


def test_a_non_bearer_challenge_is_ignored() -> None:
    assert parse_www_authenticate('Basic realm="x"') is None
    assert parse_www_authenticate(None) is None


def test_the_scope_in_a_challenge_is_read() -> None:
    challenge = parse_www_authenticate('Bearer error="insufficient_scope", scope="a b"')
    assert challenge is not None
    assert (challenge.scope, challenge.error) == ("a b", "insufficient_scope")


async def test_discovery_refuses_a_server_naming_another_resource() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"resource": "https://elsewhere.example/mcp", "authorization_servers": ["x"]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(browser.OAuthError, match="names a different resource"):
            await oauth.discover(
                client,
                "https://mcp.example/mcp",
                oauth.Challenge(resource_metadata="https://mcp.example/.well-known/x"),
            )


async def test_discovery_without_a_challenge_url_tries_the_well_known_paths() -> None:
    """The Atlassian layout: path-scoped metadata on both servers."""
    seen: list[str] = []
    documents = {
        "https://mcp.example/.well-known/oauth-protected-resource/v2/mcp": {
            "resource": "https://mcp.example/v2/mcp",
            "authorization_servers": ["https://auth.example/tenant1"],
            "scopes_supported": ["read:me", "offline_access"],
        },
        "https://auth.example/.well-known/oauth-authorization-server/tenant1": {
            "issuer": "https://auth.example/tenant1",
            "authorization_endpoint": "https://auth.example/authorize",
            "token_endpoint": "https://auth.example/oauth/token",
            "registration_endpoint": "https://auth.example/tenant1/dcr/register",
            "code_challenge_methods_supported": ["S256"],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        document = documents.get(str(request.url))
        return httpx.Response(200, json=document) if document else httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        found = await oauth.discover(
            client, "https://mcp.example/v2/mcp?tools=all", oauth.Challenge()
        )

    assert found.resource == "https://mcp.example/v2/mcp"
    assert found.scope == "read:me offline_access"
    assert found.server.token_endpoint == "https://auth.example/oauth/token"
    assert found.server.registration_endpoint == "https://auth.example/tenant1/dcr/register"
    assert seen[0] == "https://mcp.example/.well-known/oauth-protected-resource/v2/mcp"


async def test_a_configured_scope_wins_over_the_advertised_one() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "protected-resource" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "authorization_servers": ["https://auth.example"],
                    "scopes_supported": ["everything"],
                },
            )
        return httpx.Response(
            200,
            json={
                "authorization_endpoint": "https://auth.example/a",
                "token_endpoint": "https://auth.example/t",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        found = await oauth.discover(
            client, "https://mcp.example/mcp", oauth.Challenge(), OAuthOptions(scope="read")
        )
    assert found.scope == "read"


def test_the_sse_decoder_joins_multi_line_data() -> None:
    decoder = SSEDecoder()
    lines = [": comment", "id: 7", 'data: {"a":', "data: 1}", "", "data: not json", ""]
    assert [p for line in lines if (p := decoder.feed(line))] == [{"a": 1}]
    assert decoder.last_event_id == "7"


def test_headers_and_oauth_settings_survive_a_save(project: Path) -> None:
    save_config(
        MCPServerConfig(
            name="atlassian",
            transport="http",
            url="https://mcp.atlassian.com/v2/mcp",
            headers={"X-Team": "core"},
            oauth=OAuthOptions(client_id="abc", scope="read:me", callback_port=33418),
        ),
        project,
    )

    [config] = load_configs(project)
    assert config.headers == {"X-Team": "core"}
    assert config.oauth == OAuthOptions(client_id="abc", scope="read:me", callback_port=33418)
    raw = json.loads((project / ".hx" / "mcp.json").read_text())
    assert raw["mcpServers"]["atlassian"]["oauth"] == {
        "clientId": "abc",
        "scope": "read:me",
        "callbackPort": 33418,
    }


def test_hx_mcp_add_saves_headers(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The `--header` a user passes is what `mcp.json` ends up sending."""
    from hx.cli import run_mcp_command

    monkeypatch.chdir(project)
    assert (
        run_mcp_command(
            [
                "add",
                "jira",
                "--url",
                "https://mcp.example/mcp",
                "--header",
                "Authorization: Basic eDp5",
            ]
        )
        == 0
    )

    [config] = load_configs(project)
    assert config.headers == {"Authorization": "Basic eDp5"}
    assert not config.uses_oauth


@pytest.mark.parametrize("tail", [["--header"], ["--header", "no-colon"], ["--bogus", "x"], []])
def test_hx_mcp_add_rejects_a_malformed_header(
    project: Path, monkeypatch: pytest.MonkeyPatch, tail: list[str]
) -> None:
    from hx.cli import run_mcp_command

    monkeypatch.chdir(project)
    url = [] if not tail else ["https://mcp.example/mcp"]
    assert run_mcp_command(["add", "jira", "--url", *url, *tail]) == 2
    assert load_configs(project) == []
