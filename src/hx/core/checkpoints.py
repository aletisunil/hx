"""File checkpoints, so a rewind can undo edits as well as words.

Rewinding the transcript alone would leave the model reading a conversation
that never happened while the working tree still holds every edit it made. So
before each mutation Write and Edit park the file's previous bytes in a
content-addressed store under the session directory and record where in the
transcript that happened; a rewind to message ``k`` puts back the bytes each
file had before the first change after ``k``.

Entries are keyed by resolved path (:func:`store_key`), so two spellings of one
file share a chain rather than fighting over it.

Two hashes per entry, not one. The pre-image says what to put back; the
post-image says what HX left behind, and a file whose bytes have moved since
then was changed by someone else. Restoring over that would destroy work HX
never did, so those files are reported and left alone.

Only Write and Edit are covered. A shell command that rewrites a file is
invisible here, and the restore report says so rather than implying the tree
was fully returned - the alternative is snapshotting the whole tree around
every Bash call.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hx.core.session import Session
    from hx.tools.read import FileTracker

MAX_BLOB_BYTES = 8 * 1024 * 1024
"""Files above this are recorded but not snapshotted. A build artefact or a
vendored binary would otherwise put a copy of itself in the session directory
on every edit; the entry still exists, so a restore reports the gap instead of
silently leaving the file as it is."""


@dataclass(slots=True)
class Checkpoint:
    """One file, at one point in the transcript."""

    index: int
    """Number of messages in the transcript when the change was made, so a
    rewind to message ``k`` restores exactly the entries with ``index >= k``."""
    path: str
    existed: bool
    """False when HX created the file - restoring means deleting it again."""
    before: str | None = None
    """Digest of the pre-image blob. ``None`` when the file did not exist or
    was too large to snapshot."""
    after: str | None = None
    """Digest of what HX left on disk, or ``None`` if that could not be read.
    A file that no longer matches this was changed by someone else."""
    captured: bool = True
    """False when the pre-image could not be stored."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "path": self.path,
            "existed": self.existed,
            "before": self.before,
            "after": self.after,
            "captured": self.captured,
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> Checkpoint:
        return Checkpoint(
            index=int(raw["index"]),
            path=str(raw["path"]),
            existed=bool(raw["existed"]),
            before=raw.get("before"),
            after=raw.get("after"),
            captured=bool(raw.get("captured", True)),
        )


@dataclass(slots=True)
class RestoreReport:
    """What a restore actually did. Every path lands in exactly one list."""

    restored: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    conflicted: list[str] = field(default_factory=list)
    """Changed by someone other than HX since HX wrote them, so left alone."""
    skipped: list[tuple[str, str]] = field(default_factory=list)
    """``(path, reason)`` - no snapshot to put back."""

    @property
    def touched(self) -> int:
        return len(self.restored) + len(self.removed)

    def summary(self) -> str:
        parts = []
        if self.restored:
            parts.append(f"{len(self.restored)} file(s) restored")
        if self.removed:
            parts.append(f"{len(self.removed)} removed")
        if self.conflicted:
            parts.append(f"{len(self.conflicted)} changed since and left alone")
        if self.skipped:
            parts.append(f"{len(self.skipped)} not snapshotted")
        return ", ".join(parts) if parts else "no files to restore"


