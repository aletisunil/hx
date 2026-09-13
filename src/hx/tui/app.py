"""Which frontend runs this session.

HX is mid-way through replacing its Textual app with a scrollback-native
renderer. Both exist, both work, and this module is the one place that decides
between them - so ``hx.cli`` keeps importing one name that never moves, and
every stage of the replacement is additive rather than a cutover.

``HX_TUI=legacy|new`` wins over the ``tui.renderer`` setting, because the
escape hatch from a frontend that will not draw has to be reachable without
editing a config file inside a terminal that is not drawing.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from hx.config import Settings, TuiRenderer

if TYPE_CHECKING:
    from hx.core.events import EventBus
    from hx.core.loop import AgentLoop
    from hx.providers.models import ModelRegistry

ENV_VAR = "HX_TUI"


def selected_renderer(settings: Settings) -> TuiRenderer:
    """The frontend this session should use.

    An unrecognised ``HX_TUI`` is ignored rather than fatal. It arrives from a
    shell profile as often as from a deliberate choice, and refusing to start
    over a typo in an override is a worse failure than using the default.
    """
    override = os.environ.get(ENV_VAR, "").strip().lower()
    if override:
        try:
            return TuiRenderer(override)
        except ValueError:
            pass
    return settings.tui.renderer


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
    """Run the interactive session on the selected frontend.

    The signature is the boundary ``hx.cli`` calls and does not change as the
    frontend underneath it does.
    """
    kwargs: dict[str, Any] = {
        "models": models,
        "auth": auth,
        "sandbox_active": sandbox_active,
        "sandbox_backend": sandbox_backend,
        "skills": skills,
        "agents": agents,
        "mcp": mcp,
        "notices": notices,
        "checkpoints": checkpoints,
        "tracker": tracker,
    }

    if selected_renderer(settings) is TuiRenderer.NEW:
        from hx.tui.runtime import run_new_tui

        await run_new_tui(loop, bus, settings, **kwargs)
        return

    from hx.tui.legacy.app import run_tui as run_legacy_tui

    await run_legacy_tui(loop, bus, settings, **kwargs)
