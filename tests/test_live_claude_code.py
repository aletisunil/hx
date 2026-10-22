"""Live end-to-end against the real Claude Code CLI, on a Claude subscription.

Deselected by default (``-m live``). Needs ``claude`` installed and signed in
to a Claude Pro or Max account::

    hx auth login claude-code
    uv run pytest -m live tests/test_live_claude_code.py -s

This is the only place the stream-json control protocol, the MCP server served
over it, held tool calls and rebuilt sessions are proven against the real CLI.
The model is Haiku unless ``HX_LIVE_CLAUDE_MODEL`` says otherwise: these runs
spend the account's own usage.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from hx.auth.resolve import AuthResolver
from hx.config import PermissionMode, load_settings
from hx.core.context import ContextBuilder, load_system_prompt
from hx.core.events import EventBus
from hx.core.lateinject import InjectionRegistry
from hx.core.loop import AgentLoop
from hx.core.session import new_session
from hx.permissions.engine import Decision, PermissionEngine, parse_rule
from hx.providers import claude_code_catalogue, registry
from hx.providers.claude_code import ClaudeCodeProvider
from hx.providers.claude_code_transcript import project_dir
from hx.tools.registry import build_default_registry
from tests.tls_proxy import InterceptingProxy

pytestmark = pytest.mark.live

MODEL = os.environ.get("HX_LIVE_CLAUDE_MODEL", "claude-code/claude-haiku-4-5")


def _resolver() -> AuthResolver:
    resolver = AuthResolver()
    if not resolver.has_credential("claude-code"):
        pytest.skip("Claude Code not signed in; run `hx auth login claude-code`")
    return resolver


def _loop(tmp_path: Path, provider: ClaudeCodeProvider, session=None) -> AgentLoop:  # type: ignore[no-untyped-def]
    session = session or new_session(tmp_path, MODEL)
    return AgentLoop(
        provider=provider,
        session=session,
        tools=build_default_registry(None, None),
        permissions=PermissionEngine(
            PermissionMode.DEFAULT,
            [
                parse_rule("Read(**)", "live", Decision.ALLOW),
                parse_rule("Glob(**)", "live", Decision.ALLOW),
            ],
            tmp_path,
        ),
        context=ContextBuilder(load_system_prompt(tmp_path), tmp_path),
        compactor=None,
        injections=InjectionRegistry(),
        bus=EventBus(),
        settings=load_settings(tmp_path),
        model_info=None,
    )


def _provider(tmp_path: Path) -> ClaudeCodeProvider:
    _resolver()
    provider = registry.build_provider(MODEL, AuthResolver())
    assert isinstance(provider, ClaudeCodeProvider)
    provider.cwd = str(tmp_path)
    return provider


async def test_the_account_lists_its_models(hx_home: Path) -> None:
    _resolver()
    models = await claude_code_catalogue.fetch_models()
    ids = {model.id for model in models}
    assert ids, "Claude Code listed no models"
    assert all(m.provider_id == "claude-code" and m.is_subscription for m in models)
    assert not any(i.endswith("/default") for i in ids)
    print("\n".join(f"{m.id}  {m.name}  {m.reasoning_levels}" for m in models))


async def test_a_real_subscription_turn_streams(hx_home: Path, tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    loop = _loop(tmp_path, provider)
    try:
        result = await loop.run("Reply with exactly the word: pong")
    finally:
        await provider.aclose()

    assert result.error is None, result.error
    assert "pong" in loop.session.messages[-1].text().lower()
    assert loop.session.usage.total_output > 0
    assert loop.session.usage.total_cost_usd == 0


async def test_tools_run_in_hx_and_one_process_serves_the_turn(
    hx_home: Path, tmp_path: Path
) -> None:
    """Several calls, some in parallel: each is held, run by HX, answered - one process."""
    (tmp_path / "a.txt").write_text("alpha=17\n")
    (tmp_path / "b.txt").write_text("beta=25\n")
    provider = _provider(tmp_path)
    loop = _loop(tmp_path, provider)
    try:
        result = await loop.run(
            "Read a.txt and b.txt (in parallel, in one turn), then list the .txt files "
            "with the Glob tool, then reply with alpha+beta as a single number."
        )
    finally:
        spawned = provider.spawned
        await provider.aclose()

    assert result.error is None, result.error
    calls = [c for m in loop.session.messages for c in m.tool_uses()]
    assert {c.name for c in calls} >= {"Read", "Glob"}
    assert "42" in loop.session.messages[-1].text()
    # Every tool call and every model turn after it went through one CLI; the
    # other process is the one-off that named the session.
    assert spawned == 2


async def test_a_second_prompt_reuses_the_process(hx_home: Path, tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    loop = _loop(tmp_path, provider)
    try:
        await loop.run("Remember the codeword: marmalade. Reply with just: ok")
        result = await loop.run("What was the codeword? Reply with just the word.")
        spawned = provider.spawned
    finally:
        await provider.aclose()

    assert result.error is None, result.error
    assert "marmalade" in loop.session.messages[-1].text().lower()
    # Both prompts on one CLI, plus the one-off that named the session.
    assert spawned == 2


async def test_a_resumed_session_is_rebuilt(hx_home: Path, tmp_path: Path) -> None:
    """A new provider - HX restarted - rebuilds the CLI's session from HX's transcript,
    tool calls and thinking included, and the model still knows what happened."""
    (tmp_path / "secret.txt").write_text("the vault code is 7319\n")
    first = _provider(tmp_path)
    loop = _loop(tmp_path, first)
    try:
        await loop.run("Read secret.txt and reply with just: read")
    finally:
        await first.aclose()

    second = _provider(tmp_path)
    resumed = _loop(tmp_path, second, session=loop.session)
    try:
        result = await resumed.run("What was the vault code? Reply with just the number.")
    finally:
        await second.aclose()

    assert result.error is None, result.error
    assert "7319" in resumed.session.messages[-1].text()
    # The rebuilt session file was Claude Code's to read once, not to keep:
    # nothing of HX's is left in its session list for this directory.
    folder = project_dir(str(tmp_path))
    assert not list(folder.glob("*.jsonl")) if folder.exists() else True


async def test_cancelling_a_turn_stops_the_process(hx_home: Path, tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    loop = _loop(tmp_path, provider)
    task = asyncio.ensure_future(loop.run("Count from 1 to 400, one number per line."))
    for _ in range(200):
        await asyncio.sleep(0.1)
        if any(m.role == "assistant" for m in loop.session.messages) or provider.spawned:
            break
    await asyncio.sleep(3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(not c.cli.alive for c in provider._pool)
    try:
        result = await loop.run("Reply with exactly: back")
    finally:
        await provider.aclose()
    assert result.error is None, result.error
    assert "back" in loop.session.messages[-1].text().lower()


async def test_a_call_held_for_minutes_is_still_answered(hx_home: Path, tmp_path: Path) -> None:
    """The user may take minutes over an approval. Claude Code must still be holding the call
    when HX answers it - no timeout inside the CLI - and carry on on the same process."""
    from hx.core.context import AssembledContext, PromptSection
    from hx.core.messages import (
        ToolResultBlock,
        assistant_message,
        tool_result_message,
        user_message,
    )
    from hx.providers.base import ProviderRequest, StreamDelta, StreamEnd

    provider = _provider(tmp_path)
    tool = {
        "name": "Lookup",
        "description": "Look up the secret word for a key.",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    }

    def request(messages: list) -> ProviderRequest:  # type: ignore[type-arg]
        context = AssembledContext(
            system=[PromptSection("system", "")], messages=messages, tools=[tool]
        )
        return ProviderRequest(context=context, model=MODEL, max_tokens=1024)

    async def turn(messages: list) -> tuple[str, list[StreamDelta], StreamEnd]:  # type: ignore[type-arg]
        text, calls, end = "", [], None
        async for item in provider.astream(request(messages)):
            if isinstance(item, StreamEnd):
                end = item
            elif item.tool_use_id:
                calls.append(item)
            elif item.text:
                text += item.text
        assert end is not None
        return text, calls, end

    from hx.core.messages import ToolUseBlock

    messages = [user_message("Use the Lookup tool with key 'door', then reply with just the word.")]
    try:
        _, calls, end = await turn(messages)
        assert end.stop_reason == "tool_use" and calls
        call = calls[0]
        messages.append(
            assistant_message(
                [ToolUseBlock(id=call.tool_use_id, name=call.tool_name, input={"key": "door"})]
            )
        )
        await asyncio.sleep(150)
        messages.append(
            tool_result_message([ToolResultBlock(tool_use_id=call.tool_use_id, content="saffron")])
        )
        text, _, end = await turn(messages)
    finally:
        spawned = provider.spawned
        await provider.aclose()

    assert end.stop_reason == "end_turn"
    assert "saffron" in text.lower()
    assert spawned == 1


# -- a network that intercepts TLS ------------------------------------------------------
#
# The CLI makes its own connections with its own TLS stack, so HX's trust settings
# have to be restated for it. Each test puts a TLS-intercepting proxy with a CA no
# machine trusts (tests/tls_proxy.py) between the real CLI and the API.


@pytest.fixture()
def mitm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[InterceptingProxy]:
    with InterceptingProxy(tmp_path / "mitm") as proxy:
        for name in ("HTTPS_PROXY", "https_proxy"):
            monkeypatch.setenv(name, proxy.url)
        for name in (
            "HX_CA_BUNDLE",
            "SSL_CERT_FILE",
            "REQUESTS_CA_BUNDLE",
            "HX_SSL_NO_VERIFY",
            "NODE_EXTRA_CA_CERTS",
            "NODE_TLS_REJECT_UNAUTHORIZED",
            "CLAUDE_CODE_CERT_STORE",
            "CLAUDE_CODE_CLIENT_CERT",
            "CLAUDE_CODE_CLIENT_KEY",
            "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE",
        ):
            monkeypatch.delenv(name, raising=False)
        yield proxy


async def _tool_turn(tmp_path: Path) -> tuple[str | None, str]:
    """A turn that reads a file and answers from it: the error, and the reply."""
    (tmp_path / "vault.txt").write_text("combination=4417\n")
    provider = _provider(tmp_path)
    loop = _loop(tmp_path, provider)
    try:
        result = await loop.run(
            "Read vault.txt with the Read tool, then reply with just the combination."
        )
    finally:
        await provider.aclose()
    return result.error, loop.session.messages[-1].text()


async def test_an_untrusted_interception_ca_names_the_fix(
    hx_home: Path, tmp_path: Path, mitm: InterceptingProxy
) -> None:
    error, _ = await _tool_turn(tmp_path)
    assert error is not None
    assert "certificate" in error.lower()
    assert "set HX_CA_BUNDLE to its PEM file" in error
    assert "api.anthropic.com" in mitm.rejected
    assert "api.anthropic.com" not in mitm.intercepted


async def test_hx_ca_bundle_is_trusted_by_the_cli(
    hx_home: Path, tmp_path: Path, mitm: InterceptingProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HX_CA_BUNDLE", str(mitm.ca_file))
    error, reply = await _tool_turn(tmp_path)
    assert error is None, error
    assert "4417" in reply
    assert "api.anthropic.com" in mitm.intercepted
    assert "api.anthropic.com" not in mitm.rejected


async def test_hx_ssl_no_verify_reaches_the_cli(
    hx_home: Path, tmp_path: Path, mitm: InterceptingProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HX_SSL_NO_VERIFY", "1")
    error, reply = await _tool_turn(tmp_path)
    assert error is None, error
    assert "4417" in reply
    assert "api.anthropic.com" in mitm.intercepted


async def test_a_device_certificate_reaches_the_cli(
    hx_home: Path, tmp_path: Path, mitm: InterceptingProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zero-trust network that admits only machines presenting its device certificate,
    configured the way Claude Code documents it."""
    mitm.require_client_cert = True
    cert, key = mitm.client_cert()
    monkeypatch.setenv("HX_CA_BUNDLE", str(mitm.ca_file))
    monkeypatch.setenv("CLAUDE_CODE_CLIENT_CERT", str(cert))
    monkeypatch.setenv("CLAUDE_CODE_CLIENT_KEY", str(key))
    error, reply = await _tool_turn(tmp_path)
    assert error is None, error
    assert "4417" in reply
    assert "api.anthropic.com" in mitm.intercepted
    assert "api.anthropic.com" not in mitm.rejected


