"""Context assembly and cache-breakpoint placement.

This module owns the single most performance-sensitive invariant in HX:
**the request prefix must be byte-stable across turns.** Providers key their KV
cache on an exact prefix match, so any reordering, timestamp, or per-turn
counter placed above a breakpoint silently costs full price on every request.

Layout, in order::

    [1] system prompt          static for the session
    [2] tool schemas           deterministic sort: builtins, then mcp__* alphabetical
    [3] skills index           name + description only (progressive disclosure)
    [4] project context        AGENTS.md, cwd, git branch, top-level listing
    --- breakpoint A (static) ---
    [5] conversation history
    --- breakpoint B (rolling, before the last few turns) ---
    [6] latest user turn + late-injected ephemeral blocks

Volatile data belongs below breakpoint B, via ``hx.core.lateinject``.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from hx.core.messages import Message

if TYPE_CHECKING:
    from hx.config import PromptSettings

MAX_CACHE_BREAKPOINTS = 4
"""Anthropic's per-request limit on explicit ``cache_control`` markers."""


@dataclass(slots=True)
class PromptSection:
    """One addressable chunk of the prefix, tracked so ``/context`` can show a breakdown."""

    name: str
    text: str
    tokens: int = 0


@dataclass(slots=True)
class AssembledContext:
    """The exact payload handed to a provider, plus the accounting behind it.

    ``messages`` holds the conversation only. The provider prepends the system
    message when serialising, so ``breakpoints`` index the *payload* list where
    position 0 is that system message.
    """

    system: list[PromptSection]
    messages: list[Message]
    tools: list[dict[str, Any]]
    breakpoints: tuple[int, ...] = ()
    """Indices into the payload message list that carry an explicit cache
    breakpoint. Empty for implicit-caching models."""
    total_tokens: int = 0
    sections: list[PromptSection] = field(default_factory=list)

    def system_text(self) -> str:
        """Concatenated system sections, in declared order."""
        return "\n\n".join(section.text for section in self.system if section.text)

    def prefix_fingerprint(self) -> str:
        """Stable hash of everything above the first breakpoint.

        Tests assert this is unchanged across turns; a change means the cache
        was invalidated.
        """
        digest = hashlib.sha256()
        digest.update(self.system_text().encode())
        digest.update(b"\x00")
        digest.update(json.dumps(self.tools, sort_keys=True, separators=(",", ":")).encode())
        return digest.hexdigest()


class ContextBuilder:
    """Assembles :class:`AssembledContext` from session state."""

    #: Estimated tokens that must accumulate below the rolling breakpoint before
    #: it is allowed to move. Advancing it every turn rewrites the cache
    #: constantly and costs more than the hit it buys.
    BREAKPOINT_HYSTERESIS_TOKENS: ClassVar[int] = 4_096
    CHARS_PER_TOKEN: ClassVar[float] = 3.7

    def __init__(
        self,
        system_prompt: str,
        cwd: Path,
        keep_recent_turns: int = 6,
    ) -> None:
        self.system_prompt = system_prompt
        self.cwd = cwd
        self.keep_recent_turns = keep_recent_turns
        self._breakpoint_b: int | None = None
        self._tokens_below_b: int = 0

    def build(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        skills_index: str | None = None,
        project_context: str | None = None,
        cache_mode: str = "explicit",
    ) -> AssembledContext:
        """Assemble the request.

        Args:
            cache_mode: ``explicit`` (place ``cache_control`` markers),
                ``implicit`` (rely on prefix stability alone), or ``none``.
        """
        system = [PromptSection("system", self.system_prompt)]
        if skills_index:
            system.append(PromptSection("skills", skills_index))
        if project_context:
            system.append(PromptSection("project", project_context))
        for section in system:
            section.tokens = self.estimate_tokens(section.text)

        active = [m for m in messages if not m.compacted]
        sorted_tools = sort_tools(tools)

        breakpoints = self.place_breakpoints(active) if cache_mode == "explicit" else ()

        sections = [
            *system,
            PromptSection(
                "tools",
                json.dumps(sorted_tools, sort_keys=True),
                self.estimate_tokens(json.dumps(sorted_tools, sort_keys=True)),
            ),
            PromptSection(
                "history",
                "",
                sum(self.estimate_tokens(_message_text(m)) for m in active),
            ),
        ]

        return AssembledContext(
            system=system,
            messages=active,
            tools=sorted_tools,
            breakpoints=breakpoints,
            total_tokens=sum(s.tokens for s in sections),
            sections=sections,
        )

    def place_breakpoints(self, messages: list[Message]) -> tuple[int, ...]:
        """Choose breakpoint indices into the payload list (0 = system message).

        Breakpoint A sits on the system message, at the end of the static prefix.
        Breakpoint B rolls forward to just before the last ``keep_recent_turns``
        messages, and only moves once enough tokens have accumulated below it -
        see :attr:`BREAKPOINT_HYSTERESIS_TOKENS`.
        """
        breakpoints = [0]
        if len(messages) <= self.keep_recent_turns:
            self._breakpoint_b = None
            return tuple(breakpoints)

        # +1 converts a conversation index into a payload index.
        candidate = len(messages) - self.keep_recent_turns
        if self._breakpoint_b is None:
            self._breakpoint_b = candidate
        elif self._breakpoint_b > candidate:
            # The history got shorter - a rewind, /clear, or a resumed session.
            # B has to come back with it: left where it was it would sit past
            # the end of the payload, and the hysteresis window below it would
            # be empty forever, so it could never move again.
            self._breakpoint_b = candidate
        else:
            below = sum(
                self.estimate_tokens(_message_text(m))
                for m in messages[self._breakpoint_b : candidate]
            )
            if below >= self.BREAKPOINT_HYSTERESIS_TOKENS:
                self._breakpoint_b = candidate

        payload_index = self._breakpoint_b
        if payload_index > 0:
            breakpoints.append(payload_index)
        return tuple(breakpoints[:MAX_CACHE_BREAKPOINTS])

    def estimate_tokens(self, text: str) -> int:
        """Cheap local token estimate for gauges and the compaction trigger.

        Deliberately approximate; the authoritative counts come back from the
        provider in ``usage``.
        """
        return int(len(text) / self.CHARS_PER_TOKEN) + 1 if text else 0


