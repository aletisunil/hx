"""Entry point for the scrollback-native frontend.

A placeholder until the new renderer lands. It exists so that ``HX_TUI=new``
fails with a sentence explaining itself rather than an ``ImportError`` from
inside the dispatcher, and so there is an obvious file for the real
implementation to replace.
"""

from __future__ import annotations

from typing import Any

from hx.config import Settings


class RendererUnavailable(RuntimeError):
    """The requested frontend is not built yet."""


async def run_new_tui(loop: Any, bus: Any, settings: Settings, **kwargs: Any) -> None:
    raise RendererUnavailable(
        "the scrollback-native renderer is not available in this build yet. "
        "Unset HX_TUI (or set HX_TUI=legacy) to use the current interface."
    )
