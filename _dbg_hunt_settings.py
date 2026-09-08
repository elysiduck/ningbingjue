"""Hunt for the actual 5 page-appearance settings: card area bg / replacement bg / card fill / dim / margin."""
import re, subprocess

EO = "src/ningbingjue/ui/edit_overlay.py"
src = open(EO, encoding="utf-8").read()

KW = {
    "card area bg / 卡片区背景": r"card_bg|card_bg_image|cardarea|tint|背景底|背板|background_card|page_bg|page_background|_bg_image|_bg_pixmap|底板|底色|背板色",
    "replacement bg / 替换底色": r"replac|替换底|replace_bg|替换背景|alt_bg",
    "card fill / 单卡填充": r"card_fill|单卡填充|卡片填充|card_bg_color|fill_color|卡片填充色|卡填充|self\.fill|self\.fill_card",
    "dim mask / 编辑蒙版": r"_dim|dim_mask|edit_dim|蒙版|mask_opacity|dim_opa|dim_pct|scrim|scrim_alpha",
    "margin / 外圈留白": r"_margin|外圈留白|留白|card_outer_margin|outer_margin|外圈边距|外边距",
}

for label, pat in KW.items():
    print(f"=== {label} ===")
    pat_re = re.compile(pat, re.I)
    matches = []
    for i, line in enumerate(src.splitlines(), 1):
        if pat_re.search(line):
            matches.append((i, line.rstrip()))
    # de-dupe nearby lines: only show first 8 + last 3
    if matches:
        for i, l in matches[:8]:
            print(f"  {i}: {l[:140]}")
        if len(matches) > 8:
            print(f"  ... ({len(matches)-8} more)")
            for i, l in matches[-3:]:
                print(f"  {i}: {l[:140]}")
    else:
        print("  (no match)")
    print()

# Also: what _EditCard instances does BreathingEditPanel create?
print("\n=== Looking for _EditCard(...) instantiations inside BreathingEditPanel ===")
# Find class block range
m_start = src.find("class BreathingEditPanel")
# naive: until "class " after
rest = src[m_start:]
m_end = rest.find("\nclass ", 1)
breath_block = src[m_start:m_start + m_end] if m_end > 0 else rest
hits = re.findall(r"_EditCard\s*\([^)]*\)", breath_block)
for h in hits[:20]:
    print(" ", h[:120])
