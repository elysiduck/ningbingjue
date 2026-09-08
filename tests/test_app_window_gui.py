"""GUI regression test for AppWindow.paintEvent (frosted skin).

Guards against the crash:

    AttributeError: 'Color' object has no attribute 'red'

which fired whenever a *custom breathing glow colour* was configured. The
paint path assumed `self._accent` (a PyQt ``QColor``), but
``self._breath_color`` is a ningbingjue ``Color`` (channels ``.r/.g/.b``).
We drive the real ``paintEvent`` with both colour sources so the bug can
never silently return.

Auto-skips when PyQt6 is unavailable (headless / bundled runtime without Qt).
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtWidgets import QApplication, QWidget
except ImportError:  # pragma: no cover - only when Qt is missing
    raise SystemExit(0)

from ningbingjue.ui.app_window import _OverlayWindow as AppWindow
from ningbingjue.core.color.rgb import Color


class _PaintHarness(QWidget):
    """Minimal QWidget exposing the exact attributes ``paintEvent`` reads.

    Reuses the *real* ``AppWindow.paintEvent`` (monkey-patched below) so the
    regression covers the production code path, not a copy.
    """

    def __init__(self) -> None:
        super().__init__()
        self._offscreen = False
        self._bg = None  # set per-test to a QColor
        self._breath_alpha = 0.8
        self._accent = None  # set per-test to a QColor
        self._breath_color = None  # None => fall back to _accent (QColor)


# Bind the production paint method onto the harness.
_PaintHarness.paintEvent = AppWindow.paintEvent


def _harness_with(breath_color):
    app = QApplication.instance() or QApplication([])
    w = _PaintHarness()
    from PyQt6.QtGui import QColor
    w._bg = QColor(20, 20, 30)
    w._accent = QColor(127, 209, 255)
    w._breath_color = breath_color
    w.resize(400, 300)
    w.show()
    return app, w


def test_default_accent_paints_without_error():
    """No custom glow configured -> glow is a QColor (the original happy path)."""
    app, w = _harness_with(None)
    app.processEvents()
    w.repaint()
    app.processEvents()


def test_custom_breath_color_paints_without_error():
    """Custom glow configured -> glow is a ningbingjue Color; must NOT crash."""
    app, w = _harness_with(Color.from_rgb(255, 100, 50))
    app.processEvents()
    w.repaint()
    app.processEvents()


def test_breathing_color_swatches_have_background_color():
    """Each color swatch in BreathingEditPanel must show its own color."""
    app = QApplication.instance() or QApplication([])

    class DummyApp:
        _window = None

    from ningbingjue.ui.edit_overlay import BreathingEditPanel

    panel = BreathingEditPanel(DummyApp(), None)
    for sw in panel._swatches:
        ss = sw.styleSheet()
        assert "background-color:" in ss, (
            f"swatch style missing background-color: {ss}")
        assert f"#{sw._hex}" in ss, (
            f"swatch style missing expected color #{sw._hex}: {ss}")


if __name__ == "__main__":
    for name in dir(sys.modules[__name__]):
        if name.startswith("test_"):
            globals()[name]()
    print("OK - all app_window paint regression tests passed")
