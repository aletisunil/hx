"""What a component is, and how containers put them together.

A component is a pure function from a width to a list of lines. That is the
entire contract. There is no widget tree with coordinates, no layout
negotiation, no parent that can move a child: a container renders its children
and concatenates the results, and the document is the concatenation of
everything in it.

The cost of the model is that a change anywhere means re-rendering. The cache
here is what makes that affordable - a child that has not been invalidated
hands back the lines it produced last time - so a spinner ticking beside a
three-thousand-line transcript costs one child's render and a list join.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Component(Protocol):
    """Anything that can draw itself into ``width`` columns.

    Implementations must guarantee, for every line returned:

    * its :func:`~hx.term.width.cell_width` is at most ``width``
    * it ends in :data:`~hx.term.ansi.SEGMENT_RESET`, or is empty

    The renderer checks both and refuses to draw a frame that violates them,
    because a single over-wide line wraps and pushes everything below it down
    for the rest of the session.
    """

    def render(self, width: int) -> list[str]: ...


@runtime_checkable
class Interactive(Protocol):
    """A component that can take a keystroke.

    ``handle_input`` returns whether it consumed the key. Unconsumed keys carry
    on to the next handler, which is how a prompt can own ``up`` while the
    transcript still scrolls with it when the prompt has nothing to scroll.
    """

    def handle_input(self, key: str, data: str) -> bool: ...


class Widget:
    """Base class supplying the dirty-tracking half of the contract.

    Subclasses implement :meth:`draw`; :meth:`render` memoises it per width.
    Anything that changes what :meth:`draw` would return must call
    :meth:`invalidate`, and the discipline that makes this safe is that
    components hold their own state and nobody else writes to it.
    """

    __slots__ = ("_cache", "_cache_width", "_dirty")

    def __init__(self) -> None:
        self._cache: list[str] = []
        self._cache_width: int = -1
        self._dirty: bool = True

    def draw(self, width: int) -> list[str]:
        """Produce the lines. Implemented by subclasses."""
        raise NotImplementedError

    def render(self, width: int) -> list[str]:
        if self._dirty or width != self._cache_width:
            self._cache = self.draw(width)
            self._cache_width = width
            self._dirty = False
        return self._cache

    def invalidate(self) -> None:
        self._dirty = True

    @property
    def dirty(self) -> bool:
        return self._dirty


class Container(Widget):
    """Children stacked vertically. The only layout there is.

    Re-rendering asks each child for its lines; a clean child returns its cache
    without doing any work, so the cost of a frame is proportional to what
    actually changed rather than to the length of the session.
    """

    __slots__ = ("children",)

    def __init__(self, *children: Component) -> None:
        super().__init__()
        self.children: list[Component] = list(children)

    def add(self, child: Component) -> Component:
        """Append a child and return it, so callers can keep a handle on it."""
        self.children.append(child)
        self.invalidate()
        return child

    def extend(self, children: list[Component]) -> None:
        self.children.extend(children)
        self.invalidate()

    def clear(self) -> None:
        self.children.clear()
        self.invalidate()

    def remove(self, child: Component) -> None:
        if child in self.children:
            self.children.remove(child)
            self.invalidate()

    def render(self, width: int) -> list[str]:
        """Concatenate the children.

        This bypasses :meth:`Widget.render`'s cache deliberately: a container is
        clean only when every one of its children is, and a child knows that
        before the container does. Asking each child is what lets a deep tree
        skip the work rather than re-rendering from the root.
        """
        lines: list[str] = []
        for child in self.children:
            lines.extend(child.render(width))
        self._cache_width = width
        self._dirty = False
        return lines

    def draw(self, width: int) -> list[str]:  # pragma: no cover - render overrides
        return self.render(width)

    def handle_input(self, key: str, data: str) -> bool:
        """Offer the key to each child until one takes it."""
        for child in self.children:
            handler = getattr(child, "handle_input", None)
            if handler is not None and handler(key, data):
                return True
        return False
