"""Rule evaluation order."""

from __future__ import annotations

from pathlib import Path

import pytest

from hx.config import PermissionMode
from hx.permissions.engine import (
    Decision,
    PermissionEngine,
    PermissionRequest,
    parse_rule,
)


def _request(command: str) -> PermissionRequest:
    return PermissionRequest(
        tool_name="Bash",
        specifier=command,
        params={"command": command},
        mutating=True,
        description=command,
    )


def test_deny_beats_allow(project: Path) -> None:
    rules = [
        parse_rule("Bash(git:*)", "test", Decision.ALLOW),
        parse_rule("Bash(git push:*)", "test", Decision.DENY),
    ]
    engine = PermissionEngine(PermissionMode.DEFAULT, rules, project)
    assert engine.evaluate(_request("git push origin main")).decision is Decision.DENY


def test_deny_survives_bypass_mode(project: Path) -> None:
    """Bypass mode approves what is unspecified, never what is explicitly denied."""
    rules = [parse_rule("Bash(rm:*)", "test", Decision.DENY)]
    engine = PermissionEngine(PermissionMode.BYPASS, rules, project)
    assert engine.evaluate(_request("rm -rf /")).decision is Decision.DENY


def test_allowed_prefix_does_not_cover_a_chained_command(project: Path) -> None:
    rules = [parse_rule("Bash(git status:*)", "test", Decision.ALLOW)]
    engine = PermissionEngine(PermissionMode.DEFAULT, rules, project)
    assert engine.evaluate(_request("git status && rm -rf /")).decision is not Decision.ALLOW


def test_plan_mode_hides_mutating_tools(project: Path) -> None:
    engine = PermissionEngine(PermissionMode.PLAN, [], project)
    assert "Bash" not in engine.allowed_tools(["Read", "Bash", "Edit", "Grep"])


def test_unparseable_command_asks_rather_than_allows(project: Path) -> None:
    """A command we cannot decompose must never fall through to an allow rule."""
    rules = [parse_rule("Bash(echo:*)", "test", Decision.ALLOW)]
    engine = PermissionEngine(PermissionMode.DEFAULT, rules, project)
    assert engine.evaluate(_request('eval "$(printf rm)" -rf /')).decision is Decision.ASK


def test_substituted_command_is_checked_too(project: Path) -> None:
    rules = [parse_rule("Bash(rm:*)", "test", Decision.DENY)]
    engine = PermissionEngine(PermissionMode.DEFAULT, rules, project)
    assert engine.evaluate(_request("echo $(rm -rf /tmp/x)")).decision is Decision.DENY


def test_read_only_commands_do_not_prompt(project: Path) -> None:
    engine = PermissionEngine(PermissionMode.DEFAULT, [], project)
    assert engine.evaluate(_request("ls -la")).decision is Decision.ALLOW
    assert engine.evaluate(_request("git status")).decision is Decision.ALLOW


def test_a_read_only_command_that_redirects_still_asks(project: Path) -> None:
    """`cat` is harmless; `cat > /etc/hosts` is not."""
    engine = PermissionEngine(PermissionMode.DEFAULT, [], project)
    assert engine.evaluate(_request("cat a > b")).decision is Decision.ASK


def test_path_rules_match_globs_across_directories(project: Path) -> None:
    rules = [parse_rule("Edit(src/**)", "test", Decision.ALLOW)]
    engine = PermissionEngine(PermissionMode.DEFAULT, rules, project)

    def edit(path: str) -> PermissionRequest:
        return PermissionRequest("Edit", path, {"file_path": path}, True, path)

    assert engine.evaluate(edit("src/a/b/c.py")).decision is Decision.ALLOW
    assert engine.evaluate(edit("other.py")).decision is Decision.ASK


def test_deny_rules_protect_credential_paths(project: Path) -> None:
    rules = [parse_rule("Read(**/.ssh/**)", "test", Decision.DENY)]
    engine = PermissionEngine(PermissionMode.PLAN, rules, project)
    request = PermissionRequest("Read", ".ssh/id_rsa", {}, False, "read key")
    assert engine.evaluate(request).decision is Decision.DENY


async def test_ask_without_an_asker_denies_with_an_explanation(project: Path) -> None:
    """Headless runs have no way to prompt, so ASK can only mean no - but the
    model is told why instead of seeing a silent failure."""
    engine = PermissionEngine(PermissionMode.DEFAULT, [], project)
    allowed, reason = await engine.request(_request("rm -rf build"))
    assert not allowed
    assert "non-interactive" in reason


async def test_session_grant_survives_to_the_next_call(project: Path) -> None:
    from hx.permissions.engine import GrantScope, PermissionAnswer

    calls: list[str] = []

    async def asker(request: PermissionRequest) -> PermissionAnswer:
        calls.append(request.description)
        return PermissionAnswer(True, GrantScope.SESSION)

    engine = PermissionEngine(PermissionMode.DEFAULT, [], project, asker=asker)
    assert (await engine.request(_request("rm -rf build")))[0]
    assert (await engine.request(_request("rm -rf build")))[0]
    assert len(calls) == 1


async def test_always_allow_persists_a_generalised_rule(project: Path) -> None:
    """An exact-argument rule would be useless on the next invocation."""
    import json

    from hx.permissions.engine import GrantScope, PermissionAnswer

    async def asker(request: PermissionRequest) -> PermissionAnswer:
        return PermissionAnswer(True, GrantScope.ALWAYS)

    engine = PermissionEngine(PermissionMode.DEFAULT, [], project, asker=asker)
    await engine.request(_request("git commit -m 'first'"))

    saved = json.loads((project / ".hx" / "settings.json").read_text())
    assert saved["permissions"]["allow"] == ["Bash(git commit:*)"]
    assert engine.evaluate(_request("git commit -m 'second'")).decision is Decision.ALLOW


def test_malformed_rules_are_rejected_loudly() -> None:
    from hx.permissions.engine import InvalidRule

    with pytest.raises(InvalidRule):
        parse_rule("Bash(unclosed", "test", Decision.ALLOW)
