"""Verify the ShortcutEditor ('快捷键' window) buttons are excluded from the
button-style editor's collected group list.

Runs headless (offscreen). Mirrors the real widget tree:
  * a main window (AppWindow stand-in) holding:
      - a BlockPanel (block_type='power') with a real QPushButton child
      - a ShortcutEditor-like child window with several QPushButton children
  * calls the real collect_button_groups with extra_excludes pointing at the
    shortcut editor, and asserts the shortcut buttons are NOT present.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from PyQt6.QtWidgets import QApplication, QWidget, QPushButton, QVBoxLayout

# Import the REAL collection routine (also exercises that button_edit imports cleanly).
from ningbingjue.ui.button_edit import collect_button_groups

app = QApplication.instance() or QApplication(sys.argv)


class _BlockPanel(QWidget):
    def __init__(self, bt, parent=None):
        super().__init__(parent)
        self.block_type = bt


# ---- build a synthetic main window tree -------------------------------
main = QWidget()
main.setObjectName("AppWindow")

# A real HUD card button (should appear under 'power').
power_card = _BlockPanel("power", main)
real_btn = QPushButton("锁帧")
real_btn.setParent(power_card)
real_btn.setProperty("page_nav", False)

# The ShortcutEditor-like window (should be excluded).
shortcut_ed = QWidget(main)
shortcut_ed.setObjectName("ShortcutEditor")
sc_layout = QVBoxLayout(shortcut_ed)
sc_buttons = []
for label in ("录制", "清除", "删除", "关闭", "添加", "编辑条目A", "编辑条目B"):
    b = QPushButton(label)
    sc_layout.addWidget(b)
    sc_buttons.append(b)

main.show()
QApplication.processEvents()

# ---- case 1: WITHOUT exclusion (regression baseline) ------------------
groups_without = collect_button_groups(main, exclude=None, popup=None, extra_excludes=None)
leaked_without = [w for _, items in groups_without.items() for _, w in items
                  if any(b is w for b in sc_buttons)]
print("[without exclusion] shortcut buttons collected:", len(leaked_without))
for b in leaked_without:
    print("   leaked:", b.text())

# ---- case 2: WITH exclusion (the fix path) ----------------------------
groups_with = collect_button_groups(main, exclude=None, popup=None,
                                     extra_excludes=[shortcut_ed])
leaked_with = [w for _, items in groups_with.items() for _, w in items
               if any(b is w for b in sc_buttons)]
print("[with exclusion] shortcut buttons collected:", len(leaked_with))

# The real HUD button must still be present.
power_present = any(real_btn is w for _, items in groups_with.items()
                    for _, w in items)
print("[with exclusion] real 'power' HUD button present:", power_present)

ok = (len(leaked_without) == len(sc_buttons)) and (len(leaked_with) == 0) and power_present
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