def sort_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canonical tool ordering: builtins first, then ``mcp__*``, each by name.

    The serialised tool block sits in the cached prefix, so the order must not
    depend on registration order, MCP connection order, or dict iteration.
    """
    return sorted(
        tools, key=lambda t: (str(t.get("name", "")).startswith("mcp__"), t.get("name", ""))
    )


def _message_text(message: Message) -> str:
    parts: list[str] = []
    for block in message.content:
        text = getattr(block, "text", None) or getattr(block, "content", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def build_project_context(cwd: Path) -> str:
    """Static per-session project preamble: AGENTS.md contents, cwd, git branch, listing.

    Computed once at startup and then frozen - refreshing it mid-session would
    invalidate the prefix.
    """
    lines = [f"Working directory: {cwd}"]

    branch = git_branch(cwd)
    if branch:
        lines.append(f"Git branch: {branch}")

    entries = sorted(
        p.name + ("/" if p.is_dir() else "") for p in cwd.iterdir() if not p.name.startswith(".")
    )
    if entries:
        lines.append("Top level: " + ", ".join(entries[:60]))

    agents_md = cwd / "AGENTS.md"
    if agents_md.is_file():
        lines.append(f"\n# Project instructions (AGENTS.md)\n\n{agents_md.read_text()}")

    return "\n".join(lines)


def git_branch(cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


SYSTEM_PROMPT = """\
You are HX, an agentic coding assistant running in the user's terminal.

Work directly on the user's codebase using the tools available to you. Prefer
reading the relevant code over guessing at it. Make the change that was asked
for - do not narrow it, widen it, or substitute a different one.

Be concise. The user is reading your output in a terminal, so favour short
direct answers and code over prose. Reference files as `path/to/file.py:42`.

When a task takes several steps, track them so the user can see the plan and
what remains. Report what actually happened: if a command failed, say so and
show the output.

Questions about HX itself - a command, a setting, how to add an API key, what
changed in a release - are answered from the documentation shipped with this
build, not from memory: `hx docs` lists the manual's sections, `hx docs
<section>` prints one, and `hx changelog [version]` prints what each version
added. Read the relevant one before answering.
"""


@dataclass(slots=True)
class ResolvedPrompt:
    """The system prompt in force, and where each part of it came from."""

    text: str
    source: str
    """What supplied the base prompt: ``built-in``, ``--system-prompt``, or a path."""
    appends: tuple[str, ...] = ()
    """Sources of the appended blocks, in the order they were appended."""


def resolve_system_prompt(cwd: Path, prompt: PromptSettings | None = None) -> ResolvedPrompt:
    """Layer the system prompt overrides.

    The base prompt is replaced by the first of these that exists::

        --system-prompt / prompt.system   (settings layer, highest)
        <cwd>/.hx/system-prompt.md
        ~/.hx/system-prompt.md
        the built-in SYSTEM_PROMPT

    Appended text is then added in a fixed order - user file, project file, then
    each ``--append-system-prompt`` value - so two layers can both contribute
    without either winning.

    Read once per session by the caller: this text sits above the first cache
    breakpoint, and re-reading it mid-session would invalidate the prefix.
    """
    from hx.paths import (
        project_system_prompt_append_file,
        project_system_prompt_file,
        user_system_prompt_append_file,
        user_system_prompt_file,
    )

    text = SYSTEM_PROMPT
    source = "built-in"
    if prompt is not None and prompt.system:
        text, source = prompt.system, "--system-prompt"
    else:
        for path in (project_system_prompt_file(cwd), user_system_prompt_file()):
            body = _read_prompt_file(path)
            if body:
                text, source = body, str(path)
                break

    blocks: list[str] = []
    appends: list[str] = []
    for path in (user_system_prompt_append_file(), project_system_prompt_append_file(cwd)):
        body = _read_prompt_file(path)
        if body:
            blocks.append(body)
            appends.append(str(path))
    for extra in prompt.append if prompt is not None else ():
        if extra.strip():
            blocks.append(extra.strip())
            appends.append("--append-system-prompt")

    if blocks:
        text = "\n\n".join([text.rstrip("\n"), *blocks])

    return ResolvedPrompt(text=text, source=source, appends=tuple(appends))


def _read_prompt_file(path: Path) -> str:
    """Contents of a prompt override file, or ``""`` when it is absent or empty.

    An unreadable file is treated as absent: a permissions problem on an
    optional override must not stop the session from starting.
    """
    try:
        return path.read_text().strip() if path.is_file() else ""
    except OSError:
        return ""


def load_system_prompt(cwd: Path, prompt: PromptSettings | None = None) -> str:
    """The system prompt in force. Contains no volatile values (no clock, no counters)."""
    return resolve_system_prompt(cwd, prompt).text
