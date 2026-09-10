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

    saved = json.loads((project / ".hx" / "settings.local.json").read_text())
    assert saved["permissions"]["allow"] == ["Bash(git commit:*)"]
    assert engine.evaluate(_request("git commit -m 'second'")).decision is Decision.ALLOW


def test_malformed_rules_are_rejected_loudly() -> None:
    from hx.permissions.engine import InvalidRule

    with pytest.raises(InvalidRule):
        parse_rule("Bash(unclosed", "test", Decision.ALLOW)


async def test_always_allow_covers_every_segment_of_a_compound_command(project: Path) -> None:
    """A compound command stored verbatim matches nothing: a specifier is
    compared against each segment on its own, so the rule is dead on arrival
    and the user is asked again on the very next call."""
    import json

    from hx.permissions.engine import GrantScope, PermissionAnswer

    async def asker(request: PermissionRequest) -> PermissionAnswer:
        return PermissionAnswer(True, GrantScope.ALWAYS)

    engine = PermissionEngine(PermissionMode.DEFAULT, [], project, asker=asker)
    await engine.request(_request("cd /repo && uv run pytest tests/ -x -q"))

    saved = json.loads((project / ".hx" / "settings.local.json").read_text())
    assert saved["permissions"]["allow"] == ["Bash(cd /repo:*)", "Bash(uv run:*)"]
    assert engine.evaluate(_request("cd /repo && uv run pytest tests/tui -q")).decision is (
        Decision.ALLOW
    )


async def test_session_grant_covers_a_sibling_invocation(project: Path) -> None:
    """ "Allow for this session" that only matched a byte-identical command
    prompted again for every changed flag."""
    from hx.permissions.engine import GrantScope, PermissionAnswer

    async def asker(request: PermissionRequest) -> PermissionAnswer:
        return PermissionAnswer(True, GrantScope.SESSION)

    engine = PermissionEngine(PermissionMode.DEFAULT, [], project, asker=asker)
    await engine.request(_request("uv run pytest tests/tui -q"))

    assert engine.evaluate(_request("uv run pytest tests/core -x")).decision is Decision.ALLOW
    assert not (project / ".hx" / "settings.local.json").exists(), "session scope must not persist"


async def test_a_grant_on_an_undecomposable_command_is_honoured(project: Path) -> None:
    """The exact string the user approved is the exact string that runs, so it
    can be allowed - what may not be guessed at is a prefix rule for it."""
    from hx.permissions.engine import GrantScope, PermissionAnswer

    calls: list[str] = []

    async def asker(request: PermissionRequest) -> PermissionAnswer:
        calls.append(request.description)
        return PermissionAnswer(True, GrantScope.SESSION)

    engine = PermissionEngine(PermissionMode.DEFAULT, [], project, asker=asker)
    command = 'eval "$(rbenv init -)"'
    assert (await engine.request(_request(command)))[0]
    assert (await engine.request(_request(command)))[0]
    assert len(calls) == 1
    assert engine.evaluate(_request('eval "$(something else)"')).decision is Decision.ASK


def test_a_whole_command_rule_matches_that_command_exactly(project: Path) -> None:
    command = "cd /repo && uv run pytest -q"
    rules = [parse_rule(f"Bash({command})", "test", Decision.ALLOW)]
    engine = PermissionEngine(PermissionMode.DEFAULT, rules, project)
    assert engine.evaluate(_request(command)).decision is Decision.ALLOW
    assert engine.evaluate(_request("cd /repo && rm -rf /")).decision is Decision.ASK


def test_read_only_commands_with_redirections_do_not_prompt(project: Path) -> None:
    engine = PermissionEngine(PermissionMode.DEFAULT, [], project)
    for command in ("ls -la 2>&1", "cat < notes.txt", "grep foo bar 2>/dev/null"):
        assert engine.evaluate(_request(command)).decision is Decision.ALLOW, command
    assert engine.evaluate(_request("cat notes.txt > copy.txt")).decision is Decision.ASK


