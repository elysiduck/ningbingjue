"""Find config keys in save_all, the app_window HUD apply methods, and config_manager API."""
import re

EO = "src/ningbingjue/ui/edit_overlay.py"
AW = "src/ningbingjue/ui/app_window.py"
src = open(EO, encoding="utf-8").read()
aw = open(AW, encoding="utf-8").read()
lines = src.splitlines()

def fn_body(name, rng=None):
    m = re.search(rf"def {re.escape(name)}\(", src if rng is None else src[rng[0]:rng[1]], re.M)
    if not m: return None
    base = src[:m.start()].count("\n") + 1 if rng is None else 0
    s = m.start() if rng is None else m.start()+rng[0]
    i = src[:s].count("\n") if rng is None else 0
    # reconstruct
    full = src if rng is None else src[rng[0]:rng[1]]
    mm = re.search(rf"def {re.escape(name)}\(", full)
    start = full[:mm.start()].count("\n") + 1
    j = start
    body = full.splitlines()
    base_indent = len(mm.group(0)) - len(mm.group(0).lstrip())
    # Actually def line indent
    def_line = body[start-1]
    indent = len(def_line) - len(def_line.lstrip())
    while j < len(body):
        line = body[j]
        if line.strip() == "":
            j += 1
            continue
        cur = len(line) - len(line.lstrip())
        if cur <= indent and j > start-1:
            break
        j += 1
    return "\n".join(body[start-1:j-1])

# save_all in LayoutEditPanel
print("##### LayoutEditPanel.save_all (rough) #####")
m = re.search(r"class LayoutEditPanel\b", src)
rest = src[m.start():]
for fm in re.finditer(r"def save_all\(", rest):
    seg = rest[fm.start(): fm.start()+1400]
    print(seg)
    break

# LayoutEffectPanel.save_all
print("\n##### LayoutEffectPanel.save_all #####")
m2 = re.search(r"class LayoutEffectPanel\b", src)
rest2 = src[m2.start():]
for fm in re.finditer(r"def save_all\(", rest2):
    seg = rest2[fm.start(): fm.start()+1000]
    print(seg)
    break

# app_window apply methods
print("\n##### app_window apply_* (card/fill/dim/margin/outer) #####")
for name in ["apply_card_bg", "apply_card_fill", "apply_editor_dim", "apply_margin", "apply_outer_margin", "apply_card_outer_margin"]:
    for mm in re.finditer(rf"def {name}\(", aw):
        seg = aw[mm.start(): mm.start()+500]
        print(f"\n--- {name} ---\n{seg}")

# config_manager accessor on app
print("\n##### app.config_manager accessor (app.py) #####")
import glob
for f in glob.glob("src/ningbingjue/**/*.py", recursive=True):
    t = open(f, encoding="utf-8").read()
    if "config_manager" in t and ("def config_manager" in t or "self._config_manager" in t):
        # show first occurrence of def config_manager or assignment
        for mm in re.finditer(r"(def config_manager|self\._config_manager\s*=)", t):
            seg = t[mm.start(): mm.start()+300]
            print(f"[{f}] {seg}")