WRONG_PURPOSE_HINT = "Your network intercepts TLS with a certificate that is not valid for servers."


async def test_an_interception_ca_of_the_wrong_purpose_names_the_fix(
    hx_home: Path, tmp_path: Path, mitm: InterceptingProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trusted, yet refused: the proxy signs through a CA that may not vouch for servers.
    Adding the CA again changes nothing, so the hint must not say to."""
    mitm.wrong_purpose = True
    monkeypatch.setenv("HX_CA_BUNDLE", str(mitm.ca_file))
    error, _ = await _tool_turn(tmp_path)
    assert error is not None
    assert "INVALID_PURPOSE" in error
    assert WRONG_PURPOSE_HINT in error
    assert "holds the CA" not in error
    assert "api.anthropic.com" in mitm.rejected
    assert "api.anthropic.com" not in mitm.intercepted


class _PastedCode:
    """The sign-in dialog, with a code that gets as far as the token exchange: the
    state is the one the CLI asked for, the code itself is made up."""

    def __init__(self) -> None:
        self.url = ""

    def show_url(self, url: str, message: str) -> None:
        self.url = url

    async def prompt_paste(self, prompt: str) -> str:
        state = re.search(r"state=([^&\s]+)", self.url)
        assert state is not None, self.url
        return f"made-up-code#{state.group(1)}"


@pytest.mark.parametrize("wrong_purpose", [True, False], ids=["wrong-purpose", "control"])
async def test_signing_in_behind_an_interception_ca_of_the_wrong_purpose(
    tmp_path: Path, mitm: InterceptingProxy, monkeypatch: pytest.MonkeyPatch, wrong_purpose: bool
) -> None:
    """/login through such a network, as a user on Claude Code 2.1.289 met it: the code is
    pasted and the token exchange is refused. The failure says why and what to do, without
    the sign-in URL. The control - the same proxy, signing as it should - reaches the server,
    which turns the made-up code away. Claude Code's state goes to a directory of the test's
    own, so the machine's login is never touched; no browser opens."""
    from hx.auth import claude_code as cc_auth

    if cc_auth.find_cli() is None:
        pytest.skip("Claude Code is not installed")
    mitm.wrong_purpose = wrong_purpose
    monkeypatch.setenv("HX_CA_BUNDLE", str(mitm.ca_file))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    no_browser = tmp_path / "bin"
    no_browser.mkdir()
    for opener in ("open", "xdg-open"):
        (no_browser / opener).write_text("#!/bin/sh\nexit 0\n")
        (no_browser / opener).chmod(0o755)
    monkeypatch.setenv("PATH", f"{no_browser}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(cc_auth.LoginFailed) as failed:
        await cc_auth.login_interactive(_PastedCode())
    message = str(failed.value)
    assert "https://" not in message
    assert "Paste code" not in message
    if wrong_purpose:
        assert message.startswith(
            "`claude auth login` failed: unsupported certificate purpose. " + WRONG_PURPOSE_HINT
        ), message
        assert "platform.claude.com" in mitm.rejected
    else:
        assert message == "`claude auth login` failed: Request failed with status code 400"
        assert "platform.claude.com" in mitm.intercepted
