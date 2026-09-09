"""Session naming: the model call and the fallback behind it."""

from __future__ import annotations

import pytest

from hx.core.messages import StopReason, TextBlock, assistant_message, user_message
from hx.core.title import (
    MAX_TITLE_CHARS,
    TITLE_MAX_TOKENS,
    clean_title,
    fallback_title,
    generate_title,
)
from hx.core.usage import TurnUsage
from hx.providers.base import StreamEnd
from hx.providers.fake import FakeProvider, text_turn


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"Fix the parser"', "Fix the parser"),
        ("Title: Fix the parser.", "Fix the parser"),
        ("  Fix   the\n parser  ", "Fix the parser"),
        ("`Fix the parser`", "Fix the parser"),
        ("", ""),
    ],
)
def test_clean_title_strips_what_models_decorate_with(raw: str, expected: str) -> None:
    assert clean_title(raw) == expected


def test_a_long_title_is_clamped() -> None:
    title = clean_title("word " * 40)
    assert len(title) <= MAX_TITLE_CHARS + 1  # the ellipsis
    assert title.endswith("…")


def test_fallback_title_uses_the_first_user_message() -> None:
    messages = [
        assistant_message([TextBlock("ignored")]),
        user_message("\n\nAdd a retry to the uploader\nmore detail here"),
    ]
    assert fallback_title(messages) == "Add a retry to the uploader"


def test_fallback_title_when_there_is_nothing_to_go_on() -> None:
    assert fallback_title([]) == "Untitled session"


async def test_generate_title_asks_for_a_short_name() -> None:
    provider = FakeProvider([text_turn('"Retry logic for the uploader"')])
    messages = [user_message("add retries"), assistant_message([TextBlock("done")])]

    title, _usage = await generate_title(provider, "m", messages)

    assert title == "Retry logic for the uploader"
    request = provider.requests[0]
    assert request.max_tokens == TITLE_MAX_TOKENS
    assert request.context.tools == []
    assert "add retries" in request.context.messages[0].text()


async def test_generate_title_reports_no_title_when_the_model_says_nothing() -> None:
    provider = FakeProvider([[StreamEnd(stop_reason=StopReason.END_TURN, usage=TurnUsage())]])
    title, usage = await generate_title(provider, "m", [user_message("hi")])

    assert title is None
    assert usage.output_tokens == 0