class CheckpointStore:
    """Content-addressed pre-images for one session.

    Capture and settle are two calls because one hash cannot answer both
    questions a restore asks. Between them the entry is held in memory: the
    transcript is append-only, so a record written at capture time could never
    be given its post-image.
    """

    def __init__(self, session: Session, root: Path, max_bytes: int = MAX_BLOB_BYTES) -> None:
        self.session = session
        self.root = root
        self.max_bytes = max_bytes
        self._pending: dict[str, Checkpoint] = {}

    def attach(self, session: Session, root: Path) -> None:
        """Follow the app to another session.

        ``/clear`` and ``/resume`` swap the transcript underneath the tools,
        which hold this store for the life of the process. A capture that was
        never settled belongs to the session being left behind.
        """
        self.session = session
        self.root = root
        self._pending.clear()

    def capture(self, path: Path) -> None:
        """Park the current bytes of ``path`` before it is written."""
        key = store_key(path)
        index = len(self.session.messages)
        try:
            data = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            self._pending[key] = Checkpoint(index=index, path=key, existed=False)
            return
        except OSError:
            self._pending[key] = Checkpoint(index=index, path=key, existed=True, captured=False)
            return

        if len(data) > self.max_bytes:
            self._pending[key] = Checkpoint(index=index, path=key, existed=True, captured=False)
            return

        digest = self._store(data)
        self._pending[key] = Checkpoint(
            index=index, path=key, existed=True, before=digest, captured=digest is not None
        )

    def settle(self, path: Path) -> None:
        """Record the entry now that ``path`` holds what HX wrote.

        A capture with no settle is a mutation that never landed, and there is
        nothing to restore - so the entry is dropped rather than recorded
        half-formed.
        """
        entry = self._pending.pop(store_key(path), None)
        if entry is None:
            return
        entry.after = digest_of(path)
        self.session.record_checkpoint(entry)

    def restore_to(self, index: int, tracker: FileTracker | None = None) -> RestoreReport:
        """Put every file back the way it was before message ``index``.

        ``tracker`` is forgotten for each file actually put back: the read that
        justified editing it may itself have been rewound away, and a model
        allowed to edit a file it has not read in the surviving transcript is
        editing blind.
        """
        report = RestoreReport()
        first: dict[str, Checkpoint] = {}
        last: dict[str, Checkpoint] = {}
        for entry in self.session.checkpoints:
            if entry.index < index:
                continue
            first.setdefault(entry.path, entry)
            last[entry.path] = entry

        for key, entry in first.items():
            path = Path(key)
            if digest_of(path) != last[key].after:
                report.conflicted.append(key)
                continue
            if not entry.existed:
                changed = self._remove(path, key, report)
            elif not entry.captured or entry.before is None:
                report.skipped.append((key, "no snapshot was taken"))
                changed = False
            else:
                changed = self._put_back(path, key, entry.before, report)
            if changed and tracker is not None:
                tracker.forget(path)
        return report

    def _put_back(self, path: Path, key: str, digest: str, report: RestoreReport) -> bool:
        blob = self.blob_path(digest)
        try:
            data = blob.read_bytes()
        except OSError:
            report.skipped.append((key, "the snapshot is gone"))
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as exc:
            report.skipped.append((key, str(exc)))
            return False
        report.restored.append(key)
        return True

    def _remove(self, path: Path, key: str, report: RestoreReport) -> bool:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            report.skipped.append((key, str(exc)))
            return False
        report.removed.append(key)
        return True

    def blob_path(self, digest: str) -> Path:
        return self.root / digest

    def _store(self, data: bytes) -> str | None:
        """Write the blob under its own digest. Identical bytes are stored once."""
        digest = hashlib.sha256(data).hexdigest()
        blob = self.blob_path(digest)
        if blob.exists():
            return digest
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = blob.with_name(f".{digest}.hx-tmp")
            tmp.write_bytes(data)
            tmp.replace(blob)
        except OSError:
            # A full or read-only disk must not fail the edit the user asked
            # for; the entry records that there is nothing to put back.
            return None
        return digest


def store_key(path: Path) -> str:
    """The identity of a file, as the store records it.

    Resolved, the way :class:`~hx.tools.read.FileTracker` keys its hashes:
    ``resolve_path`` normalises neither ``..`` nor a symlinked working
    directory, so the same file can arrive spelled two ways. Keyed on the raw
    string those become two independent chains, and a restore then puts back
    one chain's pre-image and reports the other as changed by someone else -
    telling the user a file was preserved while it was rolled back to the
    wrong revision.
    """
    try:
        return str(path.resolve())
    except OSError:
        # A resolve can fail on a broken mount or a symlink loop. An
        # unnormalised key still restores correctly on its own; it just cannot
        # be recognised as the same file under another spelling.
        return str(path)


def digest_of(path: Path) -> str | None:
    """SHA-256 of a file, or ``None`` when it cannot be read - which is also
    how "the file is not there" is spelled."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
