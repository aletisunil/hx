"""HX's transcript in Claude Code's shapes.

Three translations live here:

* tool names - HX's tools reach the model as ``mcp__hx__<name>``, which has to
  fit the API's 64-character limit;
* content - HX blocks to Anthropic content blocks, for a user turn written to
  the CLI's stdin and for a session file;
* session files - the JSONL a ``claude --resume`` loads, rebuilt from HX's
  transcript whenever the running CLI no longer holds the conversation HX has.

HX's transcript is the one that counts. A Claude Code session is a disposable
copy of it: written when a process is started, deleted once the process has
read it, and never read back.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeGuard

from hx.core.lateinject import REMINDER_OPEN, strip_injections
from hx.core.messages import (
    ImageBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    block_to_dict,
)

SERVER_PREFIX = "mcp__hx__"
NAME_LIMIT = 64
"""The API's limit on a tool name, prefix included."""

PAYLOAD_VERSION = 1
PAYLOAD_KIND = "claude-code"
"""Marks the opaque payload on a :class:`ThinkingBlock` as this route's.

Thinking signatures are only valid for the API that issued them; a block
another route produced is dropped rather than replayed."""

RESUME_PROMPT = "Continue from where you left off."
"""What Claude Code itself says to a model whose session was resumed between a
tool result and the reply to it. A rebuilt session ending on tool results needs
a user turn to start the next request, and this one the model has seen before."""

VERSION_TAG = "hx"


# -- tool names ----------------------------------------------------------------


@dataclass(slots=True)
class ToolNames:
    """HX tool name <-> the name it is served under.

    Most names pass through. One that would push ``mcp__hx__<name>`` past the
    API's limit is shortened, with a hash of the full name so two long names
    sharing a prefix stay apart.
    """

    served: dict[str, str] = field(default_factory=dict)
    hx: dict[str, str] = field(default_factory=dict)

    @classmethod
    def for_tools(cls, names: list[str]) -> ToolNames:
        table = cls()
        for name in names:
            table.add(name)
        return table

    def add(self, name: str) -> str:
        if name in self.served:
            return self.served[name]
        served = served_name(name)
        self.served[name] = served
        self.hx[served] = name
        return served

    def model_name(self, name: str) -> str:
        """What the model calls an HX tool."""
        return SERVER_PREFIX + self.add(name)

    def from_model(self, name: str) -> str | None:
        """The HX tool behind a name the model used, or ``None`` if not one of ours."""
        if not name.startswith(SERVER_PREFIX):
            return None
        served = name[len(SERVER_PREFIX) :]
        return self.hx.get(served, served)


def served_name(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", name) or "tool"
    room = NAME_LIMIT - len(SERVER_PREFIX)
    if len(clean) <= room and clean == name:
        return clean
    digest = hashlib.sha256(name.encode()).hexdigest()[:8]
    return f"{clean[: room - 9]}_{digest}"


def tool_id(raw: str) -> str:
    """A tool call id the API accepts.

    Calls this route made already have one. A call another route made - a
    session started on OpenRouter and continued here - may not, so its id is
    rewritten the same way wherever it appears, keeping the pair together.
    """
    if raw and re.fullmatch(r"[A-Za-z0-9_-]+", raw):
        return raw
    digest = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return f"toolu_hx_{digest}"


# -- matching ------------------------------------------------------------------


def fingerprint(message: Message) -> str:
    """Identity of a message as far as the conversation is concerned.

    Late-injected reminders are left out: HX regenerates them every turn on
    whichever message is last, so the same message carries one now and none a
    turn later, and that is not a change to the conversation.
    """
    clean = strip_injections(message) if message.role == "user" else message
    payload = json.dumps(
        [clean.role, [block_to_dict(block) for block in clean.content]],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def is_reminder(block: Any) -> TypeGuard[TextBlock]:
    return isinstance(block, TextBlock) and REMINDER_OPEN in block.text


# -- content -------------------------------------------------------------------


def image_block(image: ImageBlock) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": image.media_type, "data": image.data},
    }


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def result_content(
    result: ToolResultBlock, extra_text: list[str] | None = None
) -> list[dict[str, Any]]:
    """A tool result's content as Claude Code records an MCP tool's answer."""
    content: list[dict[str, Any]] = []
    if result.content or not result.images:
        content.append(text_block(result.content))
    content.extend(image_block(image) for image in result.images)
    content.extend(text_block(text) for text in extra_text or ())
    return content


def mcp_result(result: ToolResultBlock, extra_text: list[str] | None = None) -> dict[str, Any]:
    """The MCP ``CallToolResult`` that delivers ``result`` to the CLI."""
    content: list[dict[str, Any]] = []
    if result.content or not result.images:
        content.append({"type": "text", "text": result.content})
    content.extend(
        {"type": "image", "data": image.data, "mimeType": image.media_type}
        for image in result.images
    )
    content.extend({"type": "text", "text": text} for text in extra_text or ())
    return {"content": content, "isError": bool(result.is_error)}


def user_content(message: Message) -> list[dict[str, Any]]:
    """A user message as Anthropic content, tool results first."""
    results: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, ToolResultBlock):
            entry: dict[str, Any] = {
                "tool_use_id": tool_id(block.tool_use_id),
                "type": "tool_result",
                "content": result_content(block),
            }
            if block.is_error:
                entry["is_error"] = True
            results.append(entry)
        elif isinstance(block, TextBlock):
            if block.text:
                rest.append(text_block(block.text))
        elif isinstance(block, ImageBlock):
            rest.append(image_block(block))
    return [*results, *rest]


