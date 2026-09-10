"""Late injection must not disturb the cacheable prefix."""

from __future__ import annotations

from hx.core.lateinject import Injection, InjectionRegistry


async def test_apply_does_not_mutate_input() -> None:
    registry = InjectionRegistry()
    registry.register("todos", lambda: Injection(source="todos", text="- [ ] a"))
    messages: list = []
    await registry.apply(messages)
    assert messages == []


async def test_stale_injections_are_replaced_not_accumulated() -> None:
    """Two turns of injection must leave one reminder block, not two."""
    registry = InjectionRegistry()
    registry.register("todos", lambda: Injection(source="todos", text="x"))
    once = await registry.apply([])
    twice = await registry.apply(once)
    assert twice[-1].text().count("<hx-reminder>") == 1


async def test_injectors_never_run_on_the_event_loop() -> None:
    """A blocking injector must not be able to stall the turn.

    Asserted on the thread rather than on wall-clock time: the git watcher's
    subprocess is bounded by a timeout, so a regression here shows up as a
    frozen UI, never as a failure.
    """
    import threading

    seen: list[int] = []

    def probe() -> Injection:
        seen.append(threading.get_ident())
        return Injection(source="probe", text="x")

    registry = InjectionRegistry()
    registry.register("probe", probe)

    await registry.apply([])
    assert seen and seen[0] != threading.get_ident()


def test_collect_is_deterministically_ordered() -> None:
    """Injector registration order must not affect output order."""
    registry = InjectionRegistry()
    registry.register("b", lambda: Injection(source="b", text="b"))
    registry.register("a", lambda: Injection(source="a", text="a"))
    assert [i.source for i in registry.collect()] == ["a", "b"]
