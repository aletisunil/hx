"""The agent turn engine.

One iteration: assemble context -> stream from the provider -> collect tool_use
blocks -> execute them -> append results -> repeat, until the model returns a
turn with no tool calls or the user cancels.

Read-only tools in the same assistant turn run concurrently; anything that
mutates state runs serially in the order the model emitted it, so side effects
stay predictable.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from hx.core.context import AssembledContext
from hx.core.events import (
    CompactionFinished,
    CompactionStarted,
    ErrorRaised,
    PermissionRequested,
    TextDelta,
    ThinkingDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
    UsageUpdated,
)
from hx.core.messages import (
    ContentBlock,
    Message,
    StopReason,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    assistant_message,
    tool_result_message,
    user_message,
)
from hx.core.usage import TurnUsage, compute_cost
from hx.providers.base import ProviderError, ProviderRequest, StreamDelta, StreamEnd
from hx.tools.base import ToolContext

if TYPE_CHECKING:
    from hx.config import Settings
    from hx.core.compaction import Compactor
    from hx.core.context import ContextBuilder
    from hx.core.events import EventBus
    from hx.core.lateinject import InjectionRegistry
    from hx.core.session import Session
    from hx.permissions.engine import PermissionEngine
    from hx.providers.base import Provider
    from hx.providers.models import ModelInfo
    from hx.skills.runtime import ActiveSkills
    from hx.tools.registry import ToolRegistry


@dataclass(slots=True)
class TurnResult:
    stop_reason: StopReason
    messages: list[Message]
    error: str | None = None


class AgentLoop:
    """Drives one conversation. Subagents each get their own instance."""

    MAX_TURNS: ClassVar[int] = 100
    """Backstop against a model that calls tools forever. Hitting it is a bug
    worth surfacing, not a condition to handle silently."""

    def __init__(
        self,
        *,
        provider: Provider,
        session: Session,
        tools: ToolRegistry,
        permissions: PermissionEngine | None,
        context: ContextBuilder,
        compactor: Compactor | None,
        injections: InjectionRegistry,
        bus: EventBus,
        settings: Settings,
        model_info: ModelInfo | None = None,
        active_skills: ActiveSkills | None = None,
        skills_index: str | None = None,
        project_context: str | None = None,
    ) -> None:
        self.provider = provider
        self.session = session
        self.tools = tools
        self.permissions = permissions
        self.context = context
        self.compactor = compactor
        self.injections = injections
        self.bus = bus
        self.settings = settings
        self.model_info = model_info
        self.active_skills = active_skills
        self.skills_index = skills_index
        self.project_context = project_context
        self._cancelled = False
        self._turn_index = 0
        self.last_context: AssembledContext | None = None
        self.origin: str | None = None
        """Set for subagents so approval prompts name who is asking."""

    @property
    def model(self) -> str:
        return self.session.meta.model

    def set_provider(self, provider: Provider) -> Provider:
        """Swap the provider mid-session, returning the one replaced.

        Switching to a model on a different route changes which service is
        called, not just which model. The compactor holds its own reference and
        would otherwise keep summarising through the old, possibly now
        unauthenticated, connection.

        The caller owns closing the returned provider.
        """
        previous = self.provider
        self.provider = provider
        if self.compactor is not None:
            self.compactor.provider = provider
        return previous

    def set_model(self, model_id: str, model_info: ModelInfo | None = None) -> None:
        self.session.meta.model = model_id
        self.model_info = model_info

    async def run(self, user_input: str) -> TurnResult:
        """Run turns until the model stops calling tools.

        Cancellation (Esc / Ctrl+C) raises ``asyncio.CancelledError`` into this
        coroutine; the partial assistant message is still appended to the
        transcript so the next turn has an honest history.
        """
        self._cancelled = False
        if user_input:
            self.session.append(user_message(user_input))

        produced: list[Message] = []
        stop_reason = StopReason.END_TURN

        for _ in range(self.MAX_TURNS):
            if self._cancelled:
                return TurnResult(StopReason.CANCELLED, produced)

            await self._maybe_compact()
            self._turn_index += 1
            if self.origin is None:
                self.bus.publish(TurnStarted(turn_index=self._turn_index, model=self.model))

            try:
                message, stop_reason = await self._stream_turn()
            except asyncio.CancelledError:
                self.bus.publish(TurnFinished(self._turn_index, StopReason.CANCELLED))
                raise
            except ProviderError as exc:
                self.bus.publish(ErrorRaised(message=str(exc), recoverable=True))
                self.bus.publish(TurnFinished(self._turn_index, StopReason.ERROR))
                return TurnResult(StopReason.ERROR, produced, error=str(exc))

            self.session.append(message)
            produced.append(message)
            if self.origin is None:
                self.bus.publish(TurnFinished(self._turn_index, stop_reason))

            calls = message.tool_uses()
            if not calls or self._cancelled:
                await self._name_session()
                return TurnResult(stop_reason, produced)

            results = await self._execute_tools(calls)
            result_message = tool_result_message(results)
            self.session.append(result_message)
            produced.append(result_message)

        self.bus.publish(
            ErrorRaised(message=f"Stopped after {self.MAX_TURNS} turns", recoverable=True)
        )
        return TurnResult(stop_reason, produced, error="turn limit reached")

    async def _name_session(self) -> None:
        """Give the session a title once, after its first completed exchange.

        Awaited rather than detached: ``TurnFinished`` has already been
        published so the UI is idle, and in print mode the process exits the
        moment ``run`` returns - a background task would simply be killed.

        Naming must never cost the user a turn, so every failure falls back to
        the first thing they said.
        """
        if self.origin is not None or self.session.meta.title or self._cancelled:
            return

        messages = [m for m in self.session.messages if not m.ephemeral]
        if not any(m.role == "user" for m in messages):
            return

        from hx.core.title import fallback_title, generate_title

        title: str | None = None
        try:
            model = self.settings.models.title_model or self.model
            title, usage = await generate_title(self.provider, model, messages)
            if usage.prompt_tokens or usage.output_tokens:
                self._record_usage(usage, None)
        except asyncio.CancelledError:
            raise
        except Exception:
            title = None

        self.session.set_title(title or fallback_title(messages))

    async def _stream_turn(self) -> tuple[Message, StopReason]:
        """One provider call. Publishes deltas and the usage update."""
        request = await self._build_request()

        text_parts: list[str] = []
        thinking_parts: list[str] = []
        thinking_signature: str | None = None
        tool_calls: list[ToolUseBlock] = []
        stop_reason = StopReason.END_TURN
        usage = TurnUsage()
        started = time.monotonic()

        try:
            async for item in self.provider.astream(request):
                if isinstance(item, StreamEnd):
                    stop_reason = item.stop_reason
                    usage = item.usage
                    continue
                if not isinstance(item, StreamDelta):  # pragma: no cover - defensive
                    continue
                if item.text:
                    text_parts.append(item.text)
                    # A subagent's prose is not the assistant speaking to the
                    # user: it is an intermediate result that reaches them as
                    # the Task tool's output. Streaming it into the transcript
                    # would read as if the main assistant had said it.
                    if self.origin is None:
                        self.bus.publish(TextDelta(text=item.text))
                if item.thinking:
                    thinking_parts.append(item.thinking)
                    if self.origin is None:
                        self.bus.publish(ThinkingDelta(text=item.thinking))
                if item.thinking_signature:
                    thinking_signature = item.thinking_signature
                if item.tool_use_id and item.tool_name:
                    tool_calls.append(
                        ToolUseBlock(
                            id=item.tool_use_id,
                            name=item.tool_name,
                            input=_parse_tool_input(item.tool_input_json),
                        )
                    )
        except asyncio.CancelledError:
            # Keep whatever streamed before the interrupt: the next turn must
            # reflect what the user actually saw.
            if text_parts or tool_calls:
                self.session.append(
                    self._assemble(text_parts, thinking_parts, [], thinking_signature)
                )
            raise

        if not usage.latency_ms:
            usage.latency_ms = (time.monotonic() - started) * 1000
        self._record_usage(usage, request)

        return (
            self._assemble(text_parts, thinking_parts, tool_calls, thinking_signature),
            stop_reason,
        )

    async def _build_request(self) -> ProviderRequest:
        messages = await self.injections.apply(self.session.active_messages())
        cache_mode = self.model_info.cache_mode.value if self.model_info else "none"
        assembled = self.context.build(
            messages=messages,
            tools=self.tools.schemas(self._allowed_tools()),
            skills_index=self.skills_index,
            project_context=self.project_context,
            cache_mode=cache_mode,
        )
        self.last_context = assembled
        self.session.usage.context_tokens = assembled.total_tokens
        self.session.usage.context_window = self.model_info.context_window if self.model_info else 0
        return ProviderRequest(
            context=assembled,
            model=self.model,
            max_tokens=self.settings.models.max_tokens,
            temperature=self.settings.models.temperature,
        )

    def _allowed_tools(self) -> set[str] | None:
        """Which tools the model may see this turn.

        A loaded skill's ``allowed-tools`` narrows the set - it is intersected,
        never unioned, so a skill can only ever restrict. Without this the
        Skill tool's own "use only these tools" line would be a claim with
        nothing behind it.
        """
        allowed: set[str] | None = None
        if self.permissions is not None:
            mutating = {name for name in self.tools.names() if self.tools.get(name).mutating}
            allowed = self.permissions.allowed_tools(self.tools.names(), mutating)

        if self.active_skills is not None and (
            skill_allowed := self.active_skills.tool_allowlist()
        ):
            # Skill and Task stay reachable so the model can switch skills or
            # delegate; everything else narrows to the skill's list.
            keep = skill_allowed | {"Skill", "Task"}
            allowed = keep if allowed is None else (allowed & keep)
        return allowed

    def _assemble(
        self,
        text_parts: list[str],
        thinking_parts: list[str],
        tool_calls: list[ToolUseBlock],
        thinking_signature: str | None = None,
    ) -> Message:
        blocks: list[ContentBlock] = []
        if thinking_parts or thinking_signature:
            blocks.append(ThinkingBlock(text="".join(thinking_parts), signature=thinking_signature))
        if text_parts:
            blocks.append(TextBlock(text="".join(text_parts)))
        blocks.extend(tool_calls)
        return assistant_message(blocks, model=self.model)

    def _record_usage(self, usage: TurnUsage, request: ProviderRequest | None) -> None:
        if usage.cost_usd is None and self.model_info is not None:
            usage.cost_usd = compute_cost(usage, self.model_info.pricing)
        self.session.record_usage(usage)
        ledger = self.session.usage
        self.bus.publish(
            UsageUpdated(
                input_tokens=ledger.total_input,
                output_tokens=ledger.total_output,
                cache_read_tokens=ledger.total_cache_read,
                cache_write_tokens=ledger.total_cache_write,
                context_tokens=ledger.context_tokens,
                context_window=ledger.context_window,
                cost_usd=ledger.total_cost_usd,
            )
        )

    async def _execute_tools(self, calls: list[ToolUseBlock]) -> list[ToolResultBlock]:
        """Permission-check, then run. Read-only calls are gathered concurrently;
        mutating calls run in emission order.

        A denied call becomes an ``is_error`` result rather than an exception, so
        the model can react instead of the turn dying.
        """
        results: dict[str, ToolResultBlock] = {}
        concurrent: list[ToolUseBlock] = []

        for call in calls:
            if self._runs_serially(call):
                for pending in concurrent:
                    results[pending.id] = await self._run_one(pending)
                concurrent.clear()
                results[call.id] = await self._run_one(call)
            else:
                concurrent.append(call)

        if concurrent:
            gathered = await asyncio.gather(*(self._run_one(c) for c in concurrent))
            for call, result in zip(concurrent, gathered, strict=True):
                results[call.id] = result

        return [results[c.id] for c in calls]

    def _runs_serially(self, call: ToolUseBlock) -> bool:
        try:
            tool = self.tools.get(call.name)
        except Exception:
            # An unknown tool is about to become an error result; treat it as
            # serial so it cannot slip into the concurrent batch.
            return True
        return tool.mutating and not tool.parallel_safe

    def _display_name(self, name: str) -> str:
        """Attribute a subagent's tool calls so they do not read as the parent's."""
        return name if self.origin is None else f"{self.origin} > {name}"

    async def _run_one(self, call: ToolUseBlock) -> ToolResultBlock:
        self.bus.publish(
            ToolCallStarted(
                tool_use_id=call.id, name=self._display_name(call.name), input=call.input
            )
        )
        started = time.monotonic()

        denied = await self._check_permission(call)
        if denied is not None:
            self.bus.publish(
                ToolCallFinished(
                    tool_use_id=call.id,
                    is_error=True,
                    duration_ms=(time.monotonic() - started) * 1000,
                    summary="denied",
                    detail=denied.content,
                )
            )
            return denied

        ctx = ToolContext(
            cwd=self.settings.cwd,
            session_id=self.session.meta.session_id,
            tool_use_id=call.id,
            settings=self.settings,
            emit_progress=lambda chunk: self._emit_progress(call.id, chunk),
        )
        result = await self.tools.call(call.name, call.input, ctx)

        self.bus.publish(
            ToolCallFinished(
                tool_use_id=call.id,
                is_error=result.is_error,
                duration_ms=(time.monotonic() - started) * 1000,
                summary=result.summary or ("error" if result.is_error else "done"),
                detail=result.content if result.is_error else "",
                metadata=dict(result.metadata),
            )
        )
        return ToolResultBlock(
            tool_use_id=call.id,
            content=result.content,
            is_error=result.is_error,
            spilled_path=result.spilled_path,
        )

    async def _check_permission(self, call: ToolUseBlock) -> ToolResultBlock | None:
        """Returns a denial result, or ``None`` when the call may proceed."""
        if self.permissions is None or not self.tools.has(call.name):
            return None

        from hx.permissions.engine import PermissionRequest

        tool = self.tools.get(call.name)
        request = PermissionRequest(
            tool_name=call.name,
            specifier=tool.permission_specifier(call.input),
            params=call.input,
            mutating=tool.mutating,
            description=f"{call.name}({_brief(call.input)})",
            detail=_permission_detail(call),
            origin=self.origin,
        )
        allowed, reason = await self.permissions.request(
            request, on_ask=lambda: self._announce_ask(call.id, request)
        )
        if allowed:
            return None
        return ToolResultBlock(
            tool_use_id=call.id,
            content=f"{call.name} was not permitted: {reason}",
            is_error=True,
        )

    def _announce_ask(self, call_id: str, request: Any) -> None:
        """Announce only the calls that actually stop for approval.

        Publishing on every check made the headless renderer print a permission
        line for calls that were auto-allowed and never asked about.
        """
        self.bus.publish(
            PermissionRequested(
                request_id=call_id,
                tool_name=request.tool_name,
                description=request.description,
                detail=request.detail,
            )
        )

    def _emit_progress(self, tool_use_id: str, chunk: str) -> None:
        from hx.core.events import ToolCallProgress

        self.bus.publish(ToolCallProgress(tool_use_id=tool_use_id, chunk=chunk))

    async def _maybe_compact(self) -> None:
        """Compact if the context fraction crossed the configured threshold."""
        if self.compactor is None:
            return
        fraction = self.session.usage.context_fraction
        if not self.compactor.should_compact(fraction, self.settings.context.compact_at):
            return
        await self.compact(reason=f"context at {fraction:.0%} of the window")

    async def compact(self, instructions: str | None = None, reason: str = "requested") -> bool:
        """Replace older turns with a summary. Returns False when there was
        nothing worth compacting.

        Everything below the static prefix changes, so the cached conversation
        is discarded by definition. That is announced rather than done quietly:
        the next turn re-reads its input at full price.
        """
        if self.compactor is None:
            return False

        self.bus.publish(CompactionStarted(reason=reason))
        try:
            result = await self.compactor.compact(self.session.active_messages(), instructions)
        except ProviderError as exc:
            self.bus.publish(ErrorRaised(message=f"Compaction failed: {exc}", recoverable=True))
            return False

        if not result.dropped:
            self.bus.publish(
                CompactionFinished(
                    tokens_before=result.tokens_before, tokens_after=result.tokens_after
                )
            )
            return False

        self.session.record_compaction(result.dropped, result.kept, result.summary)
        self.session.usage.context_tokens = result.tokens_after
        self.bus.publish(
            CompactionFinished(tokens_before=result.tokens_before, tokens_after=result.tokens_after)
        )
        return True

    def cancel(self) -> None:
        """Request cancellation of the in-flight turn."""
        self._cancelled = True


def _parse_tool_input(raw: str | None) -> dict[str, Any]:
    """Tool arguments arrive as a JSON string assembled from stream fragments.

    Malformed JSON becomes an empty dict; the tool's own validation then reports
    the missing arguments to the model, which is a better error than a crash.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _permission_detail(call: ToolUseBlock) -> str:
    """What the approval modal shows: the command, or the exact edit.

    For an edit this previews the change against the file on disk, so the user
    approves a diff rather than a filename.
    """
    if call.name == "Bash":
        return str(call.input.get("command", ""))

    if call.name in {"Edit", "Write"} and (raw_path := call.input.get("file_path")):
        from pathlib import Path

        from hx.tools.edit import parse_edits, unified_diff

        path = Path(str(raw_path))
        try:
            before = path.read_text(encoding="utf-8") if path.is_file() else ""
            if call.name == "Write":
                after = str(call.input.get("content", ""))
            else:
                after = before
                for edit in parse_edits(call.input):
                    after = after.replace(
                        edit.old_string, edit.new_string, -1 if edit.replace_all else 1
                    )
            return unified_diff(before, after, str(path)) or f"{path} (no change)"
        except Exception:
            # Previewing is best effort; never block the prompt on it.
            return str(raw_path)

    return _brief(call.input, limit=400)


def _brief(params: dict[str, Any], limit: int = 80) -> str:
    """One-line parameter preview for a tool-call header.

    Nested structures collapse to a placeholder rather than being sliced
    mid-literal - a header ending in ``{'active_for…`` tells the reader nothing
    and looks broken.
    """
    parts: list[str] = []
    for key, value in params.items():
        if isinstance(value, list):
            rendered = f"[{len(value)} items]"
        elif isinstance(value, dict):
            rendered = "{…}"
        elif isinstance(value, str) and len(value) > limit:
            rendered = repr(value[: limit - 1] + "…")
        else:
            rendered = repr(value)
        parts.append(f"{key}={rendered}")

    joined = ", ".join(parts)
    return joined if len(joined) <= limit else joined[: limit - 1] + "…"
