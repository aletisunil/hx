"""CLI surface."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from hx import __version__
from hx.cli import ParsedArgs, UsageError, main, parse_args, prompt_for_api_key


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    assert "an agent harness for the terminal" in capsys.readouterr().out


def test_print_mode_takes_a_prompt() -> None:
    parsed = parse_args(["-p", "do the thing"])
    assert parsed.command == "print"
    assert parsed.prompt == "do the thing"


def test_overrides_are_collected_into_settings_layers() -> None:
    parsed = parse_args(["--model", "openai/gpt-5", "--mode", "plan", "--no-sandbox"])
    assert parsed.overrides == {
        "models": {"model": "openai/gpt-5"},
        "permissions": {"mode": "plan", "sandbox": False},
    }


def test_resume_takes_an_optional_session_id() -> None:
    assert parse_args(["resume"]).session_id is None
    assert parse_args(["resume", "abc123"]).session_id == "abc123"


@pytest.mark.parametrize("args", [["--model"], ["--bogus"], ["nonsense"], ["-p"]])
def test_bad_invocations_are_usage_errors(args: list[str]) -> None:
    with pytest.raises(UsageError):
        parse_args(args)


def test_bad_flag_exits_with_usage_code(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--bogus"]) == 2
    assert "unknown option" in capsys.readouterr().err


def test_onboarding_saves_the_key_without_echoing_it(
    hx_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "sk-secret")

    assert prompt_for_api_key() is True

    auth = hx_home / "auth.json"
    assert auth.stat().st_mode & 0o777 == 0o600
    assert "sk-secret" in auth.read_text()
    assert "sk-secret" not in capsys.readouterr().out


def test_onboarding_is_skipped_when_not_interactive(
    hx_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A piped invocation must fail with a message, not block on a hidden prompt."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert prompt_for_api_key() is False
    assert not (hx_home / "auth.json").exists()


def test_parsed_args_defaults_to_the_tui() -> None:
    assert parse_args([]) == ParsedArgs(command="tui")


def test_auth_is_a_recognised_command() -> None:
    assert parse_args(["auth"]).command == "auth"
    assert parse_args(["auth", "set"]).rest == ("set",)


def test_auth_status_reports_no_key(hx_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from hx.cli import run_auth_command

    assert run_auth_command([]) == 1
    assert "hx auth set" in capsys.readouterr().out


def test_auth_status_names_the_source_and_masks_the_key(
    hx_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hx.cli import run_auth_command

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-secretmaterial9999")
    assert run_auth_command([]) == 0

    out = capsys.readouterr().out
    assert "sk-or-…9999" in out
    assert "secretmaterial" not in out, "the key must never be printed in full"
    assert "OPENROUTER_API_KEY" in out
    assert "overrides" in out


def test_auth_clear_removes_the_saved_key(
    hx_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hx.cli import run_auth_command
    from hx.providers.openrouter import MissingAPIKey, load_api_key, save_api_key

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("HX_OPENROUTER_API_KEY", raising=False)

    save_api_key("sk-or-v1-tobecleared0000")
    assert load_api_key() == "sk-or-v1-tobecleared0000"

    assert run_auth_command(["clear"]) == 0
    assert (hx_home / "auth.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(MissingAPIKey):
        load_api_key()


def test_auth_rejects_an_unknown_subcommand(
    hx_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from hx.cli import run_auth_command

    assert run_auth_command(["nonsense"]) == 2
    assert "hx auth" in capsys.readouterr().err


def test_system_prompt_flags_land_in_the_settings_layer() -> None:
    parsed = parse_args(
        [
            "--system-prompt",
            "be terse",
            "--append-system-prompt",
            "one",
            "--append-system-prompt",
            "two",
        ]
    )
    assert parsed.overrides == {"prompt": {"system": "be terse", "append": ["one", "two"]}}


def test_a_prompt_flag_reads_an_at_path(tmp_path: Path) -> None:
    target = tmp_path / "team.md"
    target.write_text("You are TESTBOT.")

    parsed = parse_args(["--system-prompt", f"@{target}"])

    assert parsed.overrides is not None
    assert parsed.overrides["prompt"]["system"] == "You are TESTBOT."


def test_a_missing_prompt_file_is_a_usage_error(tmp_path: Path) -> None:
    """Falling back to the built-in prompt silently is the worse failure."""
    with pytest.raises(UsageError):
        parse_args(["--system-prompt", f"@{tmp_path / 'nope.md'}"])


def test_hx_prompt_prints_the_prompt_and_its_source(
    hx_home: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / ".hx" / "system-prompt.md").write_text("You are TESTBOT.")

    assert main(["prompt", "--cwd", str(project)]) == 0

    captured = capsys.readouterr()
    assert captured.out.strip() == "You are TESTBOT."
    assert "system-prompt.md" in captured.err


def test_hx_prompt_reports_appended_blocks(
    hx_home: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["prompt", "--cwd", str(project), "--append-system-prompt", "be French"]) == 0

    captured = capsys.readouterr()
    assert captured.out.rstrip().endswith("be French")
    assert "[source] built-in" in captured.err
    assert "[append] --append-system-prompt" in captured.err


async def test_closing_the_session_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rename runs while the user waits for their shell prompt back, so a
    provider that has stopped answering must not hold the process open."""
    import asyncio
    import time

    from hx import cli

    class Slow:
        async def retitle_session(self) -> None:
            await asyncio.sleep(30)

    monkeypatch.setattr(cli, "RENAME_TIMEOUT_SECONDS", 0.05)
    started = time.monotonic()
    await cli._rename_closed_session(Slow())

    assert time.monotonic() - started < 1.0, "the rename was not bounded"


