"""Session naming.

``/resume`` lists sessions by title. Without one it lists ids, which are
timestamps - accurate and useless for picking the session that fixed the parser.
So after the first exchange the model is asked to name the session in a few
words, and the name is written to the session's ``meta.json``.

The call is deliberately tiny: one short prompt, no tools, a 32-token cap. If it
fails for any reason the session still gets a name, taken from the first thing
the user said.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hx.core.compaction import render_transcript
from hx.core.messages import Message, TextBlock
from hx.core.usage import TurnUsage

if TYPE_CHECKING:
    from hx.providers.base import Provider, ProviderRequest

TITLE_MAX_TOKENS = 32
"""A title is a handful of words; anything longer is a summary nobody asked for."""

MAX_TITLE_CHARS = 50
"""A session row is read at a glance, in a list. Longer names stop being names."""
TRANSCRIPT_CHARS = 2_000

TITLE_PROMPT = """\
Name this coding session so it can be recognised in a list of sessions.

Rules:
- One line, at most 6 words.
- Name the concrete subject: the file, feature, bug or command in play.
- Name what the session did overall, not only how it opened.
- No quotes, no punctuation at the end, no prefix like "Session:" or "Title:".
- Reply with the name and nothing else.

--- conversation ---

{transcript}
"""


def build_title_request(messages: list[Message], model: str) -> ProviderRequest:
    """The provider call that names a session: no tools, no project context."""
    from hx.core.context import AssembledContext, PromptSection
    from hx.providers.base import ProviderRequest

    transcript = render_transcript(messages)[:TRANSCRIPT_CHARS]
    context = AssembledContext(
        system=[PromptSection("system", "You name things briefly and precisely.")],
        messages=[
            Message(
                role="user",
                content=[TextBlock(text=TITLE_PROMPT.format(transcript=transcript))],
            )
        ],
        tools=[],
    )
    return ProviderRequest(context=context, model=model, max_tokens=TITLE_MAX_TOKENS)


async def generate_title(
    provider: Provider,
    model: str,
    messages: list[Message],
) -> tuple[str | None, TurnUsage]:
    """Ask the model for a title. Returns ``(None, usage)`` when it produced nothing."""
    from hx.providers.base import StreamDelta, StreamEnd

    request = build_title_request(messages, model)
    parts: list[str] = []
    usage = TurnUsage()
    async for item in provider.astream(request):
        if isinstance(item, StreamEnd):
            usage = item.usage
        elif isinstance(item, StreamDelta) and item.text:
            parts.append(item.text)

    return clean_title("".join(parts)) or None, usage


def clean_title(raw: str) -> str:
    """Strip the decoration models add to a name, and cap its length.

    Whitespace is collapsed first, so a model that answers in two lines still
    yields one: a name with a newline in it breaks the row it is rendered in.
    """
    text = " ".join(raw.split())
    for prefix in ("Title:", "Session:", "Name:"):
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix) :].strip()
    text = text.lstrip("-*• ").strip()
    text = text.strip("\"'`").rstrip(".:").strip()
    if len(text) > MAX_TITLE_CHARS:
        text = text[:MAX_TITLE_CHARS].rstrip() + "…"
    return text


def fallback_title(messages: list[Message]) -> str:
    """A name taken from the first user message, for when the model call cannot run."""
    for message in messages:
        if message.role != "user":
            continue
        first_line = next((line for line in message.text().splitlines() if line.strip()), "")
        cleaned = clean_title(first_line)
        if cleaned:
            return cleaned
    return "Untitled session"
