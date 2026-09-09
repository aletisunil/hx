"""The persistent shell and the Bash tool.

These run real subprocesses: a mocked shell would not have caught the sentinel
and interrupt bugs that matter here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hx.config import load_settings
from hx.tools.base import ToolContext, ToolError
from hx.tools.bash import BackgroundJobs, BashTool, PersistentShell


@pytest.fixture()
async def shell(tmp_path: Path):
    session = PersistentShell(tmp_path)
    await session.start()
    yield session
    await session.close()


@pytest.fixture()
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        cwd=tmp_path,
        session_id="test-session",
        tool_use_id="t1",
        settings=load_settings(tmp_path),
        emit_progress=lambda _chunk: None,
    )


async def test_runs_a_command(shell: PersistentShell) -> None:
    result = await shell.run("echo hello world")
    assert result.stdout.strip() == "hello world"
    assert result.exit_code == 0


async def test_stderr_is_merged_like_a_terminal(shell: PersistentShell) -> None:
    result = await shell.run("ls /definitely-not-here")
    assert "No such file" in result.stdout
    assert result.exit_code != 0


async def test_state_persists_between_calls(shell: PersistentShell, tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    await shell.run("cd sub; export HX_MARKER=kept")
    result = await shell.run("basename $(pwd); echo $HX_MARKER")
    assert result.stdout.split() == ["sub", "kept"]


async def test_output_cannot_spoof_the_completion_sentinel(shell: PersistentShell) -> None:
    """A fixed marker would let a command hide everything it printed after it.
    The sentinel is randomised per session precisely to stop that."""
    result = await shell.run("echo __HX_DONE_deadbeef__ 0; echo after-spoof")
    assert "after-spoof" in result.stdout


async def test_timeout_interrupts_the_command(shell: PersistentShell) -> None:
    result = await shell.run("sleep 30", timeout_seconds=1.0)
    assert result.timed_out
    assert result.exit_code == 124


async def test_shell_survives_a_timeout_with_state_intact(
    shell: PersistentShell, tmp_path: Path
) -> None:
    """Signalling the process group would kill the shell too, silently wiping
    the session's cd and exports on any slow command."""
    (tmp_path / "sub").mkdir()
    await shell.run("cd sub; export HX_MARKER=kept")
    await shell.run("sleep 30", timeout_seconds=1.0)

    result = await shell.run("echo alive; basename $(pwd); echo $HX_MARKER")
    assert result.stdout.split() == ["alive", "sub", "kept"]


async def test_next_command_is_not_swallowed_after_a_timeout(shell: PersistentShell) -> None:
    """The interrupted command still owes a sentinel; a stale one left in the
    pipe makes the next command look like it produced nothing."""
    await shell.run("sleep 30", timeout_seconds=1.0)
    assert (await shell.run("echo recovered")).stdout.strip() == "recovered"


async def test_background_jobs_do_not_block_the_shell(tmp_path: Path) -> None:
    jobs = BackgroundJobs(tmp_path / "logs")
    job_id = await jobs.start("for i in 1 2 3; do echo tick $i; sleep 0.05; done", tmp_path)
    await asyncio.sleep(0.5)

    assert "tick 3" in jobs.output(job_id)
    assert jobs.list()[0]["job_id"] == job_id
    await jobs.close_all()


def test_unknown_background_job_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="unknown background job"):
        BackgroundJobs(tmp_path / "logs").output("bg_nope")


async def test_tool_reports_the_exit_code_as_an_error(
    shell: PersistentShell, ctx: ToolContext
) -> None:
    tool = BashTool(shell, BackgroundJobs(ctx.cwd / "logs"))
    result = await tool.run({"command": "exit 3"}, ctx)
    assert result.is_error
    assert "exit code 3" in result.content


async def test_tool_streams_progress_to_the_ui(shell: PersistentShell, ctx: ToolContext) -> None:
    chunks: list[str] = []
    streaming_ctx = ToolContext(
        cwd=ctx.cwd,
        session_id=ctx.session_id,
        tool_use_id=ctx.tool_use_id,
        settings=ctx.settings,
        emit_progress=chunks.append,
    )
    tool = BashTool(shell, BackgroundJobs(ctx.cwd / "logs"))
    await tool.run({"command": "echo streamed"}, streaming_ctx)
    assert "streamed" in "".join(chunks)


async def test_tool_caps_huge_output_into_context(
    shell: PersistentShell, ctx: ToolContext, hx_home: Path
) -> None:
    tool = BashTool(shell, BackgroundJobs(ctx.cwd / "logs"))
    result = await tool.run({"command": "seq 1 200000"}, ctx)

    assert result.spilled_path is not None
    assert len(result.content) <= ctx.settings.context.tool_output_char_cap
    assert Path(result.spilled_path).exists()


async def test_empty_command_is_rejected(shell: PersistentShell, ctx: ToolContext) -> None:
    tool = BashTool(shell, BackgroundJobs(ctx.cwd / "logs"))
    with pytest.raises(ToolError, match="empty"):
        await tool.run({"command": "   "}, ctx)


@pytest.mark.parametrize("code", [3, 7])
async def test_a_command_that_kills_the_shell_still_reports_its_status(
    shell: PersistentShell, code: int
) -> None:
    """`exit 3` terminates the shell itself.

    Reading returncode immediately races the reaper - it is None until the
    process is collected - so the status was silently replaced by a fallback
    of 1. It reproduced on Linux CI and not on macOS, which is exactly the kind
    of thing a matrix is for.
    """
    result = await shell.run(f"exit {code}", timeout_seconds=10)
    assert result.exit_code == code


async def test_the_shell_comes_back_after_it_exits(shell: PersistentShell) -> None:
    await shell.run("exit 3", timeout_seconds=10)
    assert (await shell.run("echo alive")).stdout.strip() == "alive"
