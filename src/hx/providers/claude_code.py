"""Claude Code provider - a Claude Pro/Max subscription, through the ``claude`` CLI.

HX does not call the Anthropic API here. It runs Claude Code - the official
CLI, signed in to the user's Claude account - and lets it make the calls, the
way the Agent SDK does. What crosses the boundary:

* the model sees HX's tools, served to the CLI as one in-process MCP server
  (:mod:`hx.providers.claude_code_cli`), with every built-in Claude Code tool
  switched off - so every tool call is still run, permission-checked and drawn
  by HX;
* the system prompt is Claude Code's own, with HX's project context, skills
  index and any user additions appended. HX's own base prompt is not sent: it
  describes a different harness, and the CLI's prompt is what the subscription
  expects to see;
* thinking, text and tool calls stream back as the raw Anthropic events.

The awkward part is that Claude Code runs the agent loop and HX runs it too.
HX calls :meth:`ClaudeCodeProvider.astream` once per model turn; the CLI wants
one prompt and then runs tools until the model is done. They meet at the tool
call: when the model asks for a tool, the CLI's ``tools/call`` is held open and
:meth:`astream` ends with ``tool_use``. HX runs the tool and calls
:meth:`astream` again with the result, which answers the held call, and the CLI
carries on streaming. One process serves a conversation for as long as HX's
transcript and the CLI's stay the same.

When they stop being the same - a compaction, an interrupt, a resumed session,
a switch of model or effort - the process is dropped and a new one is started
on a session file rebuilt from HX's transcript (``--resume``). HX's transcript
is always the authority; the CLI's copy is disposable.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from hx.auth import claude_code as cc_auth
from hx.core.context import SYSTEM_PROMPT
from hx.core.messages import Message, StopReason, ToolResultBlock
from hx.core.usage import TurnUsage
from hx.net import with_tls_hint
from hx.providers import claude_code_transcript as transcript
from hx.providers.base import ProviderError, ProviderRequest, StreamDelta, StreamEnd, StreamItem
from hx.providers.claude_code_cli import CliProcess, Exit, ServedTool, ToolServer

PROVIDER_ID = cc_auth.PROVIDER_ID
MODEL_PREFIX = f"{PROVIDER_ID}/"

EffortSource = Callable[[str], str | None]

POOL_SIZE = 4
"""Most processes kept between turns: the main conversation and the subagents
running beside it. Each is a full Claude Code, a couple of hundred megabytes."""

IDLE_KEPT = 1
"""Idle processes kept for a next prompt: the main conversation's."""

CHILD_ENV = {
    # HX's tools must all be in front of the model. With tool search on, the
    # CLI defers MCP tools behind a search tool that is switched off here.
    "ENABLE_TOOL_SEARCH": "false",
    # HX compacts its own transcript. The CLI compacting its copy on its own
    # schedule would leave the two describing different conversations.
    "DISABLE_AUTO_COMPACT": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    # HX caps and spills tool output itself; the CLI's cap is far lower and
    # would cut a result HX already sized.
    "MAX_MCP_OUTPUT_TOKENS": "1000000",
    # No auto-update, telemetry or error reports from a process HX started.
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}

CHILD_SETTINGS = {"autoMemoryEnabled": False, "autoCompactEnabled": False}


class State(StrEnum):
    IDLE = "idle"
    """The model finished its turn; the CLI waits for the next prompt."""
    STREAMING = "streaming"
    """An :meth:`astream` is reading this conversation."""
    PARKED = "parked"
    """The model asked for tools; the CLI waits for HX to run them."""
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class SpawnKey:
    """Everything fixed when a process starts. A request needing anything else
    needs another process."""

    model: str
    effort: str | None
    tools: str
    """Digest of the tool schemas: plan mode and skills narrow the set, and the
    CLI lists a server's tools once."""
    append: str
    """Digest of what HX appends to the system prompt."""
    cwd: str


