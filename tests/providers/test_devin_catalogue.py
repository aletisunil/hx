"""Devin's model list: which configs are offers, and how effort families fold."""

from __future__ import annotations

from typing import Any

from hx.providers import devin_wire as wire
from hx.providers.devin_catalogue import normalize, wire_model
from hx.providers.protowire import Writer


def config(
    uid: str,
    label: str = "",
    *,
    family: str = "",
    effort: str | None = None,
    thinking: int | None = None,
    fast: bool = False,
    default: bool = False,
    display: int = 0,
    router: bool = False,
    harness: tuple[str, ...] = (),
    disabled: bool = False,
    features: dict[str, bool] | None = None,
    max_tokens: int = 0,
    max_output: int = 0,
) -> Writer:
    """A ``ClientModelConfig`` as the server encodes one."""
    message = Writer()
    message.put_str(1, label or uid)
    message.put_bool(4, disabled)
    message.put_uint(18, max_tokens)
    message.put_str(22, uid)

    info = Writer()
    info.put_uint(13, max_output)
    info.put_strs(20, harness)
    info.put_uint(22, display)
    info.put_bool(25, router)
    if features is not None:
        feats = Writer()
        feats.put_bool(12, features.get("tools", False))
        feats.put_bool(15, features.get("thinking", False))
        info.put_message(6, feats)
    message.put_message(23, info)

    if family:
        meta = Writer()
        meta.put_str(1, family)
        entries: list[tuple[str, str, int]] = []
        if effort is not None:
            entries.append(("Reasoning Effort", effort, 0))
        if thinking is not None:
            entries.append(("Thinking", "", thinking))
        if fast:
            entries.append(("Fast Mode", "Fast", 1))
        for key, name, order in entries:
            value = Writer()
            value.put_uint(1, order)
            value.put_str(2, name)
            entry = Writer()
            entry.put_str(1, key)
            entry.put_message(2, value)
            meta.put_message(2, entry)
        message.put_message(30, meta)
    message.put_bool(31, default)
    return message


def catalogue(*configs: Writer) -> list[Any]:
    response = Writer()
    for entry in configs:
        response.put_message(1, entry)
    return normalize(wire.parse_model_configs(response.finish()))


def by_id(models: list[Any]) -> dict[str, Any]:
    return {model.id: model for model in models}


def test_a_plain_config_becomes_a_subscription_model() -> None:
    [model] = catalogue(
        config(
            "swe-1-7",
            "SWE-1.7",
            features={"tools": True, "thinking": True},
            max_tokens=256_000,
            max_output=32_000,
        )
    )
    assert model.id == "devin/swe-1-7"
    assert model.name == "SWE-1.7"
    assert model.provider_id == "devin"
    assert model.is_subscription
    assert model.context_window == 256_000
    assert model.max_output_tokens == 32_000
    assert model.supports_reasoning
    assert wire_model(model, None) == "swe-1-7"


def test_missing_limits_fall_back_rather_than_reading_zero() -> None:
    [model] = catalogue(config("swe-1-6"))
    assert model.context_window == 200_000
    assert model.max_output_tokens == 64_000


def test_disabled_and_internal_configs_are_not_offered() -> None:
    models = catalogue(
        config("off", disabled=True),
        config("review", display=wire.DISPLAY_QUICK_REVIEW),
        config("internal", display=wire.DISPLAY_INTERNAL_DEFAULT),
        config("visible", display=wire.DISPLAY_NORMAL),
        config("visible"),
    )
    assert [m.id for m in models] == ["devin/visible"]


def test_an_effort_family_folds_into_one_model() -> None:
    """``/effort`` then picks the backend id, as it picks a request field on Codex."""
    models = catalogue(
        config("claude-opus-5-low", family="Claude Opus 5", effort="Low"),
        config("claude-opus-5-high", family="Claude Opus 5", effort="High", default=True),
        config("claude-opus-5-max", family="Claude Opus 5", effort="Max"),
    )
    [model] = models
    assert model.id == "devin/claude-opus-5"
    assert model.reasoning_levels == ("low", "high", "max")
    assert model.default_reasoning_level == "high"
    assert wire_model(model, "max") == "claude-opus-5-max"
    assert wire_model(model, None) == "claude-opus-5-high"


def test_fast_service_is_its_own_model_not_an_effort() -> None:
    models = by_id(
        catalogue(
            config("gpt-high", family="GPT-5.6 Sol", effort="High"),
            config("gpt-high-fast", family="GPT-5.6 Sol", effort="High", fast=True),
        )
    )
    assert set(models) == {"devin/gpt-5-6-sol", "devin/gpt-5-6-sol-fast"}
    assert models["devin/gpt-5-6-sol-fast"].name == "GPT-5.6 Sol Fast"
    assert wire_model(models["devin/gpt-5-6-sol-fast"], "high") == "gpt-high-fast"


def test_the_thinking_axis_separates_claude_pairs_sharing_an_effort_label() -> None:
    [model] = catalogue(
        config("sonnet", family="Claude Sonnet 5", effort="High", thinking=0),
        config("sonnet-thinking", family="Claude Sonnet 5", effort="High", thinking=1),
    )
    assert model.reasoning_levels == ("none", "high")
    assert wire_model(model, "none") == "sonnet"
    assert wire_model(model, "high") == "sonnet-thinking"


def test_a_family_with_no_effort_ladder_stays_separate() -> None:
    models = catalogue(
        config("a", family="Thing", effort="No Thinking"),
        config("b", family="Thing"),
    )
    assert {m.id for m in models} == {"devin/a", "devin/b"}


def test_without_a_server_default_the_first_listed_member_is_the_default() -> None:
    [model] = catalogue(
        config("m-medium", family="Model", effort="Medium"),
        config("m-low", family="Model", effort="Low"),
    )
    assert model.default_reasoning_level == "medium"
    assert wire_model(model, None) == "m-medium"


def test_a_router_is_assigned_per_turn_and_never_folded() -> None:
    models = by_id(
        catalogue(
            config("adaptive", family="Model", effort="High", display=wire.DISPLAY_MODEL_ROUTER),
            config("fusion", router=True, harness=("h1",)),
        )
    )
    assert models["devin/adaptive"].model_router is True
    assert models["devin/adaptive"].reasoning_levels == ()
    # A harness-backed composite is a chat model in its own right.
    assert models["devin/fusion"].model_router is False


def test_router_configs_without_features_still_offer_tools() -> None:
    [model] = catalogue(config("adaptive", display=wire.DISPLAY_MODEL_ROUTER))
    assert model.supports_tools is True


def test_duplicate_uids_keep_the_first() -> None:
    [model] = catalogue(config("x", "First"), config("x", "Second"))
    assert model.name == "First"


def test_an_unknown_model_is_sent_by_its_bare_id() -> None:
    from hx.providers.models import ModelInfo, ModelPricing

    info = ModelInfo(
        id="devin/swe-9",
        name="swe-9",
        context_window=1,
        max_output_tokens=1,
        pricing=ModelPricing(),
    )
    assert wire_model(info, "high") == "swe-9"
