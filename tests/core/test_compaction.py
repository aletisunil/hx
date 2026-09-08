"""Compaction boundaries and bookkeeping."""

from __future__ import annotations

from pathlib import Path

import pytest

from hx.core.compaction import SUMMARY_MARKER, Compactor, render_transcript
from hx.core.messages import (
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    assistant_message,
    tool_result_message,
    user_message,
)
from hx.providers.base import StreamItem
from hx.providers.fake import FakeProvider, text_turn


def _conversation(turns: int = 10) -> list:
    messages = []
    for index in range(turns):
        messages.append(user_message(f"question {index}"))
        messages.append(assistant_message([TextBlock(f"answer {index}")]))
    return messages


def test_triggers_at_threshold() -> None:
    compactor = Compactor()
    assert compactor.should_compact(0.85, 0.80)
    assert not compactor.should_compact(0.50, 0.80)


def test_a_zero_threshold_disables_compaction() -> None:
    assert not Compactor().should_compact(1.0, 0.0)


def test_split_keeps_the_configured_recent_turns() -> None:
    compactor = Compactor(keep_recent_turns=4)
    messages = _conversation(10)
    dropped, kept = compactor.split(messages)
    assert len(kept) == 4
    assert dropped + kept == messages


def test_split_never_separates_tool_use_from_its_results() -> None:
    """Splitting mid-turn produces a malformed next request."""
    messages = [
        user_message("do it"),
        assistant_message([TextBlock("ok"), ToolUseBlock("t1", "Read", {})]),
        tool_result_message([ToolResultBlock("t1", "contents")]),
        assistant_message([TextBlock("done")]),
    ]
    # A naive boundary of 2 would orphan the tool result from its call.
    dropped, kept = Compactor(keep_recent_turns=2).split(messages)

    assert not any(m.tool_uses() for m in dropped[-1:]) or not kept
    for index, message in enumerate(kept):
        if any(isinstance(b, ToolResultBlock) for b in message.content):
            assert index > 0, "kept history starts with an orphaned tool result"


def test_nothing_to_compact_is_not_an_error() -> None:
    assert Compactor(keep_recent_turns=6).split(_conversation(2))[0] == []


async def test_compact_replaces_history_with_one_summary(hx_home: Path) -> None:
    script: list[list[StreamItem]] = [text_turn("## Goal\nship the thing")]
    compactor = Compactor(provider=FakeProvider(script), model="m", keep_recent_turns=2)

    result = await compactor.compact(_conversation(10))

    assert result.dropped
    assert len(result.kept) == 2
    assert SUMMARY_MARKER in result.summary.text()
    assert "ship the thing" in result.summary.text()
    assert result.tokens_after < result.tokens_before


async def test_compact_falls_back_when_no_provider_is_available(hx_home: Path) -> None:
    """Losing the history silently would be far worse than a crude digest."""
    result = await Compactor(keep_recent_turns=2).compact(_conversation(10))
    assert "question 0" in result.summary.text()


async def test_instructions_steer_the_summary(hx_home: Path) -> None:
    provider = FakeProvider([text_turn("summary")])
    compactor = Compactor(provider=provider, model="m", keep_recent_turns=2)
    await compactor.compact(_conversation(10), instructions="focus on the failing test")

    prompt = provider.requests[0].context.messages[0].text()
    assert "focus on the failing test" in prompt


def test_transcript_rendering_includes_tool_activity() -> None:
    messages = [
        user_message("go"),
        assistant_message([ToolUseBlock("t1", "Bash", {"command": "ls"})]),
        tool_result_message([ToolResultBlock("t1", "a.py", is_error=False)]),
    ]
    rendered = render_transcript(messages)
    assert "calls Bash" in rendered
    assert "[tool result] a.py" in rendered


async def test_dropped_messages_stay_in_the_session_file(hx_home: Path, tmp_path: Path) -> None:
    """Compacted messages are flagged, not deleted - resume and undo depend on it."""
    from hx.core.session import load_session, new_session

    session = new_session(tmp_path, "m")
    for message in _conversation(6):
        session.append(message)

    compactor = Compactor(
        provider=FakeProvider([text_turn("digest")]), model="m", keep_recent_turns=2
    )
    original_count = len(session.messages)
    result = await compactor.compact(session.active_messages())
    session.record_compaction(result.dropped, result.kept, result.summary)

    active = session.active_messages()
    assert len(active) == 3
    assert SUMMARY_MARKER in active[0].text()
    assert sum(1 for m in session.messages if m.compacted) == original_count

    reloaded = load_session(session.meta.session_id)
    assert len(reloaded.messages) == len(session.messages)
    assert [m.text() for m in reloaded.active_messages()] == [m.text() for m in active]


@pytest.mark.parametrize("keep", [2, 4, 6])
def test_split_is_exhaustive_for_any_keep_count(keep: int) -> None:
    messages = _conversation(8)
    dropped, kept = Compactor(keep_recent_turns=keep).split(messages)
    assert dropped + kept == messages