async def test_a_rename_that_raises_never_reaches_the_user() -> None:
    from hx.cli import _rename_closed_session

    class Broken:
        async def retitle_session(self) -> None:
            raise RuntimeError("no provider")

    await _rename_closed_session(Broken())


def test_images_go_with_a_headless_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    parsed = parse_args(["-p", "why?", "--image", "a.png", "--image", "~/b.png"])
    assert parsed.images == (tmp_path / "a.png", Path("~/b.png").expanduser())


def test_images_without_a_headless_prompt_are_refused() -> None:
    with pytest.raises(UsageError, match="--image goes with -p"):
        parse_args(["--image", "a.png"])
    with pytest.raises(UsageError, match="requires a path"):
        parse_args(["-p", "x", "--image"])


def test_an_unreadable_image_fails_before_any_model_call(
    hx_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "fake.png").write_text("not an image")
    assert main(["-p", "why?", "--image", str(tmp_path / "fake.png")]) == 2
    assert "not a readable image" in capsys.readouterr().err


async def test_a_refreshed_catalogue_reaches_the_running_loop() -> None:
    """The loop resolved its model from the cache the refresh replaced; left
    alone it decides image support and the context gauge from stale figures."""
    from types import SimpleNamespace

    from hx.cli import Runtime
    from hx.providers.models import ModelInfo, ModelPricing

    fresh = ModelInfo("m", "M", 1000, 100, ModelPricing(), supports_images=True)

    class Models:
        is_stale = True

        async def refresh(self, _auth: object) -> None:
            self.is_stale = False

        def get_or_default(self, model_id: str) -> ModelInfo:
            return fresh

        def subscription_errors(self) -> list[tuple[str, str]]:
            return []

    loop = SimpleNamespace(model="m", model_info=None)
    runtime = SimpleNamespace(models=Models(), auth=None, notices=[], loop=loop)
    await Runtime.refresh_models_if_stale(runtime)  # type: ignore[arg-type]
    assert loop.model_info is fresh


@pytest.mark.parametrize(
    "args",
    [
        ["mcp", "add", "jira", "--url", "https://mcp.example/mcp", "--header", "A: b"],
        ["mcp", "add", "fs", "npx", "-y", "@modelcontextprotocol/server-filesystem", "--help"],
        ["mcp", "add", "--user", "local", "python", "server.py"],
    ],
)
def test_mcp_arguments_reach_hx_mcp_as_typed(args: list[str]) -> None:
    """`--url`, `--user` and a server's own `-y` were refused as unknown run
    options before `hx mcp` ever saw them - the documented `add --url` form
    could not be run at all."""
    parsed = parse_args(args)
    assert parsed.command == "mcp"
    assert parsed.rest == tuple(args[1:])


