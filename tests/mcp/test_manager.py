"""MCP registration."""

from __future__ import annotations

from tests.conftest import unimplemented


@unimplemented
async def test_tools_are_namespaced_per_server() -> None:
    raise NotImplementedError


@unimplemented
async def test_registration_order_is_deterministic() -> None:
    """A server returning tools in a different order must not shift the prefix."""
    raise NotImplementedError


@unimplemented
async def test_a_failing_server_does_not_abort_startup() -> None:
    raise NotImplementedError