@dataclass(slots=True)
class Conversation:
    key: SpawnKey
    cli: CliProcess
    names: transcript.ToolNames
    one_shot: bool
    mirror: list[str] = field(default_factory=list)
    """Fingerprints of the HX messages the CLI holds, in order."""
    state: State = State.IDLE
    produced_text: str = ""
    produced_calls: list[str] = field(default_factory=list)
    """What the last streamed turn said and called, to recognise it in HX's
    transcript when the next request arrives."""
    waiting: dict[str, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    """Held ``tools/call`` requests, by tool_use id."""
    answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Results HX delivered before the CLI asked for them."""
    answered: set[str] = field(default_factory=set)
    diverged: bool = False
    """The CLI's copy changed under HX - it answered a call itself, or compacted."""
    cleanup: list[Path] = field(default_factory=list)
    last_used: float = field(default_factory=time.monotonic)

    async def handle_call(
        self, tool_use_id: str, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self.answered.add(tool_use_id)
        ready = self.answers.pop(tool_use_id, None)
        if ready is not None:
            return ready
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.waiting[tool_use_id] = future
        try:
            return await future
        finally:
            self.waiting.pop(tool_use_id, None)

    def deliver(self, tool_use_id: str, result: dict[str, Any]) -> None:
        future = self.waiting.get(tool_use_id)
        if future is not None and not future.done():
            future.set_result(result)
        else:
            self.answers[tool_use_id] = result

    async def close(self) -> None:
        self.state = State.DEAD
        for future in self.waiting.values():
            if not future.done():
                future.cancel()
        await self.cli.close()
        _remove(self.cleanup)
        self.cleanup.clear()


class ClaudeCodeProvider:
    name = PROVIDER_ID

    def __init__(
        self,
        *,
        cwd: Path | str | None = None,
        effort: EffortSource | None = None,
        cli_path: str | None = None,
    ) -> None:
        self.cwd = str(cwd or os.getcwd())
        self.effort: EffortSource = effort if effort is not None else (lambda _m: None)
        self._cli_path = cli_path
        self._pool: list[Conversation] = []
        self._lock = asyncio.Lock()
        self.spawned = 0
        """Processes started so far. One per conversation while transcripts agree."""

    # -- the Provider protocol ---------------------------------------------------

    async def astream(self, request: ProviderRequest) -> AsyncIterator[StreamItem]:
        messages = request.context.messages
        if not messages:
            raise ProviderError("Nothing to send: the conversation is empty.")
        _check_prompt(request)
        key = self._spawn_key(request)
        prints = [transcript.fingerprint(m) for m in messages]

        async with self._lock:
            found = self._continue(key, messages, prints)
            if found is not None:
                found[0].state = State.STREAMING
        if found is not None:
            conversation, held = found
            await self._resume(conversation, messages[-1], held)
        else:
            await self._retire(prints)
            conversation = await self._start(key, request, prints)

        assert conversation is not None
        try:
            async for item in self._stream(conversation, request):
                yield item
        except BaseException:
            # Cancelled mid-stream (Esc, a steer), or the stream failed: the
            # CLI's copy now holds a turn HX's transcript will not, so the
            # process is of no further use.
            await self._drop(conversation)
            raise
        finally:
            conversation.last_used = time.monotonic()
        if conversation.one_shot and conversation.state is State.IDLE:
            await self._drop(conversation)
        else:
            await self._trim()

    async def aclose(self) -> None:
        async with self._lock:
            pool, self._pool = self._pool, []
        for conversation in pool:
            await conversation.close()

    # -- matching a request to a running conversation ----------------------------

    def _continue(
        self, key: SpawnKey, messages: list[Message], prints: list[str]
    ) -> tuple[Conversation, bool] | None:
        """A running conversation that ``messages`` extends by one step, and
        whether it was holding tool calls - or ``None``.

        One step is the turn this provider streamed last, plus either the
        results of the calls it is waiting on (a parked conversation) or one new
        user turn (an idle one). Anything else means the transcripts parted and
        a rebuild is the only safe answer.
        """
        for conversation in sorted(self._pool, key=lambda c: -c.last_used):
            if conversation.state not in (State.IDLE, State.PARKED) or conversation.diverged:
                continue
            if conversation.key != key or not conversation.cli.alive:
                continue
            size = len(conversation.mirror)
            if prints[:size] != conversation.mirror or len(messages) != size + 2:
                continue
            produced, tail = messages[size], messages[size + 1]
            if not self._is_produced(conversation, produced) or tail.role != "user":
                continue
            results = tail.tool_results()
            held = conversation.state is State.PARKED
            if held:
                if [r.tool_use_id for r in results] != conversation.produced_calls:
                    continue
                if any(
                    not isinstance(b, ToolResultBlock) and not transcript.is_reminder(b)
                    for b in tail.content
                ):
                    # A steer folded into the results' message: it has to reach
                    # the model as the user's own turn, which a held call cannot do.
                    continue
            elif results or not transcript.prompt_content(tail):
                continue
            conversation.mirror = prints
            return conversation, held
        return None

    def _is_produced(self, conversation: Conversation, message: Message) -> bool:
        return (
            message.role == "assistant"
            and message.text() == conversation.produced_text
            and [call.id for call in message.tool_uses()] == conversation.produced_calls
        )

    async def _resume(self, conversation: Conversation, tail: Message, held: bool) -> None:
        """Hand the running CLI the step HX took: tool results, or a new prompt."""
        if not held:
            try:
                await conversation.cli.send_user(transcript.prompt_content(tail))
            except BaseException:
                await self._drop(conversation)
                raise
            return
        reminders = [b.text.strip() for b in tail.content if transcript.is_reminder(b)]
        results = tail.tool_results()
        for index, result in enumerate(results):
            # Per-turn reminders ride on the last result: a held call is the
            # only way anything reaches the model before its next reply.
            extra = reminders if index == len(results) - 1 else None
            conversation.deliver(result.tool_use_id, transcript.mcp_result(result, extra))

    # -- starting a process -----------------------------------------------------------

    async def _start(
        self, key: SpawnKey, request: ProviderRequest, prints: list[str]
    ) -> Conversation:
        messages = request.context.messages
        tools = request.context.tools
        names = transcript.ToolNames.for_tools([str(t.get("name")) for t in tools])

        history, prompt = _split_prompt(messages)
        cleanup: list[Path] = []
        argv = self._argv(key, bool(tools))
        if tools:
            # A working session: Claude Code's own prompt, with HX's context.
            flag, system = "--append-system-prompt-file", _appended_prompt(request)
        else:
            # A one-off with no tools - naming a session, summarising one - has
            # its own short prompt, and is spared the agent's seven thousand
            # tokens of tool guidance it has no use for.
            flag, system = "--system-prompt-file", request.context.system_text()
        if system:
            prompt_file = _private_file(system)
            cleanup.append(prompt_file)
            argv += [flag, str(prompt_file)]
        turns = transcript.history_turns(history, names, _wire_model(key.model))
        if turns:
            session_id, path = transcript.write_session(turns, cwd=key.cwd)
            cleanup.append(path)
            argv += ["--resume", session_id]

        server: ToolServer | None = None
        if tools:
            server = ToolServer(
                tools=[
                    ServedTool(
                        name=names.add(str(tool.get("name"))),
                        description=str(tool.get("description") or ""),
                        input_schema=_object_schema(
                            tool.get("input_schema") or tool.get("parameters")
                        ),
                    )
                    for tool in tools
                ],
                handler=_no_tools,
            )
        try:
            cli = await CliProcess.spawn(
                argv, cwd=key.cwd, env=cc_auth.child_env(CHILD_ENV), server=server
            )
        except BaseException:
            _remove(cleanup)
            raise
        self.spawned += 1
        conversation = Conversation(
            key=key,
            cli=cli,
            names=names,
            one_shot=not tools,
            mirror=prints,
            state=State.STREAMING,
            cleanup=cleanup,
        )
        if server is not None:
            # No call can arrive before the prompt is sent below.
            server.handler = conversation.handle_call
        async with self._lock:
            self._pool.append(conversation)

        try:
            await cli.initialize()
            # Read by now: the CLI loads its session and prompt file before it
            # answers initialize, and nothing else should ever see them.
            _remove(conversation.cleanup)
            conversation.cleanup.clear()
            await cli.send_user(prompt)
        except BaseException as exc:
            await self._drop(conversation)
            if isinstance(exc, ProviderError):
                raise ProviderError(_startup_failure(str(exc), cli)) from exc
            raise
        return conversation

    def _argv(self, key: SpawnKey, with_tools: bool) -> list[str]:
        cli = self._cli_path or cc_auth.find_cli()
        if cli is None:
            raise ProviderError(cc_auth.missing_message())
        argv = [
            cli,
            "-p",
            "--output-format",
            "stream-json",
            "--input-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model",
            _wire_model(key.model),
            # No built-in tools: every tool the model can call is HX's.
            "--tools",
            "",
            # No user or project settings: their hooks, permission rules and
            # CLAUDE.md belong to Claude Code sessions, not HX's.
            "--setting-sources",
            "",
            "--disable-slash-commands",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--settings",
            json.dumps(CHILD_SETTINGS),
        ]
        if with_tools:
            argv += [
                "--mcp-config",
                json.dumps({"mcpServers": {"hx": {"type": "sdk", "name": "hx"}}}),
                # HX asks for permission before it runs anything; the CLI must
                # not ask again, or refuse what HX already allowed.
                "--allowedTools",
                "mcp__hx",
            ]
        if key.effort:
            argv += ["--effort", key.effort]
        return argv

    def _spawn_key(self, request: ProviderRequest) -> SpawnKey:
        tools = json.dumps(request.context.tools, sort_keys=True)
        return SpawnKey(
            model=request.model,
            effort=self.effort(request.model),
            tools=hashlib.sha256(tools.encode()).hexdigest(),
            append=hashlib.sha256(_appended_prompt(request).encode()).hexdigest(),
            cwd=self.cwd,
        )

    # -- reading a turn ----------------------------------------------------------------

    async def _stream(
        self, conversation: Conversation, request: ProviderRequest
    ) -> AsyncIterator[StreamItem]:
        turn = _Turn(conversation.names)
        started = time.monotonic()
        frames = conversation.cli.frames
        while True:
            frame = await frames.get()
            if isinstance(frame, Exit):
                conversation.state = State.DEAD
                raise ProviderError(_exit_message(frame, turn.rate_limit))
            kind = frame.get("type")
            if kind == "stream_event" and frame.get("parent_tool_use_id") is None:
                for item in turn.consume(frame.get("event") or {}):
                    yield item
                if turn.parked and turn.calls:
                    conversation.state = State.PARKED
                    self._remember(conversation, turn)
                    payload = from_payload(turn)
                    if payload is not None:
                        yield payload
                    yield StreamEnd(StopReason.TOOL_USE, turn.finish_usage(started))
                    return
            elif kind == "user":
                self._note_results(conversation, frame)
            elif kind == "system" and frame.get("subtype") == "compact_boundary":
                conversation.diverged = True
            elif kind == "rate_limit_event":
                info = frame.get("rate_limit_info") or {}
                if info.get("status") == "rejected":
                    turn.rate_limit = info
            elif kind == "result":
                if frame.get("is_error"):
                    conversation.diverged = True
                    raise ProviderError(_result_error(frame, turn.rate_limit, request))
                conversation.state = State.IDLE
                self._remember(conversation, turn)
                payload = from_payload(turn)
                if payload is not None:
                    yield payload
                yield StreamEnd(turn.stop_reason(), turn.finish_usage(started))
                return

    def _remember(self, conversation: Conversation, turn: _Turn) -> None:
        conversation.produced_text = turn.text
        conversation.produced_calls = list(turn.calls)

    def _note_results(self, conversation: Conversation, frame: dict[str, Any]) -> None:
        """A tool result the CLI recorded. One HX did not supply means the CLI
        answered a call itself - rejected its input, say - and its copy of the
        conversation no longer matches HX's."""
        content = (frame.get("message") or {}).get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                call_id = block.get("tool_use_id")
                if call_id in conversation.answered:
                    conversation.answered.discard(call_id)
                else:
                    conversation.diverged = True

    # -- the pool ----------------------------------------------------------------------

    async def _retire(self, prints: list[str]) -> None:
        """Stop the processes a rebuild of this transcript replaces.

        A conversation that holds any of the same messages and is not streaming
        is this transcript's earlier copy - left behind by an interrupt, a steer
        or a compaction, which keeps the latest turns as they were - and would
        otherwise sit holding calls nobody will answer. Retiring one wrongly
        costs a rebuild later, never a wrong answer.
        """
        current = set(prints)
        async with self._lock:
            stale = [
                c
                for c in self._pool
                if c.state is not State.STREAMING and current.intersection(c.mirror)
            ]
            for conversation in stale:
                self._pool.remove(conversation)
        for conversation in stale:
            await conversation.close()

    async def _drop(self, conversation: Conversation) -> None:
        async with self._lock:
            if conversation in self._pool:
                self._pool.remove(conversation)
        await conversation.close()

    async def _trim(self) -> None:
        """Stop processes that cannot be used again, or are not worth their memory.

        Each is a full Claude Code, a couple of hundred megabytes. A parked one
        is mid-turn and kept. Of the idle ones only the most recently used is:
        that is the conversation the user's next prompt continues, and the rest
        are finished subagents or conversations left behind.
        """
        async with self._lock:
            unusable = [
                c
                for c in self._pool
                if c.state is State.DEAD
                or not c.cli.alive
                or (c.diverged and c.state is not State.STREAMING)
            ]
            live = [c for c in self._pool if c not in unusable]
            idle = sorted((c for c in live if c.state is State.IDLE), key=lambda c: c.last_used)
            surplus = idle[:-IDLE_KEPT] if len(idle) > IDLE_KEPT else []
            keep = [c for c in live if c not in surplus]
            parked = sorted((c for c in keep if c.state is State.PARKED), key=lambda c: c.last_used)
            surplus += parked[: max(0, len(keep) - POOL_SIZE)]
            stale = [*unusable, *surplus]
            for conversation in stale:
                self._pool.remove(conversation)
        for conversation in stale:
            await conversation.close()


class _Turn:
    """One model turn, assembled from raw Anthropic stream events.

    A turn is usually one API message. The CLI can make more than one inside a
    turn - retrying a stream that broke, or continuing after an output-token
    limit - and their text and calls are all this turn's.
    """

    def __init__(self, names: transcript.ToolNames) -> None:
        self.names = names
        self.messages: list[dict[str, Any]] = []
        self.blocks: dict[int, dict[str, Any]] = {}
        self.json: dict[int, list[str]] = {}
        self.text = ""
        self.calls: list[str] = []
        self.parked = False
        self.stop = ""
        self.usage = TurnUsage()
        self.outputs: dict[int, int] = {}
        """Output tokens per API message; ``message_delta`` reports a running total."""
        self.rate_limit: dict[str, Any] | None = None

    def consume(self, event: dict[str, Any]) -> list[StreamItem]:
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") or {}
            self.messages.append(
                {"id": message.get("id"), "model": message.get("model"), "content": []}
            )
            self.blocks = {}
            self.json = {}
            self._add_usage(message.get("usage") or {}, prompt=True)
            return []
        if kind == "content_block_start":
            index = int(event.get("index") or 0)
            block = dict(event.get("content_block") or {})
            block.pop("caller", None)
            self.blocks[index] = block
            if block.get("type") == "tool_use":
                self.json[index] = []
            return []
        if kind == "content_block_delta":
            return self._delta(int(event.get("index") or 0), event.get("delta") or {})
        if kind == "content_block_stop":
            return self._stop_block(int(event.get("index") or 0))
        if kind == "message_delta":
            delta = event.get("delta") or {}
            self.stop = str(delta.get("stop_reason") or self.stop)
            self._add_usage(event.get("usage") or {}, prompt=False)
            return []
        if kind == "message_stop":
            # Parked only when HX has calls to run: a ``tool_use`` stop with
            # none of HX's tools in it is the CLI's to resolve.
            self.parked = self.stop == "tool_use"
            return []
        return []

    def _delta(self, index: int, delta: dict[str, Any]) -> list[StreamItem]:
        block = self.blocks.get(index)
        if block is None:
            return []
        kind = delta.get("type")
        if kind == "text_delta":
            text = str(delta.get("text") or "")
            block["text"] = block.get("text", "") + text
            self.text += text
            return [StreamDelta(text=text)] if text else []
        if kind == "thinking_delta":
            text = str(delta.get("thinking") or "")
            block["thinking"] = block.get("thinking", "") + text
            return [StreamDelta(thinking=text)] if text else []
        if kind == "signature_delta":
            block["signature"] = block.get("signature", "") + str(delta.get("signature") or "")
            return []
        if kind == "input_json_delta" and index in self.json:
            self.json[index].append(str(delta.get("partial_json") or ""))
        return []

    def _stop_block(self, index: int) -> list[StreamItem]:
        block = self.blocks.get(index)
        if block is None or not self.messages:
            return []
        if block.get("type") == "tool_use":
            raw = "".join(self.json.pop(index, []))
            try:
                block["input"] = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                block["input"] = {}
        self.messages[-1]["content"].append(block)
        if block.get("type") != "tool_use":
            return []
        name = self.names.from_model(str(block.get("name") or ""))
        if name is None:
            return []
        call_id = str(block.get("id"))
        self.calls.append(call_id)
        return [
            StreamDelta(
                tool_use_id=call_id,
                tool_name=name,
                tool_input_json=json.dumps(block["input"]),
            )
        ]

    def _add_usage(self, usage: dict[str, Any], *, prompt: bool) -> None:
        if prompt:
            created = int(usage.get("cache_creation_input_tokens") or 0)
            self.usage.input_tokens += int(usage.get("input_tokens") or 0) + created
            self.usage.cache_write_tokens += created
            self.usage.cache_read_tokens += int(usage.get("cache_read_input_tokens") or 0)
        output = int(usage.get("output_tokens") or 0)
        if not prompt and output:
            self.outputs[len(self.messages)] = output
            self.usage.output_tokens = sum(self.outputs.values())

    def stop_reason(self) -> StopReason:
        return {
            "max_tokens": StopReason.MAX_TOKENS,
            "stop_sequence": StopReason.STOP_SEQUENCE,
        }.get(self.stop, StopReason.END_TURN)

    def finish_usage(self, started: float) -> TurnUsage:
        self.usage.latency_ms = (time.monotonic() - started) * 1000
        # A subscription: nothing is charged per token.
        self.usage.cost_usd = 0.0
        return self.usage


def from_payload(turn: _Turn) -> StreamDelta | None:
    """The turn's raw messages, carried on HX's thinking block for a rebuild."""
    if not turn.messages:
        return None
    return StreamDelta(thinking_signature=transcript.encode_payload(turn.messages))


# -- helpers ---------------------------------------------------------------------------


def _split_prompt(messages: list[Message]) -> tuple[list[Message], list[dict[str, Any]]]:
    """``(history for the session file, content of the turn to send)``.

    The last user message is sent as the prompt when it can be: its text and
    images. Tool results cannot travel that way, so they stay in the history
    and Claude Code's own resume prompt starts the next request.
    """
    last = messages[-1]
    if last.role != "user":
        return messages, [transcript.text_block(transcript.RESUME_PROMPT)]
    if not last.tool_results():
        return messages[:-1], transcript.prompt_content(last)
    reminders = [b for b in last.content if transcript.is_reminder(b)]
    results_only = Message(
        role="user",
        content=[b for b in last.content if isinstance(b, ToolResultBlock)],
    )
    rest = Message(
        role="user",
        content=[
            b for b in last.content if not isinstance(b, ToolResultBlock) and b not in reminders
        ],
    )
    prompt = transcript.prompt_content(rest) or [transcript.text_block(transcript.RESUME_PROMPT)]
    prompt += [transcript.text_block(b.text) for b in reminders]
    return [*messages[:-1], results_only], prompt


def _appended_prompt(request: ProviderRequest) -> str:
    """What HX adds to Claude Code's system prompt."""
    return "\n\n".join(text for _, text in _appended_sections(request))


def _appended_sections(request: ProviderRequest) -> list[tuple[str, str]]:
    """What HX adds to Claude Code's system prompt, as ``(section, text)``.

    The project context (AGENTS.md files) and skills index always; the base
    prompt only where the user wrote their own or appended to HX's.
    """
    parts: list[tuple[str, str]] = []
    builtin = SYSTEM_PROMPT.strip()
    for section in request.context.system:
        text = section.text.strip()
        if section.name == "system" and text.startswith(builtin):
            text = text[len(builtin) :].strip()
        if text:
            parts.append((section.name, text))
    return parts


def _sent_sections(request: ProviderRequest) -> list[tuple[str, str]]:
    """HX's part of the system prompt Claude Code will send: appended to Claude
    Code's own in a working session, the whole of it in a one-off."""
    if request.context.tools:
        return _appended_sections(request)
    return [(s.name, s.text.strip()) for s in request.context.system if s.text.strip()]


HX_PROMPT_PARAGRAPHS = tuple(
    " ".join(paragraph.split()) for paragraph in SYSTEM_PROMPT.split("\n\n") if paragraph.strip()
)
"""HX's base prompt, a paragraph at a time and with whitespace flattened, so a
copy that was rewrapped or had other paragraphs edited is still recognised."""

SECTION_NAMES = {
    "system": "the system prompt",
    "skills": "the skills index",
    "project": "the project context",
}

SECTION_SOURCES = {
    "system": (
        "`hx prompt` shows where yours comes from; a subagent's is its definition "
        "in an agents folder"
    ),
    "skills": "it comes from a skill's description",
    "project": "it comes from an AGENTS.md file, which `hx prompt` lists",
}


def _check_prompt(request: ProviderRequest) -> None:
    """Refuse a turn whose system prompt carries HX's own.

    HX's base prompt describes HX's harness. This route sends Claude Code's
    prompt in its place, and Anthropic decides from a request's content whether
    a subscription covers it: another harness's prompt beside Claude Code's can
    get the turn billed as Extra Usage, which fails outright once that is spent.
    The built-in prompt is already left out; this catches it arriving another
    way - an override copied from it, an AGENTS.md quoting it, a subagent
    definition that pastes it in front of its role.
    """
    for name, text in _sent_sections(request):
        flat = " ".join(text.split())
        for paragraph in HX_PROMPT_PARAGRAPHS:
            if paragraph in flat:
                where = SECTION_NAMES.get(name, f"the {name} section")
                source = SECTION_SOURCES.get(name, "")
                snippet = " ".join(paragraph.split()[:6])
                raise ProviderError(
                    f'HX\'s own system prompt is in {where} ("{snippet}…"), so this turn was '
                    "not sent. On the Claude Code route the model gets Claude Code's prompt "
                    "instead, and another harness's prompt beside it can get the turn billed "
                    "as Extra Usage rather than your plan. Remove that text"
                    + (f" - {source}." if source else ".")
                )


def _object_schema(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict) and raw.get("type") == "object":
        return raw
    return {"type": "object", "properties": {}}


def _wire_model(model_id: str) -> str:
    return model_id[len(MODEL_PREFIX) :] if model_id.startswith(MODEL_PREFIX) else model_id


def _private_file(text: str) -> Path:
    fd, name = tempfile.mkstemp(prefix="hx-claude-", suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return Path(name)


def _remove(paths: list[Path]) -> None:
    for path in paths:
        with contextlib.suppress(OSError):
            path.unlink()
        if path.suffix == ".jsonl":
            # The project folder HX may have created for it: gone too when
            # nothing else is in it, and left alone when Claude Code's own
            # sessions are.
            with contextlib.suppress(OSError):
                path.parent.rmdir()


async def _no_tools(tool_use_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"{name} is not available."}], "isError": True}


def _startup_failure(message: str, cli: CliProcess) -> str:
    stderr = cli.stderr_tail()
    if "No conversation found" in stderr or "No conversation found" in message:
        return (
            "Claude Code could not load the conversation HX rebuilt for it. "
            "Start a new session with /clear, and report this with `hx --version`."
        )
    return with_tls_hint(f"Claude Code failed to start: {message}")


def _exit_message(frame: Exit, rate_limit: dict[str, Any] | None) -> str:
    if rate_limit:
        return _limit_message(rate_limit)
    detail = frame.stderr.strip()
    if "not logged in" in detail.lower() or "/login" in detail:
        return cc_auth.missing_message()
    suffix = f": {detail[-1200:]}" if detail else ""
    return with_tls_hint(f"Claude Code exited unexpectedly (code {frame.code}){suffix}")


def _result_error(
    frame: dict[str, Any], rate_limit: dict[str, Any] | None, request: ProviderRequest
) -> str:
    errors = frame.get("errors")
    text = "; ".join(str(e) for e in errors) if isinstance(errors, list) and errors else ""
    text = text or str(frame.get("result") or frame.get("subtype") or "unknown error")
    if _billed_as_extra_usage(frame, text, rate_limit):
        return _extra_usage_message(request, text)
    if rate_limit or "usage limit" in text.lower() or "out of extra usage" in text.lower():
        return _limit_message(rate_limit or {}, text)
    if "authentication" in text.lower() or "/login" in text:
        return f"{text.strip()}. {cc_auth.missing_message()}"
    return with_tls_hint(f"Claude Code: {text.strip()}")


def _billed_as_extra_usage(
    frame: dict[str, Any], text: str, rate_limit: dict[str, Any] | None
) -> bool:
    """A turn refused as Extra Usage while the plan itself had room.

    Two failures share the words "out of extra usage". A plan whose limit is
    reached, with Extra Usage spent too, comes with a rejected rate limit and
    says when it resets. This one is a 400 before the model runs, with the plan
    allowed: Anthropic counted the request as Extra Usage from what it carries,
    so it fails the same way however often it is sent.
    """
    if rate_limit:
        return False
    lowered = text.lower()
    status = frame.get("api_error_status")
    return "out of extra usage" in lowered and (status == 400 or "api error: 400" in lowered)


def _extra_usage_message(request: ProviderRequest, detail: str) -> str:
    names = [SECTION_NAMES[name] for name, _ in _sent_sections(request) if name in SECTION_NAMES]
    ours = f" HX's part of the system prompt: {', '.join(names)}." if names else ""
    return (
        "Claude billed this turn as Extra Usage instead of your plan, and none is left - "
        "your plan's own limits are not reached. Anthropic decides this from what the "
        "request carries, so sending it again fails the same way."
        f"{ours} Text there that describes another coding agent is a known cause; remove "
        f"it, or add Extra Usage. ({detail.strip()})"
    )


def _limit_message(info: dict[str, Any], detail: str = "") -> str:
    resets = info.get("resetsAt")
    when = ""
    if isinstance(resets, int | float) and resets > 0:
        when = " It resets at " + time.strftime("%H:%M on %b %d", time.localtime(resets)) + "."
    window = str(info.get("rateLimitType") or "").replace("_", " ")
    base = (
        f"Your Claude subscription's {window} usage limit is reached."
        if window
        else ("Your Claude subscription's usage limit is reached.")
    )
    extra = f" ({detail.strip()})" if detail.strip() else ""
    return base + when + extra