def prompt_content(message: Message) -> list[dict[str, Any]]:
    """The parts of a user message that go on stdin as a new turn: its text and
    images. Tool results cannot - the CLI drops them from a typed turn."""
    content: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, TextBlock) and block.text:
            content.append(text_block(block.text))
        elif isinstance(block, ImageBlock):
            content.append(image_block(block))
    return content


def encode_payload(messages: list[dict[str, Any]]) -> str:
    """The raw assistant message(s) behind one HX turn, kept for a rebuild.

    Thinking has to be replayed exactly - text and signature - or the API
    rejects it, and HX's own blocks keep only the text.
    """
    return json.dumps({"kind": PAYLOAD_KIND, "v": PAYLOAD_VERSION, "messages": messages})


def decode_payload(raw: str | None) -> list[dict[str, Any]] | None:
    if not raw or not raw.startswith("{"):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("kind") != PAYLOAD_KIND:
        return None
    messages = payload.get("messages")
    if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
        return None
    return messages


def assistant_messages(message: Message, names: ToolNames) -> list[dict[str, Any]]:
    """An HX assistant turn as one or more Anthropic assistant messages.

    The raw messages this route recorded are used when they still describe the
    turn - the same text, the same calls - so a rebuilt session replays the
    thinking and the cache sees the same bytes. Otherwise the turn is rebuilt
    from HX's blocks, without thinking.
    """
    raw = None
    for block in message.content:
        if isinstance(block, ThinkingBlock):
            raw = decode_payload(block.signature)
    if raw and _payload_matches(raw, message, names):
        return raw

    content: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, TextBlock) and block.text:
            content.append(text_block(block.text))
        elif isinstance(block, ToolUseBlock):
            content.append(
                {
                    "type": "tool_use",
                    "id": tool_id(block.id),
                    "name": names.model_name(block.name),
                    "input": block.input,
                }
            )
    if not content:
        return []
    return [{"id": f"msg_hx_{uuid.uuid4().hex[:24]}", "content": content}]


def _payload_matches(raw: list[dict[str, Any]], message: Message, names: ToolNames) -> bool:
    text: list[str] = []
    calls: list[tuple[str, str]] = []
    for entry in raw:
        for block in entry.get("content") or []:
            if not isinstance(block, dict):
                return False
            if block.get("type") == "text":
                text.append(str(block.get("text") or ""))
            elif block.get("type") == "tool_use":
                calls.append((str(block.get("id")), str(block.get("name"))))
    expected = [(tool_id(c.id), names.model_name(c.name)) for c in message.tool_uses()]
    return "".join(text) == message.text() and calls == expected


