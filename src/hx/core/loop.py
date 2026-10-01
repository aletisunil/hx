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
import contextlib
import json
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, ClassVar

from hx.core.context import AssembledContext, Instructions
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
    ImageBlock,
    Message,
    StopReason,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserTurn,
    answer_unanswered_calls,
    assistant_message,
    interrupted_result,
    tool_result_message,
    user_message,
)
from hx.core.session import Environment
from hx.core.usage import TurnUsage, compute_cost
from hx.hooks.spec import HookOutcome
from hx.providers.base import ProviderError, ProviderRequest, StreamDelta, StreamEnd
from hx.tools.base import ToolContext, ToolResult

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from hx.config import Settings
    from hx.core.compaction import Compactor
    from hx.core.context import ContextBuilder
    from hx.core.events import EventBus
    from hx.core.lateinject import InjectionRegistry
    from hx.core.session import Session
    from hx.hooks.engine import HookEngine
    from hx.permissions.engine import PermissionEngine
    from hx.providers.base import Provider
    from hx.providers.models import ModelInfo
    from hx.skills.runtime import ActiveSkills
    from hx.tools.registry import ToolRegistry


class _Steered(Exception):
    """Internal: this stream was cancelled to deliver a steer, not to stop."""


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
        instructions: list[Instructions] | None = None,
        hooks: HookEngine | None = None,
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
        self.instructions = instructions or []
        """The AGENTS.md files folded into ``project_context``, least specific first."""
        self.hooks = hooks
        self._cancelled = False
        self._turn_index = 0
        self._steer: list[UserTurn] = []
        """Messages the user pushed into a running turn, not yet delivered."""
        self._steered = False
        """Set while a steer is the reason the current stream is being cancelled."""
        self._stream_task: asyncio.Task[tuple[Message, StopReason]] | None = None
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

    async def run(self, user_input: str, images: Sequence[ImageBlock] = ()) -> TurnResult:
        """Run turns until the model stops calling tools.

        Cancellation (Esc / Ctrl+C) raises ``asyncio.CancelledError`` into this
        coroutine; the partial assistant message is still appended to the
        transcript so the next turn has an honest history.

        Args:
            user_input: What the user typed. Empty continues the conversation
                without a new user turn - unless there are images, which are a
                turn on their own.
            images: Attached to the user turn, after its text.
        """
        self._cancelled = False
        if user_input or images:
            gate = await self._fire_hooks(lambda h: h.user_prompt_submit(user_input))
            if gate.blocked:
                reason = gate.reason or "blocked by hook"
                self.bus.publish(ErrorRaised(message=reason, recoverable=True))
                return TurnResult(StopReason.ERROR, [], error=reason)
            if gate.context_text:
                user_input = f"{user_input}\n\n{gate.context_text}"
            self.session.append(user_message(user_input, images))

        produced: list[Message] = []
        stop_reason = StopReason.END_TURN

        try:
            return await self._turns(produced, stop_reason)
        finally:
            await self._fire_stop()

    async def _turns(self, produced: list[Message], stop_reason: StopReason) -> TurnResult:
        """The turn itself. Split out so :meth:`run` can close it off in one place."""
        for _ in range(self.MAX_TURNS):
            if self._cancelled:
                return TurnResult(StopReason.CANCELLED, produced)

            produced.extend(self._deliver_steer())

            await self._maybe_compact()
            self._turn_index += 1
            if self.origin is None:
                self.bus.publish(TurnStarted(turn_index=self._turn_index, model=self.model))

            try:
                message, stop_reason = await self._await_stream()
            except _Steered:
                # The stream was cut off to make room for what the user just
                # said. Whatever had streamed is already in the transcript, and
                # the next iteration delivers the message and asks again.
                if self.origin is None:
                    self.bus.publish(TurnFinished(self._turn_index, StopReason.CANCELLED))
                continue
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
            if calls and self._cancelled:
                # Stopped between the answer and its tools: none of them ran,
                # and the transcript has to say so before the turn ends - as
                # does the screen, which a resumed session draws them on too.
                for call in calls:
                    self.bus.publish(
                        ToolCallStarted(
                            tool_use_id=call.id,
                            name=self._display_name(call.name),
                            input=call.input,
                        )
                    )
                    self.bus.publish(
                        ToolCallFinished(
                            tool_use_id=call.id,
                            is_error=True,
                            summary="interrupted",
                            interrupted=True,
                        )
                    )
                produced.append(self._answer_calls(calls, {}))
            if not calls or self._cancelled:
                # A steer that arrived during the last stretch is the next thing
                # the user said, so the conversation continues rather than
                # ending and making them send it twice.
                if self._steer and not self._cancelled:
                    continue
                await self._name_session()
                return TurnResult(stop_reason, produced)

            results: dict[str, ToolResultBlock] = {}
            try:
                await self._execute_tools(calls, results)
            finally:
                # Also on interrupt. The calls are already in the transcript,
                # and a call left without a result is rejected by every route on
                # the next request - so every later turn in the session would
                # fail the same way. What finished keeps its real result.
                produced.append(self._answer_calls(calls, results))

        self.bus.publish(
            ErrorRaised(message=f"Stopped after {self.MAX_TURNS} turns", recoverable=True)
        )
        return TurnResult(stop_reason, produced, error="turn limit reached")

    # --- steering ---------------------------------------------------------

    def steer(self, text: str, images: Sequence[ImageBlock] = ()) -> bool:
        """Put ``text`` into the turn that is running now.

        Returns whether it will land mid-turn. ``False`` means there was no turn
        to steer - the caller still owns the text, and :meth:`take_pending_steer`
        hands it back.

        While the model is streaming, that stream is cut off: the point of
        steering is not to wait, and the tokens after the interruption are being
        spent on the wrong thing anyway. While *tools* are running they are left
        to finish, and the message lands at the next model call instead -
        cancelling a half-written file or an in-flight ``git`` command is how a
        working tree ends up in a state nobody asked for.
        """
        text = text.strip()
        if (not text and not images) or self.origin is not None:
            return False

        self._steer.append(UserTurn(text, tuple(images)))
        stream = self._stream_task
        if stream is None or stream.done():
            return False
        self._steered = True
        stream.cancel()
        return True

    def take_pending_steer(self) -> list[UserTurn]:
        """Hand undelivered steers back to the caller, clearing them.

        A cancelled turn drops what it was doing but not what the user typed:
        the frontend puts these back in its queue.
        """
        pending, self._steer = self._steer, []
        return pending

    def _deliver_steer(self) -> list[Message]:
        """Append pending steers to the transcript as what they are: user turns.

        Their own messages, rather than extra text on the tool-result message
        they follow. A correction is a thing the user said, and every route
        encodes a plain user turn the same way; folding it into a message that
        also carries tool results makes its delivery depend on how a particular
        provider flattens mixed content.
        """
        delivered: list[Message] = []
        for turn in self.take_pending_steer():
            message = user_message(turn.text, turn.images)
            self.session.append(message)
            delivered.append(message)
        return delivered

    async def _await_stream(self) -> tuple[Message, StopReason]:
        """Run one stream as its own task, so a steer can cancel just that.

        Cancelling ``run`` itself would end the whole conversation; steering has
        to be able to end one provider call and no more.
        """
        self._steered = False
        task = asyncio.ensure_future(self._stream_turn())
        self._stream_task = task
        try:
            return await task
        except asyncio.CancelledError:
            if self._steered and not self._cancelled:
                self._steered = False
                raise _Steered from None
            raise
        finally:
            self._stream_task = None

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

        messages = self._nameable_messages(self.session)
        if messages is None:
            return

        title = await self._ask_for_title(messages, self.session)
        from hx.core.title import fallback_title

        self.session.set_title(title or fallback_title(messages))

    async def retitle_session(self, session: Session | None = None) -> None:
        """Rename a session for what it turned into, as it closes.

        The first name is written after one exchange, so it describes an opening
        question. Two hours later it is the wrong label on the row the user has
        to recognise in ``/resume``, and the list gives them no way to know that.

        ``session`` names the one to rename, for the cases where it is no longer
        the live one - ``/clear`` and ``/resume`` both leave a session behind.

        Only when the transcript grew since the name was written, and never at
        the cost of the name already there: a rename that fails leaves the old
        title alone rather than replacing something specific with a guess.
        """
        if self.origin is not None:
            return

        target = session if session is not None else self.session
        messages = self._nameable_messages(target)
        if messages is None:
            return
        if len(target.messages) <= target.meta.title_message_count:
            return

        if title := await self._ask_for_title(messages, target):
            target.set_title(title)

    def _nameable_messages(self, session: Session) -> list[Message] | None:
        """The transcript a name is derived from, or ``None`` if there is none."""
        messages = [m for m in session.messages if not m.ephemeral]
        if not any(m.role == "user" for m in messages):
            return None
        return messages

    async def _ask_for_title(self, messages: list[Message], session: Session) -> str | None:
        """One tiny provider call. Returns ``None`` if it produced nothing usable.

        The cost is recorded against the session being named, which is not
        always the live one.
        """
        from hx.core.title import generate_title

        try:
            model = self.settings.models.title_model or self.model
            title, usage = await generate_title(self.provider, model, messages)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        if usage.prompt_tokens or usage.output_tokens:
            self._record_usage(usage, None, session=session)
        return title

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
            #
            # Only if prose reached the user, though. The tool calls are dropped
            # - they were never executed - and what is left of a stream cut off
            # during thinking, or before its first token, carries nothing a route
            # can encode: it goes on the wire as an assistant turn with null
            # content and no tool calls, which is rejected on the very next call.
            # Steering makes that the common case rather than a rare one.
            if text_parts:
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
        messages = answer_unanswered_calls(
            await self.injections.apply(self.session.active_messages())
        )
        if self.model_info is None or not self.model_info.supports_images:
            from hx.core.images import without_images

            name = self.model_info.name if self.model_info is not None else self.model
            messages = without_images(messages, name)
        cache_mode = self.model_info.cache_mode.value if self.model_info else "none"
        assembled = self.context.build(
            messages=messages,
            tools=self.tools.schemas(self._allowed_tools()),
            skills_index=self.skills_index,
            project_context=self.project_context,
            cache_mode=cache_mode,
        )
        self.last_context = assembled
        # What the model was told, recorded alongside what it said. Guarded
        # rather than left to `record_environment`'s own idempotence: the
        # argument renders every schema in the registry, and building one to
        # throw away is the sort of work a long session does hundreds of times.
        #
        # The full registry, not `assembled.tools` - that is this turn's
        # allowed subset, and would understate the session in plan mode or
        # under a skill.
        if self.session.environment is None:
            self.session.record_environment(
                Environment(
                    system_prompt=self.context.system_prompt,
                    tools=self.tools.schemas(),
                    project_context=self.project_context,
                    skills_index=self.skills_index,
                )
            )
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

    def _record_usage(
        self,
        usage: TurnUsage,
        request: ProviderRequest | None,
        session: Session | None = None,
    ) -> None:
        """Bill a call to a session's ledger.

        ``session`` is for spend that belongs to a session other than the live
        one - naming a transcript ``/clear`` has already moved on from. Its cost
        is not published, because the status bar shows the live session and
        adding somebody else's tokens to that total would be wrong.
        """
        if usage.cost_usd is None and self.model_info is not None:
            usage.cost_usd = compute_cost(usage, self.model_info.pricing)
        target = session if session is not None else self.session
        target.record_usage(usage)
        target.flush()
        if target is not self.session:
            return
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

    async def _execute_tools(
        self, calls: list[ToolUseBlock], results: dict[str, ToolResultBlock]
    ) -> None:
        """Permission-check, then run. Read-only calls are gathered concurrently;
        mutating calls run in emission order.

        Each result lands in ``results`` the moment its call finishes, so an
        interrupt keeps what had already run.

        A denied call becomes an ``is_error`` result rather than an exception, so
        the model can react instead of the turn dying.
        """
        concurrent: list[ToolUseBlock] = []

        async def run(call: ToolUseBlock) -> None:
            results[call.id] = await self._run_one(call, results)

        for call in calls:
            if self._runs_serially(call):
                for pending in concurrent:
                    await run(pending)
                concurrent.clear()
                await run(call)
            else:
                concurrent.append(call)

        if concurrent:
            await asyncio.gather(*(run(c) for c in concurrent))

    def _answer_calls(
        self, calls: list[ToolUseBlock], results: dict[str, ToolResultBlock]
    ) -> Message:
        """Append the results for ``calls``, in the order the model asked for them.

        A call with no result was cut off by the user, before it started or
        while it ran, and is answered as exactly that.
        """
        message = tool_result_message(
            [results.get(call.id) or interrupted_result(call.id) for call in calls]
        )
        self.session.append(message)
        return message

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

    async def _run_one(
        self, call: ToolUseBlock, results: dict[str, ToolResultBlock]
    ) -> ToolResultBlock:
        """Run one call. Interrupted, it leaves its answer in ``results``."""
        started = time.monotonic()

        # PreToolUse runs before the call is announced. A hook that rewrites the
        # input must not leave the transcript showing a command that never ran,
        # and re-announcing afterwards would draw the call twice.
        pre = await self._fire_hooks(lambda h: h.pre_tool_use(call.name, call.input))
        if pre.updated_input is not None:
            # Answered before the permission engine sees it too: the user
            # approves what will actually run.
            call = replace(call, input=dict(pre.updated_input))

        self.bus.publish(
            ToolCallStarted(
                tool_use_id=call.id, name=self._display_name(call.name), input=call.input
            )
        )
        streamed: list[str] = []
        try:
            return await self._run_announced(call, pre, started, streamed, results)
        except asyncio.CancelledError:
            if call.id in results:
                # The tool had finished and only its hooks were cut off.
                raise
            cap = self.settings.context.tool_output_char_cap
            results[call.id] = interrupted_result(call.id, "".join(streamed)[-cap:])
            # The call is on screen as running; left alone it would spin on
            # under the "Interrupted" notice for the rest of the session.
            self.bus.publish(
                ToolCallFinished(
                    tool_use_id=call.id,
                    is_error=True,
                    duration_ms=(time.monotonic() - started) * 1000,
                    summary="interrupted",
                    interrupted=True,
                )
            )
            raise

    async def _run_announced(
        self,
        call: ToolUseBlock,
        pre: HookOutcome,
        started: float,
        streamed: list[str],
        results: dict[str, ToolResultBlock],
    ) -> ToolResultBlock:
        """The part of :meth:`_run_one` after the call is on screen.

        What the tool prints is kept in ``streamed`` for an interrupt to report.
        """
        if pre.blocked:
            refusal = ToolResultBlock(
                tool_use_id=call.id,
                content=f"{call.name} was blocked by a hook: {pre.reason}",
                is_error=True,
            )
            self.bus.publish(
                ToolCallFinished(
                    tool_use_id=call.id,
                    is_error=True,
                    duration_ms=(time.monotonic() - started) * 1000,
                    summary="blocked by hook",
                    detail=refusal.content,
                )
            )
            return refusal

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

        def progress(chunk: str) -> None:
            streamed.append(chunk)
            self._emit_progress(call.id, chunk)

        ctx = ToolContext(
            cwd=self.settings.cwd,
            session_id=self.session.meta.session_id,
            tool_use_id=call.id,
            settings=self.settings,
            emit_progress=progress,
        )
        result = await self.tools.call(call.name, call.input, ctx)

        try:
            post = await self._fire_hooks(
                lambda h: h.post_tool_use(call.name, call.input, result.content, result.is_error)
            )
        except asyncio.CancelledError:
            # The tool finished and its effects are real: its own result stands,
            # rather than telling the model a write that happened never did.
            results[call.id] = self._finish(call, result, HookOutcome(), started)
            raise
        return self._finish(call, result, post, started)

    def _finish(
        self, call: ToolUseBlock, result: ToolResult, post: HookOutcome, started: float
    ) -> ToolResultBlock:
        """Announce a call the tool finished, and its result for the model."""
        content = result.content
        if post.blocked:
            content = f"{content}\n\nA hook rejected this result: {post.reason}"
        elif post.context_text:
            content = f"{content}\n\n{post.context_text}"

        self.bus.publish(
            ToolCallFinished(
                tool_use_id=call.id,
                is_error=result.is_error or post.blocked,
                duration_ms=(time.monotonic() - started) * 1000,
                summary=result.summary or ("error" if result.is_error else "done"),
                detail=content if result.is_error or post.blocked else "",
                metadata=dict(result.metadata),
            )
        )
        return ToolResultBlock(
            tool_use_id=call.id,
            content=content,
            is_error=result.is_error or post.blocked,
            spilled_path=result.spilled_path,
            images=list(result.images),
        )

    async def _check_permission(self, call: ToolUseBlock) -> ToolResultBlock | None:
        """Returns a denial result, or ``None`` when the call may proceed."""
        if self.permissions is None or not self.tools.has(call.name):
            return None

        from hx.permissions.engine import PermissionRequest

        tool = self.tools.get(call.name)
        detail, detail_kind = _permission_detail(call)
        request = PermissionRequest(
            tool_name=call.name,
            specifier=tool.permission_specifier(call.input),
            specifiers=tool.permission_specifiers(call.input),
            params=call.input,
            mutating=tool.mutating,
            description=f"{call.name}({_brief(call.input)})",
            detail=detail,
            detail_kind=detail_kind,
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

    async def _fire_stop(self) -> None:
        """Fire ``Stop`` on every way out of a turn, not only the tidy one.

        A hook that releases a lock or stops a timer is needed most on the exits
        that were not planned - Esc, a provider that fell over, the turn limit.
        A cancellation already in flight can still cut the hook short; nothing
        can be awaited once the task is unwinding, and the alternative is
        swallowing the cancellation.
        """
        with contextlib.suppress(Exception):
            await self._fire_hooks(lambda h: h.stop())

    async def _fire_hooks(
        self, call: Callable[[HookEngine], Awaitable[HookOutcome]]
    ) -> HookOutcome:
        """Run one hook event, reporting anything that broke.

        A hook that fails to run is a notice, never a block: a typo in somebody's
        shell command must not be able to wedge the session. Only an explicit
        refusal - exit 2, or ``{"decision": "block"}`` - stops anything.
        """
        if self.hooks is None:
            return HookOutcome()

        outcome = await call(self.hooks)
        for problem in outcome.errors:
            self.bus.publish(ErrorRaised(message=f"hook: {problem}", recoverable=True))
        return outcome

    def _emit_progress(self, tool_use_id: str, chunk: str) -> None:
        from hx.core.events import ToolCallProgress

        self.bus.publish(ToolCallProgress(tool_use_id=tool_use_id, chunk=chunk))

    async def _maybe_compact(self) -> None:
        """Compact if the context fraction crossed the configured threshold.

        Checked against what there is to summarise before anything is
        announced. A threshold that has been crossed stays crossed, so if the
        split has nothing behind it - the boundary never reaches a turn edge,
        or there are too few messages past it - the automatic path used to
        announce a compaction, run it, and report that nothing happened, once
        per turn for the rest of the session. The check is pure, so skipping
        costs neither a provider call nor a line of UI.

        ``/compact`` is deliberately not routed through this: somebody who asks
        for a compaction is owed the answer, including "there was nothing to
        do".
        """
        if self.compactor is None:
            return
        fraction = self.session.usage.context_fraction
        if not self.compactor.should_compact(fraction, self.settings.context.compact_at):
            return
        if not self.compactor.can_compact(self.session.active_messages()):
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
        # Decided before anything is announced: "Compacting…" followed by
        # nothing, beside the caller's "nothing to compact", reads as a
        # compaction that started and never finished.
        if not self.compactor.can_compact(self.session.active_messages()):
            return False

        self.bus.publish(CompactionStarted(reason=reason))
        try:
            result = await self.compactor.compact(self.session.active_messages(), instructions)
        except ProviderError as exc:
            self.bus.publish(ErrorRaised(message=f"Compaction failed: {exc}", recoverable=True))
            return False

        self.session.record_compaction(result.dropped, result.kept, result.summary)
        # The estimates cover the conversation only; the system prompt and tools
        # ahead of it are still sent, so they stay in the gauge - estimated the
        # same way, rather than subtracted out of the provider's real count.
        prefix = sum(
            section.tokens
            for section in (self.last_context.sections if self.last_context else [])
            if section.name != "history"
        )
        self.session.usage.context_tokens = prefix + result.tokens_after
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


def _permission_detail(call: ToolUseBlock) -> tuple[str, str]:
    """What the approval shows, and what kind of thing it is.

    For an edit this previews the change against the file on disk, so the user
    approves a diff rather than a filename.

    The kind is returned rather than left for the UI to infer. Guessing from
    the text meant a file whose first line was ``---`` - YAML front matter, a
    markdown rule - was treated as a diff and run through a painter that
    strips exactly those lines, leaving the user approving a blank space.
    """
    if call.name == "Bash":
        return str(call.input.get("command", "")), "command"

    if call.name in {"Edit", "Write"} and (raw_path := call.input.get("file_path")):
        from pathlib import Path

        from hx.tools.edit import apply_hashline, parse_edits, parse_hashline, unified_diff

        path = Path(str(raw_path))
        try:
            before = path.read_text(encoding="utf-8") if path.is_file() else ""
            if call.name == "Write":
                after = str(call.input.get("content", ""))
            elif call.input.get("hashline"):
                # An anchored edit is previewed by resolving it, the same way
                # the tool will. Without this the prompt falls back to a bare
                # filename and the user approves an edit they never saw.
                after = apply_hashline(before, parse_hashline(call.input))
            else:
                after = before
                for edit in parse_edits(call.input):
                    after = after.replace(
                        edit.old_string, edit.new_string, -1 if edit.replace_all else 1
                    )
            diff = unified_diff(before, after, str(path))
            return (diff, "diff") if diff else (f"{path} (no change)", "text")
        except Exception:
            # Previewing is best effort; never block the prompt on it.
            return str(raw_path), "text"

    return _detailed(call.input), "text"


def _detailed(params: dict[str, Any], limit: int = 400) -> str:
    """What the approval prompt shows for a tool with no preview of its own.

    Lists are spelled out, one element per line, rather than collapsed the way
    :func:`_brief` collapses them for a one-line header. ``WebFetch`` asked the
    user to approve ``urls=[2 items]``: the whole question is *which* URLs are
    about to leave the machine, and that rendering answered it with a number.
    """
    parts: list[str] = []
    for key, value in params.items():
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            listed = "\n".join(f"  {item}" for item in value[:_MAX_LISTED])
            if len(value) > _MAX_LISTED:
                listed += f"\n  … and {len(value) - _MAX_LISTED} more"
            parts.append(f"{key}:\n{listed}" if value else f"{key}: (empty)")
        else:
            parts.append(f"{key}={_one_value(value, limit)}")
    return "\n".join(parts)


_MAX_LISTED = 20
"""Elements shown in full before the rest are counted. A prompt the user has to
scroll is a prompt they stop reading."""


def _one_value(value: Any, limit: int) -> str:
    if isinstance(value, list):
        return f"[{len(value)} items]"
    if isinstance(value, dict):
        return "{…}"
    if isinstance(value, str) and len(value) > limit:
        return repr(value[: limit - 1] + "…")
    return repr(value)


def _brief(params: dict[str, Any], limit: int = 80) -> str:
    """One-line parameter preview for a tool-call header.

    Nested structures collapse to a placeholder rather than being sliced
    mid-literal - a header ending in ``{'active_for…`` tells the reader nothing
    and looks broken.
    """
    joined = ", ".join(f"{key}={_one_value(value, limit)}" for key, value in params.items())
    return joined if len(joined) <= limit else joined[: limit - 1] + "…"
