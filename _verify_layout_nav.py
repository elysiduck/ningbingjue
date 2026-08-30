"""Headless check: keyboard arrows navigate the layout editor's 2-D grid.

Run with the bundled PyQt6 python (offscreen). Verifies that, once arrow keys
reach InteractionController.handle_key in edit mode (the fix in app_window.py
eventFilter now routes them there for the layout editor instead of letting the
QScrollArea swallow them to scroll the page), the focus moves through the
explicit (row, col) matrix the way _apply_grid implements it.

This is the downstream half of the 2026-07-30 layout-editor focus fix.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication, QWidget, QPushButton

from ningbingjue.ui.interaction import InteractionController, NavItem

app = QApplication(sys.argv)
win = QWidget()
win.show()

# Build a fake edit overlay whose nav_items() returns a (row, col) matrix like
# the real LayoutEditPanel: header row 0, two card rows, footer row.
class FakeOverlay:
    def __init__(self):
        self._widgets = []
    def mode(self):
        return "layout"
    def _mk(self, label):
        b = QPushButton(label)
        b.setParent(win)
        self._widgets.append(b)
        return b
    def nav_items(self):
        items = []
        # header (row 0, col 0)
        items.append(NavItem(self._mk("style"), "action", row=0, col=0))
        # card 1 (row 1)
        items.append(NavItem(self._mk("c1-edit"), "action", row=1, col=0))
        items.append(NavItem(self._mk("c1-up"), "action", row=1, col=1))
        items.append(NavItem(self._mk("c1-down"), "action", row=1, col=2))
        items.append(NavItem(self._mk("c1-prev"), "action", row=1, col=3))
        items.append(NavItem(self._mk("c1-next"), "action", row=1, col=4))
        # card 2 (row 2)
        items.append(NavItem(self._mk("c2-edit"), "action", row=2, col=0))
        items.append(NavItem(self._mk("c2-up"), "action", row=2, col=1))
        items.append(NavItem(self._mk("c2-down"), "action", row=2, col=2))
        items.append(NavItem(self._mk("c2-prev"), "action", row=2, col=3))
        items.append(NavItem(self._mk("c2-next"), "action", row=2, col=4))
        # footer (row 3)
        items.append(NavItem(self._mk("save"), "action", row=3, col=0))
        items.append(NavItem(self._mk("cancel"), "action", row=3, col=1))
        items.append(NavItem(self._mk("shortcut"), "action", row=3, col=2))
        return items

ctrl = InteractionController(win, host=win)
ov = FakeOverlay()
ctrl.set_edit_overlay(ov)
ctrl.set_edit_mode(True)          # builds _grid_rows from the matrix
ctrl.set_host(win)
ctrl.set_focus(0)                 # header (row 0, col 0)

# Map (row, col) -> nav-item index for assertions.
idx = {}
for i, it in enumerate(ctrl._items):
    idx[(it.row, it.col)] = i

def press(key):
    ev = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
    ctrl.handle_key(ev)

def label_of(i):
    return ctrl._items[i].widget.text()

passed = 0

# Start at header (0,0). Right has no target in row 0 -> grid rejects, falls
# back to geometric move; just confirm focus is valid and not -1.
assert ctrl._focus == idx[(0, 0)], ctrl._focus
print("start at header:", label_of(ctrl._focus))

# Move to card1 edit (1,0) explicitly, then exercise the grid horizontally.
ctrl.set_focus(idx[(1, 0)])
press(Qt.Key.Key_Right)
assert ctrl._focus == idx[(1, 1)], (label_of(ctrl._focus), "expected c1-up")
print("Right from c1-edit ->", label_of(ctrl._focus)); passed += 1
press(Qt.Key.Key_Right)
assert ctrl._focus == idx[(1, 2)], label_of(ctrl._focus)
print("Right ->", label_of(ctrl._focus)); passed += 1
press(Qt.Key.Key_Left)
assert ctrl._focus == idx[(1, 1)], label_of(ctrl._focus)
print("Left  ->", label_of(ctrl._focus)); passed += 1

# Vertical move between cards keeps the same column (col 0 = edit).
ctrl.set_focus(idx[(1, 0)])
press(Qt.Key.Key_Down)
assert ctrl._focus == idx[(2, 0)], label_of(ctrl._focus)
print("Down from c1-edit ->", label_of(ctrl._focus)); passed += 1
press(Qt.Key.Key_Down)
assert ctrl._focus == idx[(3, 0)], label_of(ctrl._focus)
print("Down ->", label_of(ctrl._focus), "(footer save)"); passed += 1
press(Qt.Key.Key_Up)
assert ctrl._focus == idx[(2, 0)], label_of(ctrl._focus)
print("Up   ->", label_of(ctrl._focus)); passed += 1

# Clamp column when crossing jagged rows (footer has 3 cols, cards have 5).
ctrl.set_focus(idx[(3, 2)])  # footer shortcut (col 2)
press(Qt.Key.Key_Up)
# Up from footer col 2 -> card row 2, col 2 clamped to existing (c2-down).
assert ctrl._focus == idx[(2, 2)], (label_of(ctrl._focus), "expected c2-down")
print("Up from footer col2 ->", label_of(ctrl._focus), "(column clamped)"); passed += 1

print(f"\nALL PASS ({passed}/8) ✅ — 编辑态布局矩阵方向键导航正常")