# -- session files ---------------------------------------------------------------


def config_dir() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(override) if override else Path.home() / ".claude"
    return Path(unicodedata.normalize("NFC", str(base)))


def project_dir(cwd: str) -> Path:
    """Where Claude Code looks for the sessions of a working directory."""
    resolved = os.path.realpath(cwd)
    sanitized = re.sub(r"[^a-zA-Z0-9]", "-", unicodedata.normalize("NFC", resolved))
    projects = config_dir() / "projects"
    if len(sanitized) <= 200:
        return projects / sanitized
    # Long paths get a hash suffix whose function has changed between CLI
    # builds; a directory that already exists for this prefix is the CLI's own.
    prefix = sanitized[:200]
    try:
        for entry in projects.iterdir():
            if entry.is_dir() and entry.name.startswith(prefix + "-"):
                return entry
    except OSError:
        pass
    return projects / f"{prefix}-{_js_hash(resolved)}"


def _js_hash(text: str) -> str:
    value = 0
    for char in text:
        value = ((value << 5) - value + ord(char)) & 0xFFFFFFFF
    if value >= 0x80000000:
        value -= 0x100000000
    value = abs(value)
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while value:
        out = digits[value % 36] + out
        value //= 36
    return out or "0"


@dataclass(slots=True)
class Turn:
    """One API message for a session file."""

    role: str
    content: list[dict[str, Any]]
    message_id: str | None = None
    model: str | None = None


def history_turns(messages: list[Message], names: ToolNames, model: str) -> list[Turn]:
    """HX's transcript as the alternating user/assistant turns of a session.

    Adjacent turns of one role are merged, as the API would have them: HX keeps
    a steer as its own user message after the tool results it follows.
    """
    turns: list[Turn] = []
    for message in messages:
        if message.role == "assistant":
            for raw in assistant_messages(message, names):
                content = [dict(block) for block in raw.get("content") or []]
                if not content:
                    continue
                if turns and turns[-1].role == "assistant":
                    turns[-1].content.extend(_without_thinking(content))
                else:
                    turns.append(
                        Turn(
                            "assistant",
                            content,
                            str(raw.get("id") or ""),
                            raw.get("model") or model,
                        )
                    )
        elif message.role == "user":
            content = user_content(strip_injections(message))
            if not content:
                continue
            if turns and turns[-1].role == "user":
                turns[-1].content.extend(content)
            else:
                turns.append(Turn("user", content))
    if turns and turns[0].role != "user":
        # A transcript cut by compaction can open on the assistant; the API
        # wants a user turn first.
        turns.insert(0, Turn("user", [text_block("(earlier conversation summarised)")]))
    return turns


def _without_thinking(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Thinking is only valid at the start of the message that produced it."""
    return [b for b in content if b.get("type") not in ("thinking", "redacted_thinking")]


def write_session(
    turns: list[Turn], *, cwd: str, cli_version: str = VERSION_TAG
) -> tuple[str, Path]:
    """Write ``turns`` as a session Claude Code can resume. Returns its id and path."""
    session_id = str(uuid.uuid4())
    folder = project_dir(cwd)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{session_id}.jsonl"
    now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    parent: str | None = None
    lines: list[str] = []
    for turn in turns:
        record_id = str(uuid.uuid4())
        record: dict[str, Any] = {
            "parentUuid": parent,
            "isSidechain": False,
            "userType": "external",
            "cwd": os.path.realpath(cwd),
            "sessionId": session_id,
            "version": cli_version,
            "type": turn.role,
            "uuid": record_id,
            "timestamp": now,
        }
        if turn.role == "user":
            record["message"] = {"role": "user", "content": turn.content}
        else:
            record["message"] = {
                "model": turn.model or "",
                "id": turn.message_id or f"msg_hx_{uuid.uuid4().hex[:24]}",
                "type": "message",
                "role": "assistant",
                "content": turn.content,
                "stop_reason": "tool_use"
                if any(b.get("type") == "tool_use" for b in turn.content)
                else "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        lines.append(json.dumps(record, ensure_ascii=False))
        parent = record_id
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return session_id, path
