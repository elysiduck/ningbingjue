"""Globally hide the *vertical* scrollbar on every ``QScrollArea`` we own.

Decision (2026-07-22, after a grill session with the user):

  * Vertical scrollbars must never be *shown* anywhere in the app — main HUD
    page, per-card panels, FAQ popup, layout editor, button editor.
  * Scrolling must still work silently: wheel / trackpad / arrow keys keep
    scrolling overflowing content.  ``ScrollBarAlwaysOff`` keeps the scroll
    *range* (so content stays reachable); only the visual gutter is gone.
  * Native widget internal scrollbars (``QComboBox`` dropdowns, ``QListWidget``,
    ``QTextEdit``/``QTextBrowser``, ``QTreeWidget``, ``QTableWidget``, …) are
    **intentionally excluded** — we only intercept ``QScrollArea`` instances,
    never their ``QAbstractScrollArea`` siblings, so e.g. a long dropdown list
    still scrolls normally.
  * Mechanism: a single recursive event filter installed on ``QApplication``.
    Any ``QScrollArea`` created *now or in the future* is caught automatically,
    so there is zero per-call-site maintenance and no way to "forget" one.

This module is imported and wired up only from ``run.py`` (the GUI launcher),
so headless smoke tests and pytest sessions that build their own
``QApplication`` are unaffected and keep asserting the old ``AsNeeded``
behaviour — which is functionally identical (hidden + still scrollable).
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtWidgets import QApplication, QScrollArea, QWidget

_INSTANCE: "QObject | None" = None


class _VerticalScrollHider(QObject):
    """Event filter that forces ``ScrollBarAlwaysOff`` on every QScrollArea."""

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        etype = event.type()
        if etype == QEvent.Type.ChildAdded:
            # A widget was just reparented into the tree.  Cover it and its
            # whole subtree so nested scroll areas are caught too.
            child = event.child()
            if child is not None:
                self._cover(child)
        elif etype == QEvent.Type.Polish and isinstance(obj, QScrollArea):
            # Safety net: re-assert even if a policy was set *after* parenting
            # (e.g. a future caller sets AsNeeded right before first show).
            obj.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return False

    def _cover(self, widget: QWidget) -> None:
        """Install ourselves on ``widget`` and every descendant, and force the
        vertical policy off on any QScrollArea found."""
        widget.installEventFilter(self)
        if isinstance(widget, QScrollArea):
            widget.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for sub in widget.findChildren(QWidget):
            sub.installEventFilter(self)
            if isinstance(sub, QScrollArea):
                sub.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)


def install_vertical_scrollbar_hider(qapp: QApplication) -> None:
    """Install the global vertical-scrollbar hider on ``qapp`` (idempotent)."""
    global _INSTANCE
    if _INSTANCE is not None:
        return
    hider = _VerticalScrollHider(qapp)
    qapp.installEventFilter(hider)
    # Cover widgets already in the tree (none yet at app start, but harmless).
    for w in qapp.allWidgets():
        hider._cover(w)
    _INSTANCE = hider
