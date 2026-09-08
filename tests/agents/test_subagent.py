"""Subagent isolation."""

from __future__ import annotations

from tests.conftest import unimplemented


@unimplemented
async def test_subagents_cannot_spawn_subagents() -> None:
    """Recursion is prevented by omitting Task from the allowlist, not by a counter."""
    raise NotImplementedError


@unimplemented
async def test_only_the_final_report_reaches_the_parent() -> None:
    """Intermediate tool output staying out of the parent context is the reason
    subagents exist at all."""
    raise NotImplementedError


@unimplemented
async def test_subagent_cost_rolls_into_the_session_ledger() -> None:
    raise NotImplementedError
