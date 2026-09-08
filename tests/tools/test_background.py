"""Background jobs must be readable and killable by the model.

Before BashOutput and KillShell existed, the Bash tool told the model to use
them and neither was registered - so `run_in_background` started work nobody
could ever see again.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hx.config import load_settings
from hx.tools.base import ToolContext, ToolError
from hx.tools.bash import BackgroundJobs, BashOutputTool, BashTool, KillShellTool, PersistentShell
from hx.tools.read import FileTracker
from hx.tools.registry import build_default_registry


@pytest.fixture()
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        cwd=tmp_path,
        session_id="s",
        tool_use_id="t1",
        settings=load_settings(tmp_path),
        emit_progress=lambda _chunk: None,
    )


def test_the_tools_the_model_is_told_about_are_registered(tmp_path: Path) -> None:
    shell = PersistentShell(tmp_path)
    jobs = BackgroundJobs(tmp_path / "logs")
    names = build_default_registry(shell, jobs, FileTracker()).names()

    assert {"Bash", "BashOutput", "KillShell"} <= set(names)


async def test_a_background_job_can_be_read(tmp_path: Path, ctx: ToolContext) -> None:
    jobs = BackgroundJobs(tmp_path / "logs")
    shell = PersistentShell(tmp_path)
    await shell.start()
    try:
        start = await BashTool(shell, jobs).run(
            {
                "command": "for i in 1 2 3; do echo tick $i; sleep 0.05; done",
                "run_in_background": True,
            },
            ctx,
        )
        job_id = start.metadata["job_id"]
        assert job_id in start.content

        await asyncio.sleep(0.5)
        out = await BashOutputTool(jobs).run({"job_id": job_id}, ctx)
        assert "tick 3" in out.content
    finally:
        await jobs.close_all()
        await shell.close()


async def test_polling_returns_only_new_output(tmp_path: Path, ctx: ToolContext) -> None:
    """Re-sending the whole log on every poll would fill the window with
    output the model has already read."""
    jobs = BackgroundJobs(tmp_path / "logs")
    tool = BashOutputTool(jobs)
    job_id = await jobs.start("echo first; sleep 0.4; echo second", tmp_path)
    try:
        await asyncio.sleep(0.2)
        first = await tool.run({"job_id": job_id}, ctx)
        await asyncio.sleep(0.5)
        second = await tool.run({"job_id": job_id}, ctx)
    finally:
        await jobs.close_all()

    assert "first" in first.content
    assert "second" in second.content
    assert "first" not in second.content


async def test_output_reports_when_the_job_has_finished(tmp_path: Path, ctx: ToolContext) -> None:
    jobs = BackgroundJobs(tmp_path / "logs")
    job_id = await jobs.start("echo done; exit 7", tmp_path)
    await asyncio.sleep(0.4)
    try:
        result = await BashOutputTool(jobs).run({"job_id": job_id}, ctx)
    finally:
        await jobs.close_all()

    assert "job finished" in result.content
    assert "7" in result.content


async def test_a_job_can_be_killed(tmp_path: Path, ctx: ToolContext) -> None:
    jobs = BackgroundJobs(tmp_path / "logs")
    job_id = await jobs.start("sleep 30", tmp_path)
    try:
        await KillShellTool(jobs).run({"job_id": job_id}, ctx)
        await asyncio.sleep(0.3)
        assert not jobs.state(job_id)["running"]
    finally:
        await jobs.close_all()


async def test_unknown_job_ids_are_reported(tmp_path: Path, ctx: ToolContext) -> None:
    jobs = BackgroundJobs(tmp_path / "logs")
    with pytest.raises(ToolError, match="unknown background job"):
        await BashOutputTool(jobs).run({"job_id": "bg_nope"}, ctx)


async def test_the_configured_timeout_is_honoured(tmp_path: Path) -> None:
    """`bash.timeout_seconds` in settings.json must actually bound a command."""
    settings = load_settings(tmp_path, {"bash": {"timeout_seconds": 1, "max_timeout_seconds": 2}})
    ctx = ToolContext(tmp_path, "s", "t1", settings, lambda _c: None)

    shell = PersistentShell(tmp_path)
    await shell.start()
    try:
        result = await BashTool(shell, BackgroundJobs(tmp_path / "logs")).run(
            {"command": "sleep 20"}, ctx
        )
        assert "exceeded 1s" in result.content

        # And the ceiling wins over a larger model-supplied timeout.
        assert BashTool._timeout({"timeout": 600}, ctx) == 2
    finally:
        await shell.close()
