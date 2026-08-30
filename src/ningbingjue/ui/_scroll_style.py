"""Shared slim-scrollbar styling.

Every scrollable page (HUD homepage, tile / layout edit pages, per-card preset
lists) used to render the *fat* default Windows scrollbar.  We standardise on a
single 6px slim scrollbar (the same look the homepage / FAQ popup already use).

Crucially, ``apply_thin_scroll(..., always_on=True)`` also **locks the vertical
gutter width**: the scrollbar is always reserved, so the content viewport width
never changes when the scrollbar appears / disappears.  This prevents a reflow
loop — without it, a width-sensitive child (the wrapping card-colour swatch
list) would re-wrap whenever the scrollbar toggled, the content height would
change, and ``QScrollArea`` would reset the scroll position to the top.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QScrollArea

THIN_SCROLL_STYLE = (
    "QScrollArea { border: none; background: transparent; }"
    "QScrollBar:vertical {"
    "  width: 6px; background: transparent; margin: 2px 1px 2px 1px;"
    "}"
    "QScrollBar::handle:vertical {"
    "  background: rgba(150,165,190,0.30); border-radius: 3px; min-height: 28px;"
    "}"
    "QScrollBar::handle:vertical:hover { background: rgba(150,165,190,0.55); }"
    "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {"
    "  height: 0px; subcontrol-position: bottom; subcontrol-origin: margin;"
    "}"
    "QScrollBar::up-arrow:vertical, QScrollBar::down-arrow:vertical {"
    "  width: 0px; height: 0px;"
    "}"
    "QScrollBar:horizontal { height: 0px; background: transparent; }"
)


def apply_thin_scroll(area: QScrollArea, always_on: bool = True) -> None:
    """Style ``area`` with the slim 6px scrollbar and (when ``always_on``) lock
    its vertical gutter so the content width stays constant."""
    area.setStyleSheet(THIN_SCROLL_STYLE)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setVerticalScrollBarPolicy(
        Qt.ScrollBarPolicy.ScrollBarAlwaysOn if always_on
        else Qt.ScrollBarPolicy.ScrollBarAsNeeded)
