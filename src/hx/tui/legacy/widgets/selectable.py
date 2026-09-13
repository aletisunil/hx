"""Text selection for widgets that draw themselves with Rich renderables.

Textual extracts the text under a mouse selection from the widget's visual, and
only when that visual is a ``Text`` or a ``Content``::

    visual = self._render()
    if isinstance(visual, (Text, Content)):
        text = str(visual)
    else:
        return None

A widget whose ``render`` returns a Rich renderable - a ``Markdown``, a
``Group``, anything wrapped in ``Padding`` - is turned into a ``RichVisual``
instead, so the check fails and the selection yields nothing. Dragging across
assistant prose or a tool block highlighted it and copied an empty string,
while a notice right beside it copied fine, which is what made the behaviour
look intermittent rather than structural.

Selection offsets are screen coordinates - line and column of what is drawn -
so the text they index has to be the drawn text. This renders the widget and
reads the strips back, which is exactly what the user is pointing at: wrapped
lines wrap in the same places, and a markdown bullet copies as the bullet it
shows rather than as the asterisk in the source.
"""

from __future__ import annotations

from textual.geometry import Region
from textual.selection import Selection
from textual.widget import Widget


class SelectableBlock(Widget):
    """Mixin: make a Rich-rendered widget's on-screen text selectable.

    Mix in *after* the widget's real base - ``class Notice(Static,
    SelectableBlock)``, not the other way round. Textual derives a widget's CSS
    type names by walking the *first* DOM base of each class in turn, not the
    full MRO, so leading with the mixin drops ``Static`` out of that chain: the
    widget stops matching ``Static`` in a stylesheet and in ``query(Static)``,
    and loses ``Static``'s own default CSS with it. Python's MRO still finds
    ``get_selection`` here either way, because the widget's base does not
    define one.
    """

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        text = self.rendered_text()
        if text is None:
            return None
        return selection.extract(text), "\n"

    def rendered_text(self) -> str | None:
        """The widget's drawn text, one string, lines joined by newlines.

        ``None`` when there is nothing drawn to select - a widget with no size
        yet, or one whose render failed. Trailing padding is stripped per line:
        Rich pads every strip out to the full width, and a copied paragraph
        should not arrive with a hundred spaces on the end of each line.
        """
        width, height = self.size
        if not width or not height:
            return None
        try:
            strips = self.render_lines(Region(0, 0, width, height))
        except Exception:
            # Selection is a convenience; a renderer that throws here must not
            # take the keystroke - or the app - down with it.
            return None
        return "\n".join(strip.text.rstrip() for strip in strips)
