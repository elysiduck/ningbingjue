"""Headless behavioural smoke test for the refactored shortcut editor."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication

from ningbingjue.core.shortcuts import ShortcutStore, ShortcutEngine, Shortcut, ActionSpec
from ningbingjue.ui.shortcut_editor import ShortcutEditor, _RecordButton

app = QApplication([])  # required before any QWidget

PASS, FAIL = [], []

def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name)

# Fake controller: records the one-shot gamepad capture callback.
class FakeCtrl:
    def __init__(self):
        self._cap = None
    def set_gamepad_capture(self, cb):
        self._cap = cb
    def get_gamepad_capture(self):
        return self._cap

ctrl = FakeCtrl()
store = ShortcutStore()
store.add(Shortcut(id="s1", label="Test", gamepad="x",
                   short=ActionSpec("launch", {"exe": "notepad.exe"}),
                   long=ActionSpec("inapp", {"command": "next_page"})))
engine = ShortcutEngine(store)

# 1) Window flags + fixed width.
ed = ShortcutEditor(None, store, engine, ctrl, lambda: None, parent=None)
check("editor is a top-level Window", bool(ed.windowFlags() & Qt.WindowType.Window))
check("editor has title bar", bool(ed.windowFlags() & Qt.WindowType.WindowTitleHint))
check("editor has OS close button", bool(ed.windowFlags() & Qt.WindowType.WindowCloseButtonHint))
check("editor fixed width = 500", ed.width() == 500)
check("editor non-modal", ed.windowModality() == Qt.WindowModality.NonModal)

# 2) rebuild produced system + user cards.
n = ed._list_lay.count()
check("rebuild created content (>=5 widgets)", n >= 5)

# 3) width clamp does not crash and caps inner list <= viewport.
ed._scroll.setGeometry(0, 0, 500, 560)
ed._clamp_inner_width()
check("inner list max-width clamped", ed._list.maximumWidth() <= 500)

# 4) beside-parent placement never crashes (parent None -> screen centre path).
try:
    ed._place_beside_parent()
    check("place_beside_parent no crash", True)
except Exception as e:
    check("place_beside_parent no crash (" + str(e) + ")", False)

# 5) keyboard chord capture: modifier-only must NOT overwrite a real key.
rb = _RecordButton("keyboard", ctrl, lambda: "", lambda t: None)
rb._recording = True  # arm capture path directly (skip grabKeyboard in offscreen)
ev_ctrl = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Control,
                    Qt.KeyboardModifier.ControlModifier)
rb.keyPressEvent(ev_ctrl)
tok_after_ctrl = rb._pending_token
ev_f8 = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_F8,
                  Qt.KeyboardModifier.ControlModifier)
rb.keyPressEvent(ev_f8)
tok_after_f8 = rb._pending_token
rb._recording = False
check("modifier-only ignored (pending empty)", tok_after_ctrl == "")
check("full chord captured (CTRL+F8)", tok_after_f8 == "CTRL+F8")

# 6) user card rejects reserved 'select', accepts 'y'.
from ningbingjue.ui.shortcut_editor import _ShortcutCard
sc = store.shortcuts[0]
card = _ShortcutCard(sc, store, ctrl, lambda: None)
card._on_gp_captured("select")
check("user card rejects reserved 'select'", sc.gamepad != "select")
card._on_gp_captured("y")
check("user card accepts non-reserved 'y'", sc.gamepad == "y")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
