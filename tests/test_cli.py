"""CLI surface."""

from __future__ import annotations

from pathlib import Path

import pytest

from hx import __version__
from hx.cli import ParsedArgs, UsageError, main, parse_args, prompt_for_api_key


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    assert "terminal coding agent" in capsys.readouterr().out


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
