"""Regression test for the "owned helper popup" focus logic.

When the foreground window is one of this app's own docked helper popups
(FAQ window / button-list window), the main HUD must treat itself as still
focused: no unfocus dim, breathing keeps running.  This test exercises the
real :meth:`_OverlayWindow.register_associated_window` and
:meth:`_OverlayWindow._is_own_popup_active` methods (bound onto a tiny double
so the full HUD never has to boot) to lock that behaviour in.
"""

import os
import sys
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtWidgets import QApplication, QWidget
except ImportError:  # pragma: no cover - only when Qt is missing
    raise SystemExit(0)

from ningbingjue.ui.app_window import _OverlayWindow as AppWindow


class _FocusLogic:
    """Minimal stand-in carrying exactly the attributes the two methods read.

    The production methods are bound onto it so we test the real code path
    without constructing a whole ``_OverlayWindow`` (which needs a display,
    admin rights, ryzenadj, etc.).
    """

    def __init__(self) -> None:
        self._associated_windows: list = []
        self._offscreen = False
        self._breath = None


_FocusLogic.register_associated_window = AppWindow.register_associated_window
_FocusLogic._is_own_popup_active = AppWindow._is_own_popup_active


def _make_popup() -> QWidget:
    from PyQt6.QtCore import Qt

    w = QWidget()
    w.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
    w.show()
    return w


def test_register_and_detect():
    app = QApplication.instance() or QApplication([])
    win = _FocusLogic()
    p1 = _make_popup()
    p2 = _make_popup()

    # Nothing registered -> no popup considered "own".
    assert win._is_own_popup_active() is False

    # Register p1; it should now be recognised when active.
    win.register_associated_window(p1)
    assert p1 in win._associated_windows
    assert len(win._associated_windows) == 1

    # Duplicate registration is a no-op.
    win.register_associated_window(p1)
    assert len(win._associated_windows) == 1

    # None is ignored gracefully.
    win.register_associated_window(None)
    assert len(win._associated_windows) == 1

    # p1 active -> True.  (Offscreen QPA does not track real activation, so
    # we drive QApplication.activeWindow directly to simulate the popup being
    # foreground — that is exactly what Windows reports when the Tool window
    # takes focus, which is the real-world trigger for the bug we fix.)
    with patch.object(QApplication, "activeWindow", return_value=p1):
        assert win._is_own_popup_active() is True

    # p2 (not registered) active -> False, even though it is a Tool window.
    with patch.object(QApplication, "activeWindow", return_value=p2):
        assert win._is_own_popup_active() is False

    # No foreground window -> False.
    with patch.object(QApplication, "activeWindow", return_value=None):
        assert win._is_own_popup_active() is False

    # Offscreen guard: even a registered+active popup must not count as
    # "own" in headless/offscreen mode (where the dim/breath paths are inert).
    win._offscreen = True
    with patch.object(QApplication, "activeWindow", return_value=p1):
        assert win._is_own_popup_active() is False


if __name__ == "__main__":
    for name in dir(sys.modules[__name__]):
        if name.startswith("test_"):
            globals()[name]()
    print("OK - owned-popup focus detection tests passed")
