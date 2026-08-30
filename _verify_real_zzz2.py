"""Verify ZZZ2 against the user's REAL on-disk data (no synthetic data).

Steps:
  1. Load the real <script_root>/buttons/*.json (presets, theme, map).
  2. Print ZZZ2 CornerRadius from disk + the active theme's ls# bindings.
  3. Build REAL HUD button instances (RoundedButton + QPushButton) for
     ls#0/ls#1/ls#2, apply_styles_to_buttons() exactly like HUD rebuild does.
  4. Read back each button's rounded-ness.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

REAL = (r"D:\下载\localsend\凝冰结魄-覆盖任务栏-修复-完全解耦-备份-20260717172824"
        r"\凝冰结魄-覆盖任务栏-修复-完全解耦-备份")

from PyQt6.QtWidgets import QApplication, QPushButton
from ningbingjue.ui.rounded_button import RoundedButton
from ningbingjue.core.color import styles as _BS
from ningbingjue.core.color.styles import (
    apply_styles_to_buttons, PER_BUTTON_PRESETS, BUTTON_PRESET_MAP,
    BUTTON_STYLES, ACTIVE_STYLE_NAME,
)

app = QApplication(sys.argv)

def hr(t):
    print("\n" + "=" * 64)
    print(t)
    print("=" * 64)

hr("1) 加载真实磁盘数据")
_BS.load_all_button_data(REAL)

zzz2 = PER_BUTTON_PRESETS.get("ZZZ2")
print(f"  ZZZ2 存在: {zzz2 is not None}")
if zzz2 is not None:
    print(f"  ZZZ2.CornerRadius (磁盘) = {zzz2.corner_radius}")
    print(f"  ZZZ2.BorderThickness    = {zzz2.border_thickness}")
    print(f"  ZZZ2.colors keys        = {sorted(zzz2.colors.keys())}")
print(f"  ACTIVE_STYLE_NAME        = {ACTIVE_STYLE_NAME!r}")
theme = BUTTON_STYLES.get(ACTIVE_STYLE_NAME, {})
print(f"  主题[{ACTIVE_STYLE_NAME}].ls#0 = {theme.get('ls#0')!r}")
print(f"  主题[{ACTIVE_STYLE_NAME}].ls#1 = {theme.get('ls#1')!r}")
print(f"  主题[{ACTIVE_STYLE_NAME}].ls#2 = {theme.get('ls#2')!r}")
print(f"  BUTTON_PRESET_MAP.ls#0        = {BUTTON_PRESET_MAP.get('ls#0')!r}")

hr("2) 用真实数据对真实按钮实例套用（模拟 HUD 重建）")
rb0 = RoundedButton(); rb0.setText("LS0")
pb1 = QPushButton("LS1")
pb2 = QPushButton("LS2")
buttons = [rb0, pb1, pb2]
key_map = {id(b): f"ls#{i}" for i, b in enumerate(buttons)}
apply_styles_to_buttons(buttons, key_map)

hr("3) 读回按钮信息 —— 是否圆角？")
cr0 = getattr(rb0, "corner_radius", "N/A")
print(f"  RoundedButton ls#0 : corner_radius = {cr0}")
ss1 = pb1.styleSheet()
ss2 = pb2.styleSheet()
print(f"  QPushButton   ls#1 : styleSheet 含 'border-radius' = {'border-radius' in ss1}")
import re
m1 = re.search(r"border-radius:\s*(\d+)px", ss1)
print(f"  QPushButton   ls#1 : border-radius = {m1.group(1) if m1 else 'NONE'} px")
m2 = re.search(r"border-radius:\s*(\d+)px", ss2)
print(f"  QPushButton   ls#2 : border-radius = {m2.group(1) if m2 else 'NONE'} px")

hr("判定")
rounded = (cr0 == 17) and bool(m1) and (int(m1.group(1)) == 17)
print("  >>> 真实数据 + 真实代码套用后，按钮是否为圆角(17):", "是 ✅" if rounded else "否 ❌")
