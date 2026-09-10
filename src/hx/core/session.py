"""Session persistence: append-only JSONL transcripts.

Every message, tool result, usage record, file checkpoint and compaction
boundary is appended as it happens, so a crashed session is fully resumable and
a compaction can be inspected or undone after the fact.

Nothing is ever rewritten or removed - a rewind is one more record, and the
state it produces is whatever replaying the file yields. That is what keeps
"undo" and "resume" the same code path rather than two that must agree.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from hx.core.checkpoints import Checkpoint
from hx.core.messages import Message, ToolResultBlock, from_dict, to_dict
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


@dataclass(frozen=True, slots=True)
class RewindPoint:
    """A prompt the session can be taken back to."""

    index: int
    """Position in ``messages``. Rewinding here drops this prompt and
    everything after it."""
    text: str
    timestamp: float
    compacted: bool
    """The prompt is behind a compaction summary. Rewinding to it undoes that
    compaction too, which is the only way back to the turns it replaced."""


@dataclass(slots=True)
class Session:
    """In-memory session state, mirrored to ``~/.hx/sessions/<id>/transcript.jsonl``."""

    meta: SessionMeta
    messages: list[Message] = field(default_factory=list)
    usage: UsageLedger = field(default_factory=UsageLedger)
    checkpoints: list[Checkpoint] = field(default_factory=list)
    """Pre-images of the files HX changed, in the order they were changed."""
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

    def record_checkpoint(self, checkpoint: Checkpoint) -> None:
        self.checkpoints.append(checkpoint)
        self._pending.append({"kind": "checkpoint", "data": checkpoint.as_dict()})
        self.flush()

    def rewind_points(self) -> list[RewindPoint]:
        """The prompts in this session, oldest first.

        Only things the user actually typed. Tool results ride user-role
        messages to match the provider wire format, and cutting the transcript
        at one would sever a tool call from its result - which no provider
        accepts back. Late-injected reminders and compaction summaries are not
        prompts either.

        One row per prompt, at its earliest index. A compaction re-appends the
        turns it kept, so a prompt it spanned occurs again verbatim - same text,
        same timestamp - and listing both would offer two rows a picker cannot
        tell apart. The original wins: rewinding there undoes the compaction
        and gives back the turns it replaced, which is the more complete of the
        two and the only one that can reach them at all.
        """
        points: list[RewindPoint] = []
        seen: set[tuple[float, str]] = set()
        for index, message in enumerate(self.messages):
            if not _is_prompt(message):
                continue
            text = message.text().strip()
            identity = (message.timestamp, text)
            if identity in seen:
                continue
            seen.add(identity)
            points.append(
                RewindPoint(
                    index=index,
                    text=text,
                    timestamp=message.timestamp,
                    compacted=message.compacted,
                )
            )
        return points

    def rewind_to(self, index: int) -> None:
        """Drop the transcript from ``index`` on.

        The file keeps every line: the rewind is appended as a record, and the
        in-memory state is then whatever replaying the file produces. Replaying
        rather than editing in place is what makes an undone compaction come
        back correctly - the messages it superseded have to lose that flag, and
        only the record order knows which ones.

        Usage is deliberately not rewound. Those tokens were spent; a ledger
        that forgets them would be lying about what the session cost.
        """
        if not 0 <= index <= len(self.messages):
            raise ValueError(f"cannot rewind to {index}: the session has {len(self.messages)}")

        self._pending.append({"kind": "rewind", "data": {"to": index}})
        self.flush()

        replayed = load_session(self.meta.session_id)
        self.messages = replayed.messages
        self.checkpoints = replayed.checkpoints
        self.meta.message_count = len(self.messages)
        self.meta.updated_at = time.time()
        self._write_meta()

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


def _is_prompt(message: Message) -> bool:
    return (
        message.role == "user"
        and not message.ephemeral
        and not message.metadata.get("compaction_summary")
        and not any(isinstance(block, ToolResultBlock) for block in message.content)
    )


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

    #: ``(summary index, indices it superseded)`` per compaction still standing.
    #: A rewind past a summary has to give those messages their content back,
    #: and only the compaction that flagged them knows which they were.
    compactions: list[tuple[int, list[int]]] = []

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
            elif kind == "checkpoint":
                session.checkpoints.append(Checkpoint.from_dict(record["data"]))
            elif kind == "compaction":
                # Compaction always supersedes a prefix of the live messages,
                # so replaying in order reproduces the same partition.
                remaining = int(record["data"]["count"])
                flagged: list[int] = []
                for index, message in enumerate(session.messages):
                    if remaining <= 0:
                        break
                    if not message.compacted:
                        message.compacted = True
                        flagged.append(index)
                        remaining -= 1
                # The summary is the next message appended, so it lands here.
                compactions.append((len(session.messages), flagged))
            elif kind == "rewind":
                _rewind(session, int(record["data"]["to"]), compactions)
    session.meta.message_count = len(session.messages)
    return session


def _rewind(session: Session, to: int, compactions: list[tuple[int, list[int]]]) -> None:
    """Apply one rewind record while replaying.

    Any compaction whose summary is being cut away is undone with it: the
    messages it replaced are all that is left to represent those turns, and
    leaving them flagged would rewind the session into an empty context.
    """
    del session.messages[to:]
    session.checkpoints = [c for c in session.checkpoints if c.index < to]
    while compactions and compactions[-1][0] >= to:
        _, flagged = compactions.pop()
        for index in flagged:
            # A compaction can supersede messages on both sides of the cut: the
            # ones past it went with the truncation and need nothing.
            if index < len(session.messages):
                session.messages[index].compacted = False


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
