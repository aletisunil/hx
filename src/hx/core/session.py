"""Session persistence: append-only JSONL transcripts.

Every message, tool result, usage record and compaction boundary is appended as
it happens, so a crashed session is fully resumable and a compaction can be
inspected or undone after the fact.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from hx.core.messages import Message, from_dict, to_dict
from hx.core.usage import TurnUsage, UsageLedger
from hx.paths import (
    session_dir,
    session_outputs_dir,
    session_transcript_file,
    sessions_dir,
)


@dataclass(slots=True)
class SessionMeta:
    session_id: str
    cwd: str
    created_at: float
    updated_at: float
    model: str
    title: str | None = None
    message_count: int = 0
    parent_id: str | None = None
    """Set for subagent sessions, which nest under their parent's directory."""


@dataclass(slots=True)
class Session:
    """In-memory session state, mirrored to ``~/.hx/sessions/<id>/transcript.jsonl``."""

    meta: SessionMeta
    messages: list[Message] = field(default_factory=list)
    usage: UsageLedger = field(default_factory=UsageLedger)
    _pending: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def append(self, message: Message) -> None:
        """Add to memory and flush the JSONL line. Ephemeral messages are recorded
        but marked so resume does not replay stale reminders."""
        self.messages.append(message)
        self.meta.message_count = len(self.messages)
        self.meta.updated_at = time.time()
        self._pending.append({"kind": "message", "data": to_dict(message)})
        self.flush()

    def record_compaction(
        self,
        dropped: list[Message],
        kept: list[Message],
        summary: Message,
    ) -> None:
        """Retire the pre-compaction history and re-lay it as summary + kept.

        The transcript is append-only, so the summary cannot be spliced in
        ahead of the messages it replaces. Instead every live message is
        flagged compacted and the new sequence is appended in order. Nothing is
        deleted, so a resume replays exactly what happened and a future undo
        still has the original turns to restore.
        """
        superseded = [*dropped, *kept]
        for message in superseded:
            message.compacted = True

        self._pending.append({"kind": "compaction", "data": {"count": len(superseded)}})
        self.append(summary)
        for message in kept:
            self.append(replace(message, compacted=False))

    def set_title(self, title: str) -> None:
        """Name the session for ``/resume``.

        ``updated_at`` is deliberately left alone: it orders the picker and must
        keep reflecting real activity, not the moment a name was written.
        """
        self.meta.title = title.strip() or None
        self._write_meta()

    def record_usage(self, usage: TurnUsage) -> None:
        self.usage.record(usage)
        self._pending.append({"kind": "usage", "data": asdict(usage)})

    def active_messages(self) -> list[Message]:
        """Messages eligible for context: not ``compacted``, not ``ephemeral``."""
        return [m for m in self.messages if not m.compacted and not m.ephemeral]

    def flush(self) -> None:
        if not self._pending:
            return
        path = session_transcript_file(self.meta.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for record in self._pending:
                handle.write(json.dumps(record) + "\n")
        self._pending.clear()
        self._write_meta()

    def _write_meta(self) -> None:
        path = session_dir(self.meta.session_id) / "meta.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self.meta), indent=2))


def new_session(cwd: Path, model: str, parent_id: str | None = None) -> Session:
    """Create a session with a fresh id and its on-disk directory.

    A subagent session nests under its parent (``<parent>/sub-<id>``), which
    keeps its transcript alongside the work it belongs to and keeps it out of
    the ``/resume`` listing, where it would be noise.
    """
    now = time.time()
    session_id = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}-{uuid.uuid4().hex[:8]}"
    if parent_id:
        if ".." in parent_id or parent_id.startswith("/"):
            raise ValueError(f"unsafe parent session id: {parent_id!r}")
        session_id = f"{parent_id}/sub-{uuid.uuid4().hex[:8]}"

    meta = SessionMeta(
        session_id=session_id,
        cwd=str(cwd.resolve()),
        created_at=now,
        updated_at=now,
        model=model,
        parent_id=parent_id,
    )
    session_outputs_dir(session_id).mkdir(parents=True, exist_ok=True)
    session = Session(meta=meta)
    session._write_meta()
    return session


def load_session(session_id: str) -> Session:
    """Rehydrate from JSONL.

    Raises:
        SessionNotFound: if the id has no transcript.
    """
    path = session_transcript_file(session_id)
    meta_path = session_dir(session_id) / "meta.json"
    if not meta_path.exists():
        raise SessionNotFound(session_id)

    meta = SessionMeta(**json.loads(meta_path.read_text()))
    session = Session(meta=meta)
    if not path.exists():
        return session

    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # A torn final line from a crashed write costs one record, not the session.
                continue
            kind = record.get("kind")
            if kind == "message":
                session.messages.append(from_dict(record["data"]))
            elif kind == "usage":
                session.usage.record(TurnUsage(**record["data"]))
            elif kind == "compaction":
                # Compaction always supersedes a prefix of the live messages,
                # so replaying in order reproduces the same partition.
                remaining = int(record["data"]["count"])
                for message in session.messages:
                    if remaining <= 0:
                        break
                    if not message.compacted:
                        message.compacted = True
                        remaining -= 1
    session.meta.message_count = len(session.messages)
    return session


def list_sessions(cwd: Path | None = None, limit: int = 20) -> list[SessionMeta]:
    """Most-recent-first. Filtered to ``cwd`` when given - powers ``/resume``.

    Sessions that never recorded a message are skipped: they are the residue of
    a launch that went nowhere, and resuming one is indistinguishable from
    starting fresh.
    """
    root = sessions_dir()
    if not root.is_dir():
        return []

    metas: list[SessionMeta] = []
    for entry in root.iterdir():
        meta_path = entry / "meta.json"
        if not meta_path.is_file():
            continue
        try:
            meta = SessionMeta(**json.loads(meta_path.read_text()))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if cwd is not None and meta.cwd != str(cwd.resolve()):
            continue
        if meta.message_count <= 0:
            continue
        metas.append(meta)

    metas.sort(key=lambda m: m.updated_at, reverse=True)
    return metas[:limit]


def latest_session(cwd: Path) -> SessionMeta | None:
    found = list_sessions(cwd, limit=1)
    return found[0] if found else None


class SessionNotFound(Exception):
    pass
