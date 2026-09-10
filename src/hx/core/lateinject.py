"""Late injection of per-turn context.

Anything that changes every turn - todo state, files modified since last read,
plan-mode notices, permission-mode changes - must NOT live in the system prompt.
Mutating the prefix invalidates the provider KV cache on every single turn,
which is the single most expensive mistake this harness can make.

Instead, injectors emit ``<hx-reminder>`` blocks that are appended to the tail
of the newest user message and flagged ``ephemeral=True``. Before each turn the
previous ephemeral blocks are stripped and regenerated. Nothing above the
rolling cache breakpoint is ever rewritten.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, replace

from hx.core.messages import ContentBlock, Message, TextBlock

REMINDER_OPEN = "<hx-reminder>"
REMINDER_CLOSE = "</hx-reminder>"


@dataclass(slots=True)
class Injection:
    """One block of late-injected context."""

    source: str
    """Injector name, for debugging via ``/context``."""
    text: str
    priority: int = 0
    """Higher priority renders closer to the end of the message."""


Injector = Callable[[], Injection | None]
"""Returns the current injection, or ``None`` to contribute nothing this turn."""


class InjectionRegistry:
    """Holds the active injectors and applies them to a message list."""

    def __init__(self) -> None:
        self._injectors: dict[str, Injector] = {}

    def register(self, name: str, injector: Injector) -> None:
        self._injectors[name] = injector

    def unregister(self, name: str) -> None:
        self._injectors.pop(name, None)

    def collect(self) -> list[Injection]:
        """Run every injector, drop ``None`` results, sort by priority then name
        so output is deterministic (a reordered prefix is a cache miss).

        Blocking by nature - injectors stat files, hash them, and shell out to
        git - so it is called off the event loop by :meth:`apply`. Callers
        inside a coroutine must await that rather than reaching in here.
        """
        collected: list[Injection] = []
        for name in sorted(self._injectors):
            try:
                injection = self._injectors[name]()
            except Exception:
                # A broken injector must not kill the turn.
                continue
            if injection is not None and injection.text.strip():
                collected.append(injection)
        return sorted(collected, key=lambda i: (i.priority, i.source))

    async def apply(self, messages: list[Message]) -> list[Message]:
        """Return a copy of ``messages`` with stale ephemeral content stripped and
        fresh injections appended to the final user message.

        The input list is never mutated: the caller keeps the clean transcript
        and only the assembled request carries injections.

        :meth:`collect` runs in a worker thread. Every injector does real I/O -
        the git watcher spawns ``git status`` under a two-second timeout, the
        stale-file injector hashes each file it is tracking - and this runs
        once per provider call, not once per user turn. On the event loop a
        turn with ten tool calls would freeze the TUI ten times over.
        """
        cleaned = [strip_injections(m) if m.role == "user" else m for m in messages]

        injections = await asyncio.to_thread(self.collect)
        if not injections:
            return cleaned

        block = render(injections)
        tail = cleaned[-1] if cleaned else None
        if tail is not None and tail.role == "user":
            content = [*tail.content, TextBlock(text="\n\n" + block)]
            cleaned[-1] = replace(tail, content=content, ephemeral=True)
        else:
            # No trailing user turn to ride on - carry the reminders on their own
            # ephemeral message rather than touching anything already cached.
            cleaned.append(Message(role="user", content=[TextBlock(text=block)], ephemeral=True))
        return cleaned


def strip_injections(message: Message) -> Message:
    """Remove ``<hx-reminder>`` blocks from a message's text content."""
    if REMINDER_OPEN not in _text_of(message):
        return message

    content: list[ContentBlock] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            stripped = _REMINDER_RE.sub("", block.text).rstrip()
            if stripped:
                content.append(TextBlock(text=stripped))
        else:
            content.append(block)
    return replace(message, content=content, ephemeral=False)


def render(injections: list[Injection]) -> str:
    """Render injections into a single reminder-wrapped string."""
    body = "\n\n".join(i.text.strip() for i in injections)
    return f"{REMINDER_OPEN}\n{body}\n{REMINDER_CLOSE}"


def _text_of(message: Message) -> str:
    return "".join(b.text for b in message.content if isinstance(b, TextBlock))


_REMINDER_RE = re.compile(
    re.escape(REMINDER_OPEN) + r".*?" + re.escape(REMINDER_CLOSE),
    re.DOTALL,
)
