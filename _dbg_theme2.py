"""Dump remaining ThemeMaskPanel methods (_save/_cancel/_set_highlight/hideEvent/showEvent/_load)."""
EO = "src/ningbingjue/ui/edit_overlay.py"
lines = open(EO, encoding="utf-8").read().splitlines()
print("=== ThemeMaskPanel methods (7048..7240) ===")
for i in range(7048, min(7240, len(lines)+1)):
    if i-1 < len(lines):
        print(f"{i}: {lines[i-1]}")
