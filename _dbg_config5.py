"""Find ALL references to card_bg_enabled / card_fill / editor_dim / card_outer_margin to understand config read/write + load pattern."""
import re, glob

files = glob.glob("src/ningbingjue/**/*.py", recursive=True)
keys = ["card_bg_enabled", "card_bg_mode", "card_bg_color", "card_bg_image",
        "card_fill_hex", "card_fill_alpha", "editor_dim", "card_outer_margin", "card_gap"]
seen = {k: [] for k in keys}
for f in files:
    t = open(f, encoding="utf-8").read()
    for k in keys:
        for mm in re.finditer(re.escape(k), t):
            s = max(0, mm.start()-50); e = min(len(t), mm.end()+50)
            seen[k].append(f"  {f}: …{t[s:e]}…")

for k, hits in seen.items():
    print(f"\n##### {k}  ({len(hits)} hits) #####")
    for h in hits[:6]:
        print(h)

# Also: how does LayoutEditPanel __init__ seed _card_bg_enabled default? search around 4140..4180 and 5160..5180
print("\n=== LayoutEditPanel __init__ tail (where _card_bg_*/_card_fill_*/_editor_dim_* are seeded) ===")
le = open("src/ningbingjue/ui/edit_overlay.py", encoding="utf-8").read()
m = re.search(r"class LayoutEditPanel\b", le)
rest = le[m.start(): m.start()+2000]  # first 2000 chars of class ~ up to __init__ end
# find self._card_bg_enabled = and self._editor_dim_alpha = and self._card_fill_hex = assignments
for mm in re.finditer(r"self\.(_card_bg_enabled|_card_bg_mode|_card_bg_color_hex|_card_bg_image|_card_fill_hex|_card_fill_alpha|_editor_dim_alpha|_editor_dim_slider)\s*=\s*", le):
    s = max(0, mm.start()-30); e = min(len(le), mm.end()+80)
    print(f"  …{le[s:e]}…")

# Look for config read in LayoutEditPanel load (def load_config / load_all / refresh)
print("\n=== LayoutEditPanel load/config read methods ===")
for mm in re.finditer(r"def (load_config|load_all|_load|refresh|read_config)\(", le):
    seg = le[mm.start(): mm.start()+300]
    print(f"--- {mm.group(1)} ---\n{seg}\n")
