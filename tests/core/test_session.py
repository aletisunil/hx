"""Session persistence around naming.

A session with no name shows up in ``/resume`` as a timestamped id, which is
what these tests exist to prevent regressing.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hx.core.session import list_sessions, load_session, new_session
from hx.paths import session_dir


def test_set_title_persists_to_meta(hx_home: Path, tmp_path: Path) -> None:
    session = new_session(tmp_path, "m")
    session.set_title("  Fix the parser  ")

    meta = json.loads((session_dir(session.meta.session_id) / "meta.json").read_text())
    assert meta["title"] == "Fix the parser"
    assert load_session(session.meta.session_id).meta.title == "Fix the parser"


def test_an_empty_title_leaves_the_session_unnamed(hx_home: Path, tmp_path: Path) -> None:
    session = new_session(tmp_path, "m")
    session.set_title("   ")
    assert session.meta.title is None


def test_naming_does_not_reorder_the_resume_list(hx_home: Path, tmp_path: Path) -> None:
    """``updated_at`` orders the picker, so it must track work, not naming."""
    from hx.core.messages import user_message

    older = new_session(tmp_path, "m")
    older.append(user_message("first"))
    time.sleep(0.01)
    newer = new_session(tmp_path, "m")
    newer.append(user_message("second"))

    before = older.meta.updated_at
    older.set_title("named later")

    assert older.meta.updated_at == before
    assert next(m.session_id for m in list_sessions(tmp_path)) == newer.meta.session_id


def test_the_title_survives_a_resume_with_messages(hx_home: Path, tmp_path: Path) -> None:
    from hx.core.messages import user_message

    session = new_session(tmp_path, "m")
    session.append(user_message("hello"))
    session.set_title("greeting")

    resumed = load_session(session.meta.session_id)
    assert resumed.meta.title == "greeting"
    assert resumed.messages[0].text() == "hello"


def test_empty_sessions_stay_out_of_the_resume_list(hx_home: Path, tmp_path: Path) -> None:
    """A session nobody spoke in is noise in the picker."""
    from hx.core.messages import user_message

    new_session(tmp_path, "m")
    used = new_session(tmp_path, "m")
    used.append(user_message("hello"))

    assert [m.session_id for m in list_sessions(tmp_path)] == [used.meta.session_id]


def test_prompts_are_counted_separately_from_wire_records(hx_home: Path, tmp_path: Path) -> None:
    """``57 msgs`` on a three-prompt session describes the protocol, not the
    conversation: tool results ride the user role, so the two numbers differ by
    a factor nobody can reconstruct from the list."""
    from hx.core.messages import (
        TextBlock,
        ToolResultBlock,
        assistant_message,
        tool_result_message,
        user_message,
    )

    session = new_session(tmp_path, "m")
    session.append(user_message("first"))
    session.append(assistant_message([TextBlock("thinking about it")]))
    session.append(tool_result_message([ToolResultBlock("c1", "output")]))
    session.append(user_message("second"))

    assert session.meta.prompt_count == 2
    assert session.meta.message_count == 4
    assert load_session(session.meta.session_id).meta.prompt_count == 2


def test_a_compaction_does_not_count_the_prompts_it_re_appends(
    hx_home: Path, tmp_path: Path
) -> None:
    """It re-appends the turns it kept verbatim, and a counter would read that
    as the user having asked twice."""
    from hx.core.messages import Message, TextBlock, assistant_message, user_message

    session = new_session(tmp_path, "m")
    session.append(user_message("first"))
    session.append(assistant_message([TextBlock("answer")]))
    kept = list(session.messages[:2])
    session.record_compaction(
        [],
        kept,
        Message(
            role="user",
            content=[TextBlock(text="<summary/>")],
            metadata={"compaction_summary": True},
        ),
    )

    assert session.meta.prompt_count == 1
    assert load_session(session.meta.session_id).meta.prompt_count == 1


def test_a_rewind_takes_the_prompt_count_back_with_it(hx_home: Path, tmp_path: Path) -> None:
    from hx.core.messages import user_message

    session = new_session(tmp_path, "m")
    session.append(user_message("first"))
    session.append(user_message("second"))
    assert session.meta.prompt_count == 2

    session.rewind_to(1)

    assert session.meta.prompt_count == 1


def test_a_session_recorded_before_prompts_were_counted_is_backfilled(
    hx_home: Path, tmp_path: Path
) -> None:
    """Otherwise every older session lists as ``N msgs`` forever - the very
    shape the count replaced."""
    from hx.core.messages import user_message

    session = new_session(tmp_path, "m")
    session.append(user_message("first"))
    session.append(user_message("second"))

    meta_path = session_dir(session.meta.session_id) / "meta.json"
    stored = json.loads(meta_path.read_text())
    del stored["prompt_count"]
    meta_path.write_text(json.dumps(stored))

    assert [m.prompt_count for m in list_sessions(tmp_path)] == [2]
    # Written back, so the replay happens at most once per session.
    assert json.loads(meta_path.read_text())["prompt_count"] == 2


def test_a_meta_file_from_a_newer_hx_still_lists(hx_home: Path, tmp_path: Path) -> None:
    """The file gains fields over time; an unknown one must not be fatal."""
    from hx.core.messages import user_message

    session = new_session(tmp_path, "m")
    session.append(user_message("first"))

    meta_path = session_dir(session.meta.session_id) / "meta.json"
    stored = json.loads(meta_path.read_text())
    stored["invented_later"] = {"whatever": True}
    meta_path.write_text(json.dumps(stored))

    assert [m.session_id for m in list_sessions(tmp_path)] == [session.meta.session_id]
    assert load_session(session.meta.session_id).meta.prompt_count == 1


def test_only_the_sessions_actually_listed_are_replayed(hx_home: Path, tmp_path: Path) -> None:
    """The backfill replays a transcript, and ``/resume`` is what the user is
    waiting on: doing it for every session on the machine to show five of them
    turns one upgrade into a visible stall."""
    from hx.core.messages import user_message

    sessions = []
    for n in range(5):
        session = new_session(tmp_path, "m")
        session.append(user_message(f"prompt {n}"))
        sessions.append(session)
        meta_path = session_dir(session.meta.session_id) / "meta.json"
        stored = json.loads(meta_path.read_text())
        del stored["prompt_count"]
        meta_path.write_text(json.dumps(stored))

    listed = list_sessions(tmp_path, limit=2)

    assert [m.session_id for m in listed] == [s.meta.session_id for s in reversed(sessions[-2:])]
    assert all(m.prompt_count == 1 for m in listed)
    written = [
        "prompt_count" in json.loads((session_dir(s.meta.session_id) / "meta.json").read_text())
        for s in sessions
    ]
    assert written == [False, False, False, True, True]


def test_a_record_appended_during_a_flush_is_not_dropped(
    hx_home: Path, tmp_path: Path, monkeypatch
) -> None:
    """``/trace`` flushes from a worker thread while the turn keeps running.

    The queue used to be written from and then cleared, so a record appended
    between those two steps was discarded without ever reaching the file - the
    transcript silently lost a message.
    """
    from hx.core.messages import user_message

    session = new_session(tmp_path, "m")
    session.append(user_message("first"))
    session._pending.append({"kind": "usage", "data": {}})

    real_dumps = json.dumps
    interleaved: list[dict] = [{"kind": "usage", "data": {"arrived": "mid-write"}}]

    def dumps_and_interleave(obj, **kwargs):
        # Stands in for the other thread: a record queued while the file is
        # being written, after this flush took the records it is writing.
        if interleaved:
            session._pending.append(interleaved.pop())
        return real_dumps(obj, **kwargs)

    monkeypatch.setattr(json, "dumps", dumps_and_interleave)
    try:
        session.flush()
    finally:
        monkeypatch.setattr(json, "dumps", real_dumps)

    assert session._pending == [{"kind": "usage", "data": {"arrived": "mid-write"}}]
    session.flush()

    lines = (session_dir(session.meta.session_id) / "transcript.jsonl").read_text().splitlines()
    assert [json.loads(line)["kind"] for line in lines] == ["message", "usage", "usage"]


def test_a_failed_flush_keeps_its_records_for_the_next_one(
    hx_home: Path, tmp_path: Path, monkeypatch
) -> None:
    """A full disk costs the next flush a retry, not the records."""
    from hx.core.messages import user_message

    session = new_session(tmp_path, "m")
    session.append(user_message("first"))
    session._pending.append({"kind": "usage", "data": {}})

    def explode(*_args: object, **_kwargs: object) -> None:
        raise OSError("no space left on device")

    # Restored by hand: `monkeypatch.undo` would also revert the $HX_HOME the
    # `hx_home` fixture set, pointing the rest of the test at the real one.
    real_open = Path.open
    monkeypatch.setattr(Path, "open", explode)
    try:
        with pytest.raises(OSError):
            session.flush()
    finally:
        monkeypatch.setattr(Path, "open", real_open)

    assert len(session._pending) == 1
    session.flush()

    lines = (session_dir(session.meta.session_id) / "transcript.jsonl").read_text().splitlines()
    assert [json.loads(line)["kind"] for line in lines] == ["message", "usage"]
