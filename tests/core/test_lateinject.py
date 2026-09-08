"""Late injection must not disturb the cacheable prefix."""

from __future__ import annotations

from hx.core.lateinject import Injection, InjectionRegistry


def test_apply_does_not_mutate_input() -> None:
    registry = InjectionRegistry()
    registry.register("todos", lambda: Injection(source="todos", text="- [ ] a"))
    messages: list = []
    registry.apply(messages)
    assert messages == []


def test_stale_injections_are_replaced_not_accumulated() -> None:
    """Two turns of injection must leave one reminder block, not two."""
    registry = InjectionRegistry()
    registry.register("todos", lambda: Injection(source="todos", text="x"))
    once = registry.apply([])
    twice = registry.apply(once)
    assert twice[-1].text().count("<hx-reminder>") == 1


def test_collect_is_deterministically_ordered() -> None:
    """Injector registration order must not affect output order."""
    registry = InjectionRegistry()
    registry.register("b", lambda: Injection(source="b", text="b"))
    registry.register("a", lambda: Injection(source="a", text="a"))
    assert [i.source for i in registry.collect()] == ["a", "b"]
