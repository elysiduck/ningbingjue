"""GUI-thread regression test for the shortcut editor (pyqt-thread-bug skill).

The skill's core rule: any timer callback that touches a widget must run on the
GUI thread.  A threading.Timer / threading.Thread callback touching a widget is
undefined behaviour that py_compile + plain unit tests cannot catch.

This test drives a REAL Qt event loop (offscreen) and asserts that every timer
callback in the shortcut-editor recording path runs on the GUI thread:

  * countdown ``_tick``        (QTimer -> setText)
  * keyboard debounce          (QTimer -> _finalize_keyboard -> on_captured)
  * gamepad capture            (InteractionController._gp_timer QTimer ->
                               _poll_gamepad -> set_gamepad_capture cb -> _finalize_gamepad)

Run:  runtime/python/python.exe test_editor_thread_gui.py
"""

import os
import sys
import time as _t

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import QThread, Qt
    from PyQt6.QtGui import QKeyEvent
    from PyQt6.QtWidgets import QApplication, QWidget
except Exception:  # pragma: no cover - no PyQt6 in this runtime
    print("SKIP: PyQt6 not available")
    raise SystemExit(0)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from ningbingjue.ui.gamepad import GamepadState, MockBackend
from ningbingjue.ui.interaction import InteractionController
from ningbingjue.ui.shortcut_editor import _RecordButton

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name)


app = QApplication.instance() or QApplication([])
gui = QApplication.instance().thread()


class _Win(QWidget):  # a QWidget so isActiveWindow works
    def __init__(self):
        super().__init__()
        self.active = True

    def isActiveWindow(self):
        return self.active


def pump(ms):
    """Process the event loop for ~ms milliseconds so QTimer.timeouts fire."""
    end = _t.time() + ms / 1000.0
    while _t.time() < end:
        QApplication.processEvents()
        _t.sleep(0.01)


host = QWidget()
win = _Win()
mb = MockBackend()
ctrl = InteractionController(win, host, gamepad=mb, config=None)
mb.state = GamepadState(connected=True)  # baseline: all released

seen = {}

# Patch the countdown slot on the CLASS *before* any instance is built, so the
# timer's timeout.connect(self._tick) captures the spy (connect binds at ctor).
orig_tick = _RecordButton._tick


def _spy_tick(self):
    seen["countdown_gui"] = (QThread.currentThread() == gui)
    orig_tick(self)


_RecordButton._tick = _spy_tick


# ---------------------------------------------------------------------------
# 1) Countdown timer runs on the GUI thread (QTimer -> _tick -> setText)
# ---------------------------------------------------------------------------
rb_gp = _RecordButton("gamepad", ctrl, lambda: None, lambda btn: None)
rb_gp.start_record()           # arms self._timer (QTimer, 1000ms)
pump(1300)                     # let at least one _tick fire
rb_gp._cancel()
check("countdown tick on GUI thread", seen.get("countdown_gui") is True)
check("countdown actually ticked (secs<3)", rb_gp._secs < 3)


# ---------------------------------------------------------------------------
# 2) Keyboard debounce runs on the GUI thread (QTimer -> _finalize_keyboard)
# ---------------------------------------------------------------------------
kb_captured = {}


def _on_kb(token):
    kb_captured["token"] = token
    kb_captured["gui"] = (QThread.currentThread() == gui)


rb_kb = _RecordButton("keyboard", ctrl, lambda: None, _on_kb)
try:
    rb_kb.start_record()       # grabKeyboard + _timer
    ev = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_F8,
                   Qt.KeyboardModifier.ControlModifier)
    rb_kb.keyPressEvent(ev)    # sets _pending_token + starts _kb_debounce (350ms)
    pump(600)                  # let the single-shot debounce fire
    rb_kb._cancel()
except Exception as e:                         # pragma: no cover - offscreen guard
    check("keyboard capture path executed", False)
    print("   (error:", e, ")")
else:
    check("keyboard debounce on GUI thread", kb_captured.get("gui") is True)
    check("keyboard chord captured CTRL+F8", kb_captured.get("token") == "CTRL+F8")


# ---------------------------------------------------------------------------
# 3) Gamepad capture runs on the GUI thread
#    (InteractionController._gp_timer QTimer -> _poll_gamepad -> capture cb)
# ---------------------------------------------------------------------------
gp_captured = {}


def _on_gp(btn):
    gp_captured["btn"] = btn
    gp_captured["gui"] = (QThread.currentThread() == gui)


rb_gp2 = _RecordButton("gamepad", ctrl, lambda: None, _on_gp)
rb_gp2.start_record()          # arms ctrl.set_gamepad_capture(cb)
# Drive the real poll timer; create a rising edge on button 'x'.
ctrl.start_gamepad()
pressed = False
deadline = _t.time() + 2.0
while _t.time() < deadline and "btn" not in gp_captured:
    if not pressed:
        # first poll baseline (released) already taken; now press 'x'
        mb.state = GamepadState(connected=True, x=True)
        pressed = True
    QApplication.processEvents()
    _t.sleep(0.02)
ctrl.stop_gamepad()
rb_gp2._cancel()

check("gamepad capture fired", "btn" in gp_captured)
check("gamepad capture on GUI thread", gp_captured.get("gui") is True)
check("gamepad captured button 'x'", gp_captured.get("btn") == "x")


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    raise SystemExit(1)
