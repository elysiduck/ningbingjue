"""Headless check: preset active-state colour now reaches HintButton.set_style.

Run with the bundled PyQt6 python (offscreen). Verifies the 2026-07-30 regression
fix: toggling a preset-bound toggle button (e.g. ZZZ2 w/ ActiveBackColor) now
shows the preset's active colour instead of freezing on the base colour.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from PyQt6.QtWidgets import QApplication
from ningbingjue.ui.gamepad_hint import HintButton
from ningbingjue.core.color.styles import _apply_style_to_ctrl, ButtonStyle

app = QApplication(sys.argv)


def build_preset(active_hex=None, inactive_hex=None, cr=17):
    colors = {
        "BackColor": "282828",
        "HoverColor": "ffff00",
        "BorderColor": "5f5f5f",
        "ForeColor": "aaff7f",
    }
    if active_hex:
        colors["ActiveBackColor"] = active_hex
    if inactive_hex:
        colors["InactiveBackColor"] = inactive_hex
    return ButtonStyle(corner_radius=cr, border_thickness=4, colors=colors)


def rgb_in(ss, hexv):
    # hexv like "ff8c00" -> rgb(255,140,0)
    r = int(hexv[0:2], 16); g = int(hexv[2:4], 16); b = int(hexv[4:6], 16)
    return f"rgb({r},{g},{b})" in ss


print("== Case 1: ZZZ2-like preset WITH active colour (orange ff8c00) ==")
btn = HintButton("锁帧: 关")
_apply_style_to_ctrl(btn, build_preset(active_hex="ff8c00"))
print("  _bs_active_back set:", btn._bs_active_back)
btn.set_style("#7da2c8", False)
base_ss = btn.styleSheet()
print("  inactive stylesheet contains base 282828:", rgb_in(base_ss, "282828"))
print("  inactive stylesheet contains active ff8c00:", rgb_in(base_ss, "ff8c00"), "(应为 False)")
btn.set_style("#7da2c8", True)
act_ss = btn.styleSheet()
print("  ACTIVE stylesheet contains active ff8c00:", rgb_in(act_ss, "ff8c00"), "(应为 True)")
print("  ACTIVE stylesheet keeps radius 17:", "border-radius: 17px" in act_ss)
assert btn._bs_active_back is not None
assert rgb_in(base_ss, "282828") and not rgb_in(base_ss, "ff8c00")
assert rgb_in(act_ss, "ff8c00") and "border-radius: 17px" in act_ss
print("  >>> Case 1 PASS ✅")

print("\n== Case 2: sparse preset WITHOUT active colour (no regression) ==")
btn2 = HintButton("应用")
_apply_style_to_ctrl(btn2, build_preset())  # no active colour
print("  _bs_active_back:", btn2._bs_active_back, "(应为 None)")
btn2.set_style("#7da2c8", True)
ss2 = btn2.styleSheet()
print("  active=True still uses base 282828 (no active colour):", rgb_in(ss2, "282828"))
print("  active=True does NOT invent ff8c00:", not rgb_in(ss2, "ff8c00"))
assert btn2._bs_active_back is None
assert rgb_in(ss2, "282828") and not rgb_in(ss2, "ff8c00")
print("  >>> Case 2 PASS ✅")

print("\n== Case 3: chamfer preset WITH active colour (draw path) ==")
btn3 = HintButton("GPU浮动")
_apply_style_to_ctrl(btn3, build_preset(active_hex="ff8c00", cr=0))  # cr=0 -> chamfer
print("  _bs_active_back:", btn3._bs_active_back)
print("  _corner_radius:", btn3._corner_radius, "(应为 0 -> chamfer path)")
btn3.set_style("#7da2c8", True)
# chamfer path keeps transparent stylesheet; active colour is read at paint time.
print("  stylesheet transparent (chamfer):", "background: transparent" in btn3.styleSheet())
btn3.set_style("#7da2c8", False)
print("  toggling back does not crash; stylesheet still transparent:",
      "background: transparent" in btn3.styleSheet())
assert btn3._bs_active_back is not None
assert "background: transparent" in btn3.styleSheet()
print("  >>> Case 3 PASS ✅")

print("\nALL PASS ✅ — 激活态颜色已正确接到 HintButton")
