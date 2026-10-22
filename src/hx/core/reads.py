"""What a session has read: the record behind the read-before-edit rule.

Edit and Write refuse a file the model has not read, because a model editing a
file it has never seen is guessing at it. "Read" means read in this session's
transcript - the one the model is shown - so the record lives in the session,
not in the process. A resumed session gets back the reads its transcript
holds, ``/clear`` starts with none, and a rewind loses the reads it cuts away.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hx.core.checkpoints import digest_of, store_key

if TYPE_CHECKING:
    from hx.core.session import Session


@dataclass(frozen=True, slots=True)
class FileRead:
    """One file as the model saw it."""

    index: int
    """``len(messages)`` when the file was read: where the result that showed
    it lands. A rewind to this index or earlier cuts that result, and the read
    goes with it - the same rule :class:`~hx.core.checkpoints.Checkpoint` uses."""
    path: str
    """Resolved, as :func:`~hx.core.checkpoints.store_key` spells it."""
    digest: str | None
    """SHA-256 of the content then. ``None`` when it could not be read."""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FileRead:
        return cls(index=int(raw["index"]), path=str(raw["path"]), digest=raw.get("digest"))


class FileTracker:
    """The reads of one session, as the tools ask about them.

    A view, not a store: the reads are :attr:`Session.reads`, so whatever
    replaces the session - ``/clear``, ``/resume``, a subagent's own session -
    replaces what counts as read with it.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def mark_read(self, path: Path) -> None:
        """Record ``path`` as the model now sees it: after a Read, or after an
        Edit or Write, whose result shows what HX itself put there."""
        self.session.record_read(store_key(path), digest_of(path))

    def was_read(self, path: Path) -> bool:
        return store_key(path) in self.session.read_digests

    def changed_since_read(self, path: Path) -> bool:
        key = store_key(path)
        known = self.session.read_digests
        return key in known and known[key] != digest_of(path)

    def stale_files(self) -> list[Path]:
        """Every read file whose content is no longer what was read.

        Asked before every model call, over every file the session ever read -
        a resumed session included - so a file is hashed again only when its
        stat says it may have changed. Edit and Write still hash in full: they
        are the check that must not miss a change.
        """
        known = self.session.read_digests
        return [Path(key) for key, digest in known.items() if _cached_digest(key) != digest]


_STAT_DIGESTS: dict[str, tuple[tuple[int, int, int], str | None]] = {}
"""Each checked path's (inode, size, mtime) and its digest at that stat."""


def _cached_digest(key: str) -> str | None:
    """:func:`digest_of`, hashing only a file whose stat has moved since."""
    path = Path(key)
    try:
        info = path.stat()
    except OSError:
        _STAT_DIGESTS.pop(key, None)
        return None
    stat = (info.st_ino, info.st_size, info.st_mtime_ns)
    cached = _STAT_DIGESTS.get(key)
    if cached is not None and cached[0] == stat:
        return cached[1]
    digest = digest_of(path)
    _STAT_DIGESTS[key] = (stat, digest)
    return digest
