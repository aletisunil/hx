"""Permission approval modal."""

from __future__ import annotations

from typing import Any

from textual.screen import ModalScreen


class PermissionModal(ModalScreen[Any]):
    """Approve or refuse one tool call.

    Shows exactly what will run - the full command, or the unified diff for an
    edit - before offering the choice. Options: allow once, allow for this
    session, always allow (persists a rule), or refuse.

    When the request came from a subagent the modal names it; an approval whose
    origin is unclear is not an informed approval.
    """

    def __init__(self, request: Any, origin: str | None = None) -> None:
        raise NotImplementedError