def test_legacy_whole_command_rules_are_widened_in_place(project: Path) -> None:
    import json

    from hx.permissions.engine import migrate_legacy_rules

    path = project / ".hx" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "permissions": {
                    "allow": [
                        "Bash(cd /repo && uv run pytest tests/ -x -q)",
                        "Bash(clear:*)",
                        "Bash(git status)",
                        "Edit(src/hx/tui/commands.py)",
                    ]
                }
            }
        )
    )

    notices = migrate_legacy_rules(project)

    assert len(notices) == 1 and str(path) in notices[0]
    saved = json.loads(path.read_text())["permissions"]["allow"]
    assert saved == [
        "Bash(cd /repo:*)",
        "Bash(uv run:*)",
        "Bash(clear:*)",
        # Single-segment rules are already usable; widening them would grant
        # more than their author asked for.
        "Bash(git status)",
        "Edit(src/hx/tui/commands.py)",
    ]
    assert migrate_legacy_rules(project) == [], "migration must be idempotent"


async def test_a_grant_never_widens_past_the_command_it_was_given(project: Path) -> None:
    """A segment with no subcommand has nothing to anchor a prefix rule to.

    Widening to the bare executable reads as harmless on ``ls -la`` and is not:
    approving ``rm -rf build`` once would cover every other ``rm`` the user was
    never shown.
    """
    from hx.permissions.engine import GrantScope, PermissionAnswer

    async def asker(request: PermissionRequest) -> PermissionAnswer:
        return PermissionAnswer(True, GrantScope.SESSION)

    engine = PermissionEngine(PermissionMode.DEFAULT, [], project, asker=asker)
    await engine.request(_request("rm -rf build"))

    assert engine.evaluate(_request("rm -rf build")).decision is Decision.ALLOW
    for unapproved in ("rm -rf /Users/me/photos", "rm -rf ~", "rm -rf /"):
        assert engine.evaluate(_request(unapproved)).decision is Decision.ASK, unapproved


def test_persistable_rules_widen_only_where_a_subcommand_anchors_them() -> None:
    from hx.permissions.engine import persistable_rules

    assert persistable_rules("Bash", "git commit -m x") == ["Bash(git commit:*)"]
    assert persistable_rules("Bash", "uv run pytest tests/tui -q") == ["Bash(uv run:*)"]
    # Nothing but flags after the executable: kept exactly as approved.
    assert persistable_rules("Bash", "rm -rf build") == ["Bash(rm -rf build)"]
    # One unwidenable segment keeps the whole command exact, so the rule can
    # never grant a segment the user did not see.
    assert persistable_rules("Bash", "cd /repo && rm -rf dist") == ["Bash(cd /repo && rm -rf dist)"]


def test_migration_leaves_unwidenable_rules_alone_and_names_what_it_changed(
    project: Path,
) -> None:
    import json

    from hx.permissions.engine import migrate_legacy_rules

    path = project / ".hx" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "permissions": {
                    "allow": [
                        "Bash(cd /repo && rm -rf dist)",
                        "Bash(cd /repo && uv run pytest -x)",
                    ]
                }
            }
        )
    )

    notices = migrate_legacy_rules(project)

    saved = json.loads(path.read_text())["permissions"]["allow"]
    assert saved == [
        # Not widened: `rm -rf dist` has no subcommand to anchor a rule to.
        "Bash(cd /repo && rm -rf dist)",
        "Bash(cd /repo:*)",
        "Bash(uv run:*)",
    ]
    assert len(notices) == 1
    assert "Bash(cd /repo && uv run pytest -x) -> Bash(cd /repo:*), Bash(uv run:*)" in notices[0]
    # The rule it left alone is not reported as a rewrite that never happened.
    assert "Bash(cd /repo && rm -rf dist) ->" not in notices[0]