def test_a_server_argument_named_help_is_not_hxs_help(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(project)
    assert main(["mcp", "add", "fs", "npx", "-y", "server", "--help"]) == 0
    assert "Added fs" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["mcp", "auth"])
def test_subcommand_help_is_its_own_usage(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([command, "--help"]) == 0
    assert f"hx {command} " in capsys.readouterr().out


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], ["resume", "NEW"]),
        (["--mode", "bypass"], ["--mode", "bypass", "resume", "NEW"]),
        (["resume", "OLD", "--model", "x/y"], ["--model", "x/y", "resume", "NEW"]),
        (["--cwd", "/somewhere", "resume"], ["resume", "NEW"]),
        (["resume", "--mode", "plan"], ["--mode", "plan", "resume", "NEW"]),
    ],
)
def test_a_relaunch_keeps_the_run_options_and_swaps_the_session(
    argv: list[str], expected: list[str]
) -> None:
    from hx.cli import relaunch_argv

    assert relaunch_argv(argv, "NEW") == expected
    assert parse_args(expected).session_id == "NEW"


def test_every_option_a_relaunch_treats_as_taking_a_value_does(tmp_path: Path) -> None:
    from hx.cli import RUN_OPTIONS_WITH_VALUES

    for option in RUN_OPTIONS_WITH_VALUES - {"--image"}:
        value = str(tmp_path) if option == "--cwd" else "plan" if option == "--mode" else "x"
        with pytest.raises(UsageError):
            parse_args([option])
        parse_args([option, value])


def test_hx_resume_runs_in_the_directory_the_session_was_recorded_in(
    hx_home: Path, tmp_path: Path
) -> None:
    from hx.cli import _adopt_session_directory

    recorded = tmp_path / "project"
    recorded.mkdir()
    session = new_session_with_a_prompt(recorded)

    parsed = parse_args(["resume", session])
    assert _adopt_session_directory(parsed, session) is None
    assert parsed.cwd == recorded.resolve()

    explicit = parse_args(["--cwd", str(tmp_path), "resume", session])
    _adopt_session_directory(explicit, session)
    assert explicit.cwd == tmp_path


def test_hx_resume_says_so_when_the_recorded_directory_is_gone(
    hx_home: Path, tmp_path: Path
) -> None:
    from hx.cli import _adopt_session_directory

    recorded = tmp_path / "gone"
    recorded.mkdir()
    session = new_session_with_a_prompt(recorded)
    recorded.rmdir()

    parsed = parse_args(["resume", session])
    notice = _adopt_session_directory(parsed, session)
    assert parsed.cwd is None
    assert notice is not None and "no longer exists" in notice


def new_session_with_a_prompt(cwd: Path) -> str:
    from hx.core.messages import user_message
    from hx.core.session import new_session

    session = new_session(cwd, "m")
    session.append(user_message("hi"))
    return session.meta.session_id


def test_a_relaunch_runs_from_where_hx_was_started(
    hx_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HX started by a relative path, with a prompt file named relative to the
    directory it was typed in. Relaunched into a session recorded elsewhere,
    both still have to resolve - the process used to change directory first.

    The command handed to ``exec`` is run for real, as ``hx prompt`` in place
    of the TUI, from wherever ``exec`` would have run it.
    """
    import subprocess

    from hx import cli

    started = tmp_path / "started"
    (started / "bin").mkdir(parents=True)
    (started / "bin" / "hx").write_text("import sys\nfrom hx.cli import main\nsys.exit(main())\n")
    (started / "p.md").write_text("THE PROMPT FROM p.md")
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    session = new_session_with_a_prompt(recorded)

    argv = ["./bin/hx", "--system-prompt", "@p.md"]
    monkeypatch.chdir(started)
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys, "orig_argv", [sys.executable, *argv])
    execs: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(os, "execv", lambda path, args: execs.append((os.getcwd(), args)))

    cli._relaunch(session)

    [(where, command)] = execs
    assert command[-2:] == ["resume", session]
    ran = subprocess.run(
        [*command[:-2], "prompt"], cwd=where, capture_output=True, text=True, timeout=60
    )
    assert ran.returncode == 0, ran.stderr
    assert "THE PROMPT FROM p.md" in ran.stdout
