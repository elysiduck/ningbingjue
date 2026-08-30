"""Robust extraction: save_all config keys, margin application, config_manager accessor."""
import re, glob

EO = "src/ningbingjue/ui/edit_overlay.py"
AW = "src/ningbingjue/ui/app_window.py"
src = open(EO, encoding="utf-8").read()
aw = open(AW, encoding="utf-8").read()

def extract_def(text, name):
    """Return body of first top-level-ish def <name> in <text>."""
    m = re.search(rf"^(\s*)def {re.escape(name)}\(", text, re.M)
    if not m:
        # try any indentation
        m = re.search(rf"def {re.escape(name)}\(", text)
    if not m:
        return None
    start = m.start()
    # compute indent of def line
    line_start = text.rfind("\n", 0, start) + 1
    def_line = text[line_start:start]
    indent = len(def_line) - len(def_line.lstrip())
    # find end: next line with indent <= indent (and non-blank) after def
    i = text.find("\n", start) + 1
    end = i
    while i < len(text):
        nl = text.find("\n", i)
        if nl == -1:
            nl = len(text)
        seg = text[i:nl]
        if seg.strip() == "":
            i = nl + 1
            end = i
            continue
        cur = len(seg) - len(seg.lstrip())
        if cur <= indent:
            break
        end = nl + 1
        i = nl + 1
    return text[line_start:end]

# 1) LayoutEditPanel.save_all — find by class then def
m = re.search(r"class LayoutEditPanel\b", src)
le = src[m.start():]
sa = extract_def(le, "save_all")
print("===== LayoutEditPanel.save_all =====")
print(sa[:1800] if sa else "NOT FOUND")

# 2) LayoutEffectPanel.save_all
m2 = re.search(r"class LayoutEffectPanel\b", src)
lp = src[m2.start():]
sa2 = extract_def(lp, "save_all")
print("\n===== LayoutEffectPanel.save_all =====")
print(sa2[:1400] if sa2 else "NOT FOUND")

# 3) config_manager accessor usage: search across all py for ".set(" with card_/editor_dim/margin keys
print("\n===== config keys referenced (card_bg_*/card_fill_*/editor_dim/card_outer_margin/margin) =====")
for f in glob.glob("src/ningbingjue/**/*.py", recursive=True):
    t = open(f, encoding="utf-8").read()
    for mm in re.finditer(r"\.set\(\s*[\"'](card_bg_[a-z_]*|card_fill_[a-z_]*|editor_dim[a-z_]*|card_outer_margin|outer_margin|margin[a-z_]*)[\"']", t):
        print(f"  {f}: {mm.group(0)[:80]}")

# 4) how margin is applied to real window (search app_window for margin / apply in layout)
print("\n===== app_window: margin application =====")
for mm in re.finditer(r"def (apply_outer_margin|apply_margin|relayout|apply_layout|_apply_outer)", aw):
    seg = aw[mm.start(): mm.start()+400]
    print(f"--- {mm.group(1)} ---\n{seg}\n")

# 5) config_manager accessor on Application
print("\n===== Application.config_manager accessor =====")
for f in ["src/ningbingjue/app.py", "src/ningbingjue/ui/app_window.py"]:
    try:
        t = open(f, encoding="utf-8").read()
    except FileNotFoundError:
        continue
    for mm in re.finditer(r"(def config_manager|self\._config_manager\s*=|config_manager\s*[:=])", t):
        seg = t[mm.start(): mm.start()+200]
        print(f"[{f}] {seg}\n")
