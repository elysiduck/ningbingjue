"""Find: (1) app.config_manager accessor, (2) margin consumption in app_window, (3) config keys read on load for card/fill/dim/margin."""
import re, glob

files = glob.glob("src/ningbingjue/**/*.py", recursive=True)

print("=== 1) config_manager accessor / ConfigManager set API ===")
for f in files:
    t = open(f, encoding="utf-8").read()
    for mm in re.finditer(r"config_manager\s*=\s*ConfigManager|def config_manager|self\.config_manager\s*=", t):
        s = max(0, mm.start()-60); e = min(len(t), mm.end()+120)
        print(f"  {f}: …{t[s:e]}…")

print("\n=== 2) app_window: margin / outer_margin consumption ===")
aw = open("src/ningbingjue/ui/app_window.py", encoding="utf-8").read()
for mm in re.finditer(r"(outer_margin|card_outer_margin|setContentsMargins\(|_layout_margin|apply_outer)", aw):
    s = max(0, mm.start()-150); e = min(len(aw), mm.end()+200)
    print(f"  …{aw[s:e]}…\n")

print("=== 3) config_manager.get( with card/fill/dim/margin keys, project-wide ===")
for f in files:
    t = open(f, encoding="utf-8").read()
    for mm in re.finditer(r"config_manager\.get\(\s*[\"']([a-z_]*margin[a-z_]*|[a-z_]*(card_bg|card_fill|editor_dim)[a-z_]*)[\"']", t):
        s = max(0, mm.start()-40); e = min(len(t), mm.end()+60)
        print(f"  {f}: …{t[s:e]}…")

print("\n=== 4) ConfigManager.set / .get signatures ===")
for f in files:
    t = open(f, encoding="utf-8").read()
    if "class ConfigManager" in t:
        for mm in re.finditer(r"def (set|get)\(", t):
            seg = t[mm.start(): mm.start()+240]
            print(f"  [{f}] {seg}\n")
