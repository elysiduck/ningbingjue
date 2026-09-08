"""Direct grep-style extraction via Python of save_all bodies + margin + config persistence."""
import re, glob

files = glob.glob("src/ningbingjue/**/*.py", recursive=True)

def print_def(text, name, srcfile, maxlen=2200):
    m = re.search(rf"^(\s*)def {re.escape(name)}\(", text, re.M)
    if not m: 
        print(f"  [{srcfile}] def {name}: NOT FOUND"); return
    line_start = text.rfind("\n", 0, m.start()) + 1
    indent = len(text[line_start:m.start()])  # leading spaces
    i = text.find("\n", m.start()) + 1
    end = i
    while i < len(text):
        nl = text.find("\n", i)
        if nl == -1: nl = len(text)
        seg = text[i:nl]
        if seg.strip() == "":
            end = nl + 1; i = nl + 1; continue
        cur = len(seg) - len(seg.lstrip())
        if cur <= indent: break
        end = nl + 1; i = nl + 1
    print(f"=== {srcfile}: def {name} ===")
    print(text[line_start:end][:maxlen])

for f in files:
    t = open(f, encoding="utf-8").read()
    if "def save_all" in t:
        print_def(t, "save_all", f)

# margin application in app_window
aw = open("src/ningbingjue/ui/app_window.py", encoding="utf-8").read()
print("\n=== app_window: search 'outer_margin' / 'card_outer_margin' / 'apply_margin' ===")
for mm in re.finditer(r"(outer_margin|card_outer_margin|apply_margin|setContentsMargins|_layout_margin)", aw):
    s = max(0, mm.start()-120)
    e = min(len(aw), mm.end()+160)
    print(f"  ...{aw[s:e]}...")

# config persistence of card_bg_*
print("\n=== project-wide: how card_bg_*/card_fill_*/editor_dim_* get persisted ===")
for f in files:
    t = open(f, encoding="utf-8").read()
    for mm in re.finditer(r"(card_bg_enabled|card_bg_mode|card_bg_color|card_bg_image|card_fill_hex|card_fill_alpha|editor_dim|card_outer_margin)", t):
        s = max(0, mm.start()-90)
        e = min(len(t), mm.end()+90)
        snippet = t[s:e].replace("\n", " ")
        print(f"  {f}: …{snippet}…")
