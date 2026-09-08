"""Read _MaskPreviewCanvas fully to see what owner attributes paintEvent/_draw_window use."""
EO = "src/ningbingjue/ui/edit_overlay.py"
lines = open(EO, encoding="utf-8").read().splitlines()
print("=== _MaskPreviewCanvas class (find start) ===")
for i, l in enumerate(lines, 1):
    if "class _MaskPreviewCanvas" in l:
        print(f"  starts at line {i}")
        start = i
        break
# print 6600..6860
print("=== _MaskPreviewCanvas body 6600..6860 ===")
for i in range(6600, min(6860, len(lines)+1)):
    if i-1 < len(lines):
        print(f"{i}: {lines[i-1]}")
