"""File checkpoints: what a rewind can put back, and what it must not touch."""

from __future__ import annotations

from pathlib import Path

from hx.core.checkpoints import Checkpoint, CheckpointStore, digest_of
from hx.core.messages import Message, TextBlock
from hx.core.session import new_session
from hx.paths import session_checkpoints_dir
from hx.tools.read import FileTracker


def _store(tmp_path: Path) -> CheckpointStore:
    session = new_session(tmp_path, "test/model")
    return CheckpointStore(session, session_checkpoints_dir(session.meta.session_id))


def _say(store: CheckpointStore, text: str) -> None:
    store.session.append(Message(role="user", content=[TextBlock(text=text)]))


def _edit(store: CheckpointStore, path: Path, content: str) -> None:
    """What Write and Edit do around a mutation."""
    store.capture(path)
    path.write_text(content)
    store.settle(path)


def test_a_file_is_put_back_as_it_was(hx_home: Path, tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "a.py"
    target.write_text("original\n")
    _say(store, "change it")
    _edit(store, target, "changed\n")

    report = store.restore_to(0)

    assert target.read_text() == "original\n"
    assert report.restored == [str(target)]


def test_only_changes_after_the_rewind_point_are_undone(hx_home: Path, tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "a.py"
    target.write_text("v0\n")
    _say(store, "first")
    _edit(store, target, "v1\n")
    _say(store, "second")
    keep_from = len(store.session.messages)
    _edit(store, target, "v2\n")

    store.restore_to(keep_from)

    assert target.read_text() == "v1\n"


def test_the_earliest_snapshot_after_the_point_wins(hx_home: Path, tmp_path: Path) -> None:
    """Three edits in one turn must rewind to what the file held before the
    first of them, not the second."""
    store = _store(tmp_path)
    target = tmp_path / "a.py"
    target.write_text("v0\n")
    _say(store, "go")
    point = len(store.session.messages)
    for version in ("v1\n", "v2\n", "v3\n"):
        _edit(store, target, version)

    store.restore_to(point)

    assert target.read_text() == "v0\n"


def test_a_file_hx_created_is_removed_again(hx_home: Path, tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "new.py"
    _say(store, "make it")
    _edit(store, target, "fresh\n")

    report = store.restore_to(0)

    assert not target.exists()
    assert report.removed == [str(target)]


def test_a_file_changed_by_someone_else_is_left_alone(hx_home: Path, tmp_path: Path) -> None:
    """The user kept editing after HX did. Restoring would destroy work HX
    never did, so the file is reported instead."""
    store = _store(tmp_path)
    target = tmp_path / "a.py"
    target.write_text("original\n")
    _say(store, "change it")
    _edit(store, target, "hx wrote this\n")
    target.write_text("and then a human did\n")

    report = store.restore_to(0)

    assert target.read_text() == "and then a human did\n"
    assert report.conflicted == [str(target)]
    assert not report.restored


def test_a_file_deleted_after_the_edit_is_not_resurrected(hx_home: Path, tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "a.py"
    target.write_text("original\n")
    _say(store, "change it")
    _edit(store, target, "hx wrote this\n")
    target.unlink()

    report = store.restore_to(0)

    assert not target.exists()
    assert report.conflicted == [str(target)]


def test_identical_bytes_are_stored_once(hx_home: Path, tmp_path: Path) -> None:
    store = _store(tmp_path)
    first, second = tmp_path / "a.py", tmp_path / "b.py"
    first.write_text("same\n")
    second.write_text("same\n")
    _say(store, "go")
    _edit(store, first, "one\n")
    _edit(store, second, "two\n")

    blobs = list(store.root.iterdir())

    assert len(blobs) == 1


def test_an_oversized_file_is_recorded_but_not_snapshotted(hx_home: Path, tmp_path: Path) -> None:
    """The entry has to exist even when the bytes do not: a restore that says
    nothing about the file would look like a complete rewind."""
    store = _store(tmp_path)
    store.max_bytes = 8
    target = tmp_path / "big.bin"
    target.write_text("far too many bytes\n")
    _say(store, "go")
    _edit(store, target, "replaced\n")

    report = store.restore_to(0)

    assert target.read_text() == "replaced\n"
    assert [path for path, _ in report.skipped] == [str(target)]
    assert not report.restored


def test_a_capture_that_never_settles_is_not_recorded(hx_home: Path, tmp_path: Path) -> None:
    """The mutation raised before it landed - there is nothing to put back."""
    store = _store(tmp_path)
    target = tmp_path / "a.py"
    target.write_text("original\n")
    _say(store, "go")

    store.capture(target)

    assert store.session.checkpoints == []


def test_restoring_forgets_the_read_that_justified_the_edit(hx_home: Path, tmp_path: Path) -> None:
    """The read may itself have been rewound away, and a model editing a file
    it has not read in the surviving transcript is editing blind."""
    store = _store(tmp_path)
    tracker = FileTracker()
    target = tmp_path / "a.py"
    target.write_text("original\n")
    tracker.mark_read(target)
    _say(store, "go")
    _edit(store, target, "changed\n")
    tracker.mark_read(target)

    store.restore_to(0, tracker)

    assert not tracker.was_read(target)


def test_attaching_drops_a_capture_from_the_session_being_left(
    hx_home: Path, tmp_path: Path
) -> None:
    store = _store(tmp_path)
    other = new_session(tmp_path, "test/model")
    target = tmp_path / "a.py"
    target.write_text("original\n")
    store.capture(target)

    store.attach(other, session_checkpoints_dir(other.meta.session_id))
    target.write_text("changed\n")
    store.settle(target)

    assert other.checkpoints == []


def test_an_entry_survives_a_round_trip(hx_home: Path, tmp_path: Path) -> None:
    entry = Checkpoint(index=3, path="/tmp/a.py", existed=True, before="aa", after="bb")

    assert Checkpoint.from_dict(entry.as_dict()) == entry


def test_the_digest_of_a_missing_file_is_none(tmp_path: Path) -> None:
    assert digest_of(tmp_path / "nope.py") is None
