"""Rewinding the transcript.

The file is append-only, so every one of these asserts the same thing twice:
the state in memory, and the state a resume replays out of the file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hx.core.checkpoints import Checkpoint
from hx.core.messages import Message, TextBlock
from hx.core.session import Session, load_session, new_session
from hx.core.usage import TurnUsage


def _session(tmp_path: Path) -> Session:
    return new_session(tmp_path, "test/model")


def _user(session: Session, text: str) -> None:
    session.append(Message(role="user", content=[TextBlock(text=text)]))


def _assistant(session: Session, text: str) -> None:
    session.append(Message(role="assistant", content=[TextBlock(text=text)]))


def _texts(session: Session) -> list[str]:
    return [m.text() for m in session.active_messages()]


def test_rewinding_drops_the_prompt_and_everything_after(hx_home: Path, tmp_path: Path) -> None:
    session = _session(tmp_path)
    _user(session, "one")
    _assistant(session, "first answer")
    _user(session, "two")
    _assistant(session, "second answer")

    session.rewind_to(2)

    assert _texts(session) == ["one", "first answer"]
    assert _texts(load_session(session.meta.session_id)) == ["one", "first answer"]


def test_the_transcript_file_keeps_every_line(hx_home: Path, tmp_path: Path) -> None:
    """Nothing is deleted - the rewind is one more record, which is what makes
    the replay the single source of truth."""
    from hx.paths import session_transcript_file

    session = _session(tmp_path)
    _user(session, "one")
    _assistant(session, "answer")
    before = session_transcript_file(session.meta.session_id).read_text()

    session.rewind_to(0)

    after = session_transcript_file(session.meta.session_id).read_text()
    assert after.startswith(before)
    assert "answer" in after


def test_the_session_carries_on_after_a_rewind(hx_home: Path, tmp_path: Path) -> None:
    session = _session(tmp_path)
    _user(session, "one")
    _assistant(session, "wrong answer")
    session.rewind_to(1)

    _assistant(session, "better answer")

    assert _texts(session) == ["one", "better answer"]
    assert _texts(load_session(session.meta.session_id)) == ["one", "better answer"]


def test_two_rewinds_compose(hx_home: Path, tmp_path: Path) -> None:
    session = _session(tmp_path)
    _user(session, "one")
    _assistant(session, "a")
    session.rewind_to(1)
    _assistant(session, "b")

    session.rewind_to(1)

    assert _texts(session) == ["one"]
    assert _texts(load_session(session.meta.session_id)) == ["one"]


def test_rewinding_past_a_compaction_gives_the_turns_back(hx_home: Path, tmp_path: Path) -> None:
    """The summary is gone, so the messages it replaced are all that is left to
    represent those turns. Left flagged, they would rewind into an empty
    context - the worst possible outcome of an undo."""
    session = _session(tmp_path)
    _user(session, "one")
    _assistant(session, "first answer")
    _user(session, "two")
    _assistant(session, "second answer")

    dropped = session.messages[:2]
    kept = session.messages[2:]
    summary = Message(role="user", content=[TextBlock(text="<summary/>")])
    session.record_compaction(dropped, kept, summary)
    assert _texts(session) == ["<summary/>", "two", "second answer"]

    session.rewind_to(2)

    assert _texts(session) == ["one", "first answer"]
    assert _texts(load_session(session.meta.session_id)) == ["one", "first answer"]


def test_a_compaction_before_the_rewind_point_still_stands(hx_home: Path, tmp_path: Path) -> None:
    """Only the compactions being cut away are undone. One whose summary
    survives is still doing its job."""
    session = _session(tmp_path)
    _user(session, "one")
    _assistant(session, "first answer")
    session.record_compaction(
        session.messages[:2], [], Message(role="user", content=[TextBlock(text="<summary/>")])
    )
    _user(session, "two")
    _assistant(session, "second answer")
    summary_index = 2

    session.rewind_to(summary_index + 1)

    assert _texts(session) == ["<summary/>"]
    assert _texts(load_session(session.meta.session_id)) == ["<summary/>"]


def test_checkpoints_after_the_point_go_with_the_messages(hx_home: Path, tmp_path: Path) -> None:
    session = _session(tmp_path)
    _user(session, "one")
    session.record_checkpoint(Checkpoint(index=1, path="/tmp/a.py", existed=True))
    _user(session, "two")
    session.record_checkpoint(Checkpoint(index=2, path="/tmp/b.py", existed=True))

    session.rewind_to(2)

    assert [c.path for c in session.checkpoints] == ["/tmp/a.py"]
    assert [c.path for c in load_session(session.meta.session_id).checkpoints] == ["/tmp/a.py"]


def test_the_cost_ledger_is_not_rewound(hx_home: Path, tmp_path: Path) -> None:
    """Those tokens were spent. A ledger that forgets them lies about what the
    session cost."""
    session = _session(tmp_path)
    _user(session, "one")
    session.record_usage(TurnUsage(input_tokens=100, output_tokens=50, cost_usd=0.5))
    session.flush()

    session.rewind_to(0)

    assert session.usage.total_cost_usd == pytest.approx(0.5)


def test_rewind_points_are_the_prompts(hx_home: Path, tmp_path: Path) -> None:
    """Not assistant turns: cutting there would sever a tool call from its
    result, which no provider accepts back."""
    session = _session(tmp_path)
    _user(session, "one")
    _assistant(session, "answer")
    _user(session, "two")
    session.append(Message(role="user", content=[TextBlock(text="<hx-reminder/>")], ephemeral=True))

    points = session.rewind_points()

    assert [(p.index, p.text) for p in points] == [(0, "one"), (2, "two")]


def test_tool_results_are_not_offered_as_rewind_points(hx_home: Path, tmp_path: Path) -> None:
    """They ride user-role messages to match the wire format. Cutting at one
    would leave the assistant's tool call with nothing answering it."""
    from hx.core.messages import ToolResultBlock, tool_result_message

    session = _session(tmp_path)
    _user(session, "one")
    _assistant(session, "calling a tool")
    session.append(tool_result_message([ToolResultBlock(tool_use_id="t1", content="output")]))

    assert [p.index for p in session.rewind_points()] == [0]


def test_a_compaction_summary_is_not_offered_as_a_prompt(hx_home: Path, tmp_path: Path) -> None:
    session = _session(tmp_path)
    _user(session, "one")
    _assistant(session, "answer")
    session.record_compaction(
        session.messages[:2],
        [],
        Message(
            role="user",
            content=[TextBlock(text="<summary/>")],
            metadata={"compaction_summary": True},
        ),
    )

    assert [p.text for p in session.rewind_points()] == ["one"]


def test_an_out_of_range_rewind_is_refused(hx_home: Path, tmp_path: Path) -> None:
    session = _session(tmp_path)
    _user(session, "one")

    with pytest.raises(ValueError):
        session.rewind_to(5)
