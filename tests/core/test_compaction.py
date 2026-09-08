"""Compaction boundaries."""

from __future__ import annotations

from hx.core.compaction import Compactor
from tests.conftest import unimplemented


@unimplemented
def test_triggers_at_threshold() -> None:
    compactor = Compactor()
    assert compactor.should_compact(0.85, 0.80)
    assert not compactor.should_compact(0.50, 0.80)


@unimplemented
def test_split_never_separates_tool_use_from_its_results() -> None:
    """Splitting mid-turn produces a malformed next request."""
    raise NotImplementedError


@unimplemented
def test_dropped_messages_stay_in_the_session_file() -> None:
    """Compacted messages are flagged, not deleted - resume and undo depend on it."""
    raise NotImplementedError
