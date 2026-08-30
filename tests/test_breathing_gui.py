"""GUI-thread regression test for ``BreathingController`` (requires PyQt6).

Guards the fix where the breathing timer MUST fire on the GUI thread, so its
callback is allowed to touch widgets (e.g. call ``widget.update()``).  The old
implementation used a ``threading.Timer`` (``RepeatingTimer``) whose callback ran
on a worker thread — calling ``update()`` from there is undefined behaviour that
crashed / lagged the real PyQt6 app (the "呼吸灯闪退/卡死/预览灰" bugs).

Skipped automatically when PyQt6 is unavailable (headless CI shims).
"""

from __future__ import annotations

import os

# Run headless-safe regardless of whether a display is present.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import pytest
    _HAVE_PYTEST = True
except Exception:  # pragma: no cover - manual runs may lack pytest
    pytest = None
    _HAVE_PYTEST = False

try:
    from PyQt6.QtCore import QThread, QTimer
    from PyQt6.QtWidgets import QApplication
    _HAVE_QT = True
except Exception:  # pragma: no cover - headless shims without PyQt6
    _HAVE_QT = False

if not _HAVE_QT:
    if _HAVE_PYTEST:
        pytest.skip("PyQt6 not available", allow_module_level=True)

from ningbingjue.ui.breathing import BreathingController, _HAS_QT
from ningbingjue.core.timers import RepeatingTimer


def test_controller_uses_gui_thread_qtimer():
    """The breathing timer must be a GUI-thread QTimer, never RepeatingTimer."""
    assert _HAS_QT, "this test only makes sense with PyQt6 present"
    app = QApplication.instance() or QApplication([])
    bc = BreathingController(period_s=2.0)
    bc.start_scanline()
    try:
        assert isinstance(bc._scan_timer, QTimer), (
            "scan timer must be a QTimer (GUI thread), got %r" % type(bc._scan_timer))
        assert not isinstance(bc._scan_timer, RepeatingTimer), (
            "scan timer must NOT be the threaded RepeatingTimer")
    finally:
        bc.stop()


def test_scanline_callback_runs_on_gui_thread():
    """The on_scanline callback must execute on the GUI thread, so it can safely
    repaint widgets.  This is the property that prevents the cross-thread crash."""
    assert _HAS_QT
    app = QApplication.instance() or QApplication([])
    bc = BreathingController(period_s=1.0)
    gui_thread = QThread.currentThread()
    seen = {}
    fired = []

    def _cb(alpha: float) -> None:
        fired.append(alpha)
        seen["thread"] = QThread.currentThread()

    bc.on_scanline = _cb
    bc.start_scanline()
    try:
        QTimer.singleShot(150, app.quit)
        app.exec()
        assert fired, "scanline callback never fired"
        assert seen.get("thread") is gui_thread, (
            "scanline callback ran OFF the GUI thread — cross-thread widget "
            "access would crash/lag the real app")
    finally:
        bc.stop()
