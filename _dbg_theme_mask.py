"""Inspect ThemeMaskPanel.__init__ and BreathingEditPanel attributes on REAL disk."""
import re, sys

EO = "src/ningbingjue/ui/edit_overlay.py"
lines = open(EO, encoding="utf-8").read().splitlines()

# 1) theme_mask_panel.__init__ body
print("=== ThemeMaskPanel.__init__ ~lines 6850..7050 ===")
for i in range(6850, min(7050, len(lines)) + 1):
    if i - 1 < len(lines):
        print(f"{i}: {lines[i-1]}")
