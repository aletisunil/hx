"""Images through the loop: into the transcript, out to the provider, back from tools."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

from hx.config import load_settings
from hx.core.compaction import Compactor, render_transcript
from hx.core.context import ContextBuilder
from hx.core.events import EventBus
from hx.core.lateinject import InjectionRegistry
from hx.core.loop import AgentLoop
from hx.core.messages import (
    ImageBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    from_dict,
    to_dict,
    user_message,
)
from hx.core.session import load_session, new_session
from hx.providers.base import StreamItem
from hx.providers.fake import FakeProvider, Pause, text_turn, tool_turn
from hx.providers.models import ModelInfo, ModelPricing
from hx.tools.base import Tool, ToolContext, ToolResult
from hx.tools.registry import ToolRegistry

MODEL = "anthropic/claude-sonnet-4.5"

PICTURE = ImageBlock("image/png", "iVBORw0KGgo=", width=640, height=480, label="Image #1")


def info(*, images: bool) -> ModelInfo:
    return ModelInfo(
        id=MODEL,
        name="Claude Sonnet 4.5" if images else "Text Model",
        context_window=200_000,
        max_output_tokens=8192,
        pricing=ModelPricing(),
        supports_images=images,
    )


def build_loop(
    script: list[list[StreamItem]],
    tmp_path: Path,
    *,
    images: bool = True,
    tools: ToolRegistry | None = None,
) -> AgentLoop:
    session = new_session(tmp_path, MODEL)
    session.set_title("images")
    return AgentLoop(
        provider=FakeProvider(script),
        session=session,
        tools=tools or ToolRegistry(),
        permissions=None,
        context=ContextBuilder("sys", tmp_path),
        compactor=None,
        injections=InjectionRegistry(),
        bus=EventBus(),
        settings=load_settings(tmp_path),
        model_info=info(images=images),
    )


class Screenshot(Tool):
    name = "Screenshot"
    description = "returns a picture"

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        shot = replace(PICTURE, label="screen.png")
        return ToolResult(content="took a screenshot", images=[shot])


async def test_an_attached_image_reaches_the_provider(hx_home: Path, tmp_path: Path) -> None:
    loop = build_loop([text_turn("a red square")], tmp_path)
    await loop.run("what is [Image #1]?", [PICTURE])

    sent = loop.provider.requests[0].context.messages[-1]
    assert sent.text().startswith("what is [Image #1]?")
    assert sent.images() == [PICTURE]


async def test_an_image_with_no_words_is_a_turn(hx_home: Path, tmp_path: Path) -> None:
    loop = build_loop([text_turn("a red square")], tmp_path)
    await loop.run("", [PICTURE])
    assert loop.session.messages[0].content == [PICTURE]


async def test_a_text_only_model_is_told_rather_than_sent(hx_home: Path, tmp_path: Path) -> None:
    loop = build_loop([text_turn("I cannot see it")], tmp_path, images=False)
    await loop.run("what is [Image #1]?", [PICTURE])

    sent = loop.provider.requests[0].context.messages[-1]
    assert sent.images() == []
    assert "[Image #1 omitted: Text Model does not accept image input]" in sent.text()
    # Kept in the transcript, for a model that can see it.
    assert loop.session.messages[0].images() == [PICTURE]


async def test_a_tools_image_rides_its_result(hx_home: Path, tmp_path: Path) -> None:
    tools = ToolRegistry()
    tools.register(Screenshot())
    loop = build_loop(
        [tool_turn("Screenshot", {}), text_turn("I see the screen")], tmp_path, tools=tools
    )
    await loop.run("look at the screen")

    carrier = loop.provider.requests[1].context.messages[-1]
    [result] = carrier.tool_results()
    assert result.content == "took a screenshot"
    assert [image.label for image in result.images] == ["screen.png"]


async def test_a_steer_can_carry_an_image(hx_home: Path, tmp_path: Path) -> None:
    pause = Pause()
    loop = build_loop([[pause, *text_turn("first")], text_turn("second")], tmp_path)

    turn = asyncio.ensure_future(loop.run("start"))
    await loop.provider.wait_for_requests(1)
    assert loop.steer("this one [Image #1]", [PICTURE]) is True
    await loop.provider.wait_for_requests(2)
    pause.release()
    await turn

    assert loop.provider.requests[1].context.messages[-1].images() == [PICTURE]


async def test_images_survive_a_resume(hx_home: Path, tmp_path: Path) -> None:
    loop = build_loop([text_turn("ok")], tmp_path)
    await loop.run("keep [Image #1]", [PICTURE])
    loop.session.flush()

    restored = load_session(loop.session.meta.session_id)
    assert restored.messages[0].images() == [PICTURE]


def test_a_tool_result_round_trips_with_and_without_images() -> None:
    with_images = Message(role="user", content=[ToolResultBlock("t1", "x", images=[PICTURE])])
    plain = Message(role="user", content=[ToolResultBlock("t1", "x")])

    assert from_dict(to_dict(with_images)) == with_images
    assert from_dict(to_dict(plain)) == plain
    # A text-only result is written exactly as before images existed.
    assert "images" not in to_dict(plain)["content"][0]


def test_images_count_by_pixels_in_the_context_estimate(tmp_path: Path) -> None:
    builder = ContextBuilder("sys", tmp_path)
    heavy = replace(PICTURE, data="A" * 2_000_000)
    with_image = builder.build([user_message("hi", [heavy])], [])
    without = builder.build([user_message("hi")], [])
    added = with_image.total_tokens - without.total_tokens
    # About 410 tokens for 640x480 - not the half-million its base64 would be.
    assert 350 < added < 500


def test_the_summary_prompt_records_images_it_cannot_show() -> None:
    messages = [
        user_message("see [Image #1]", [PICTURE]),
        Message(role="user", content=[ToolResultBlock("t1", "shot", images=[PICTURE])]),
    ]
    rendered = render_transcript(messages)
    assert "[user] (attached an image: Image #1 · 640x480" in rendered
    assert "[tool result] (attached an image: Image #1" in rendered
    assert "iVBOR" not in rendered


def test_compaction_estimates_images_too(tmp_path: Path) -> None:
    compactor = Compactor(context=ContextBuilder("sys", tmp_path))
    text_only = compactor._estimate([Message(role="user", content=[TextBlock("hi")])])
    with_image = compactor._estimate([user_message("hi", [PICTURE])])
    assert with_image > text_only + 300