async def test_always_allow_leaves_the_shared_project_settings_alone(project: Path) -> None:
    """A grant is one person's decision on one machine.

    ``.hx/settings.json`` is the file a team checks in; appending grants there
    committed one developer's absolute paths to everybody who cloned the repo.
    """
    import json

    from hx.permissions.engine import GrantScope, PermissionAnswer

    shared = project / ".hx" / "settings.json"
    shared.write_text(json.dumps({"permissions": {"deny": ["Bash(curl:*)"]}}) + "\n")
    before = shared.read_text()

    async def asker(request: PermissionRequest) -> PermissionAnswer:
        return PermissionAnswer(True, GrantScope.ALWAYS)

    engine = PermissionEngine(PermissionMode.DEFAULT, [], project, asker=asker)
    await engine.request(_request("git commit -m 'first'"))

    assert shared.read_text() == before, "the shared file was rewritten"
    local = json.loads((project / ".hx" / "settings.local.json").read_text())
    assert local["permissions"]["allow"] == ["Bash(git commit:*)"]


async def test_the_local_layer_excludes_itself_from_git(project: Path) -> None:
    """``.hx/.gitignore`` is HX's own file; the project's belongs to the project."""
    from hx.permissions.engine import GrantScope, PermissionAnswer

    async def asker(request: PermissionRequest) -> PermissionAnswer:
        return PermissionAnswer(True, GrantScope.ALWAYS)

    engine = PermissionEngine(PermissionMode.DEFAULT, [], project, asker=asker)
    await engine.request(_request("git commit -m 'first'"))
    await engine.request(_request("git status"))

    ignored = (project / ".hx" / ".gitignore").read_text()
    assert ignored.split() == ["settings.local.json"], "one entry, written once"


def test_local_settings_rules_load_and_are_named_as_such(project: Path) -> None:
    """``/permissions`` has to be able to say which file a rule came from."""
    import json

    from hx.permissions.engine import load_rules

    (project / ".hx" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}) + "\n"
    )
    (project / ".hx" / "settings.local.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(git push:*)"], "deny": ["Bash(rm:*)"]}}) + "\n"
    )

    rules = load_rules(project)
    sources = {(rule.tool, rule.specifier): rule.source for rule in rules}
    assert sources[("Bash", "ls:*")] == "project settings"
    assert sources[("Bash", "git push:*")] == "local settings"
    assert sources[("Bash", "rm:*")] == "local settings"


def test_a_project_deny_still_beats_a_local_allow(project: Path) -> None:
    """The local layer is where HX writes, not a way around the project's rules."""
    import json

    from hx.permissions.engine import load_rules

    (project / ".hx" / "settings.json").write_text(
        json.dumps({"permissions": {"deny": ["Bash(curl:*)"]}}) + "\n"
    )
    (project / ".hx" / "settings.local.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(curl:*)"]}}) + "\n"
    )

    engine = PermissionEngine(PermissionMode.BYPASS, load_rules(project), project)
    assert engine.evaluate(_request("curl https://example.com")).decision is Decision.DENY


@pytest.mark.parametrize(
    "command",
    [
        "cd src && cat pyproject.toml",
        "git rev-parse --show-toplevel",
        "git ls-files 'src/**/*.py'",
        "git branch -a",
        "git stash list",
        "env",
        "test -f README.md && head -5 README.md",
        "ls -la | grep py | wc -l",
    ],
)
def test_reading_the_project_does_not_prompt(project: Path, command: str) -> None:
    """These are what an agent runs constantly while orienting itself.

    Every one of them used to ask, which is how a user learns to approve without
    reading the command.
    """
    engine = PermissionEngine(PermissionMode.DEFAULT, [], project)
    assert engine.evaluate(_request(command)).decision is Decision.ALLOW


@pytest.mark.parametrize(
    "command",
    [
        "sed -i 's/a/b/' src/hx/config.py",
        "find . -name '*.pyc' -delete",
        "sort -o names.txt names.txt",
        "git branch -D main",
        "git tag v9.9.9",
        "git config user.email someone@example.com",
        "git stash",
        "env FOO=1 rm -rf /",
    ],
)
def test_a_writing_command_wearing_a_read_only_name_still_prompts(
    project: Path, command: str
) -> None:
    """`sed`, `find` and `git branch` all read under one flag and write under
    another. The read-only relaxation must not cover the writing half."""
    engine = PermissionEngine(PermissionMode.DEFAULT, [], project)
    assert engine.evaluate(_request(command)).decision is Decision.ASK
