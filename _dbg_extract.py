"""Extract the 4 existing push functions + config keys + HUD-push path, and the ThemeMaskPanel call site."""
import re

EO = "src/ningbingjue/ui/edit_overlay.py"
src = open(EO, encoding="utf-8").read()
lines = src.splitlines()

def fn_body(name, cls=None):
    """Return (start_line, end_line, text) of first def <name> optionally in <cls>."""
    tree = None
    # find all defs by regex (cheap)
    for m in re.finditer(rf"^(\s*)def {re.escape(name)}\(", src, re.M):
        # get the def line
        def_start = src[:m.start()].count("\n") + 1
        # walk forward to find dedent end
        i = def_start
        base_indent = len(m.group(1))
        # find first non-empty line after def to get body indent
        # collect until line with indent <= base_indent and not blank
        j = i
        while j < len(lines):
            line = lines[j]
            if line.strip() == "":
                j += 1
                continue
            cur_indent = len(line) - len(line.lstrip())
            if cur_indent <= base_indent and j > i:
                break
            j += 1
        return def_start, j, "\n".join(lines[i-1:j-1])
    return None

targets = [
    "_on_card_bg_enabled", "_push_card_bg_live", "_on_card_bg_color",
    "_on_card_bg_mode", "_pick_card_bg_color_custom", "_on_editor_dim",
    "_push_editor_dim_live", "_push_card_fill_live", "_on_card_fill",
    "_pick_card_fill_custom", "_sync_card_bg_controls",
]
for t in targets:
    b = fn_body(t)
    if b:
        s, e, txt = b
        print(f"\n##### {t}  (lines {s}..{e}) #####")
        print(txt[:1400])

# LayoutEffectPanel._on_margin
print("\n##### LayoutEffectPanel._on_margin #####")
m = re.search(r"class LayoutEffectPanel\b", src)
rest = src[m.start():]
m2 = re.search(r"def _on_margin\(", rest)
if m2:
    seg = rest[m2.start(): m2.start()+900]
    print(seg)

# ThemeMaskPanel call site (around 8522)
print("\n##### ThemeMaskPanel call site (around 8515..8535) #####")
for i in range(8513, min(8535, len(lines)+1)):
    if i-1 < len(lines):
        print(f"{i}: {lines[i-1]}")
