"""Check imports available in edit_overlay.py (core import pattern, QColorDialog/QFileDialog, NavItem)."""
EO = "src/ningbingjue/ui/edit_overlay.py"
lines = open(EO, encoding="utf-8").read().splitlines()
print("=== from ..core / import core lines ===")
for i, l in enumerate(lines, 1):
    if "core" in l and ("import" in l):
        print(f"  {i}: {l}")
print("\n=== QColorDialog / QFileDialog / NavItem / ClickableTitle / HintButton / _EditCard / _hex / get_theme_accent / PAGE_TITLE_SIZE / _MaskPreviewCanvas present? ===")
import re
text = open(EO, encoding="utf-8").read()
for name in ["QColorDialog", "QFileDialog", "class NavItem", "ClickableTitle",
             "class HintButton", "class _EditCard", "def _hex", "get_theme_accent",
             "PAGE_TITLE_SIZE", "_MaskPreviewCanvas", "Optional", "QTimer", "QSlider",
             "QCheckBox", "QFrame", "QSizePolicy"]:
    print(f"  {name}: {'YES' if name in text else 'NO'}")
