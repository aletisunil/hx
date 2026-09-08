"""Rule evaluation order."""

from __future__ import annotations

from pathlib import Path

from hx.config import PermissionMode
from hx.permissions.engine import (
    Decision,
    PermissionEngine,
    PermissionRequest,
    parse_rule,
)
from tests.conftest import unimplemented


def _request(command: str) -> PermissionRequest:
    return PermissionRequest(
        tool_name="Bash",
        specifier=command,
        params={"command": command},
        mutating=True,
        description=command,
    )


@unimplemented
def test_deny_beats_allow(project: Path) -> None:
    rules = [
        parse_rule("Bash(git:*)", "test", Decision.ALLOW),
        parse_rule("Bash(git push:*)", "test", Decision.DENY),
    ]
    engine = PermissionEngine(PermissionMode.DEFAULT, rules, project)
    assert engine.evaluate(_request("git push origin main")).decision is Decision.DENY


@unimplemented
def test_deny_survives_bypass_mode(project: Path) -> None:
    """Bypass mode approves what is unspecified, never what is explicitly denied."""
    rules = [parse_rule("Bash(rm:*)", "test", Decision.DENY)]
    engine = PermissionEngine(PermissionMode.BYPASS, rules, project)
    assert engine.evaluate(_request("rm -rf /")).decision is Decision.DENY


@unimplemented
def test_allowed_prefix_does_not_cover_a_chained_command(project: Path) -> None:
    rules = [parse_rule("Bash(git status:*)", "test", Decision.ALLOW)]
    engine = PermissionEngine(PermissionMode.DEFAULT, rules, project)
    assert engine.evaluate(_request("git status && rm -rf /")).decision is not Decision.ALLOW


@unimplemented
def test_plan_mode_hides_mutating_tools(project: Path) -> None:
    engine = PermissionEngine(PermissionMode.PLAN, [], project)
    assert "Bash" not in engine.allowed_tools(["Read", "Bash", "Edit", "Grep"])
