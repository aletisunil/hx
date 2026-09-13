"""The entry point ``hx.cli`` calls.

One name that does not move, whatever is drawing underneath it. That is what
let the frontend be replaced in stages without the CLI being touched once, and
it is worth keeping for the next time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hx.config import Settings

if TYPE_CHECKING:
    from hx.core.events import EventBus
    from hx.core.loop import AgentLoop
    from hx.providers.models import ModelRegistry


async def run_tui(
    loop: AgentLoop,
    bus: EventBus,
    settings: Settings,
    models: ModelRegistry | None = None,
    auth: Any = None,
    sandbox_active: bool = True,
    sandbox_backend: str = "none",
    skills: Any = None,
    agents: Any = None,
    mcp: Any = None,
    notices: list[str] | None = None,
    checkpoints: Any = None,
    tracker: Any = None,
) -> None:
    """Run the interactive session.

    The signature is the boundary ``hx.cli`` calls, and it did not change as
    the frontend underneath it was replaced.
    """
    from hx.tui.runtime import run_session

    await run_session(
        loop,
        bus,
        settings,
        models=models,
        auth=auth,
        sandbox_active=sandbox_active,
        sandbox_backend=sandbox_backend,
        skills=skills,
        agents=agents,
        mcp=mcp,
        notices=notices,
        checkpoints=checkpoints,
        tracker=tracker,
    )
