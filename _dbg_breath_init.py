"""Find what self.* variable each _EditCard in BreathingEditPanel is stored as."""
import re

EO = "src/ningbingjue/ui/edit_overlay.py"
src = open(EO, encoding="utf-8").read()
lines = src.splitlines()

# Find BreathingEditPanel class block boundaries
m_start = re.search(r"^class BreathingEditPanel\b", src, re.M)
rest = src[m_start.start():]
m_end = re.search(r"\nclass \w+", rest[1:])
breath_text = rest[: m_end.start() + 1 if m_end else len(rest)]
breath_start_line = src[:m_start.start()].count("\n") + 1

# Now within that block, find every assignment of the form
#   self.<NAME> = _EditCard("...", self)
# plus any standalone _EditCard("...", self) calls (unassigned)
print(f"=== _EditCard instantiations in BreathingEditPanel (class starts line {breath_start_line}) ===")
assigns = re.finditer(r"self\.(\w+)\s*=\s*_EditCard\s*\(\s*['\"]([^'\"]+)['\"]", breath_text)
for m in assigns:
    line_in_class = breath_text[: m.start()].count("\n") + 1
    abs_line = breath_start_line + line_in_class - 1
    print(f"  line {abs_line}: self.{m.group(1)} = _EditCard({m.group(2)!r})")
unbound = re.findall(r"_EditCard\(\s*['\"]([^'\"]+)['\"]", breath_text)
bound_titles = set(re.findall(r"self\.\w+\s*=\s*_EditCard\(\s*['\"]([^'\"]+)['\"]", breath_text))
print("--- unassigned _EditCard(title) in BreathingEditPanel (titles not yet bound to self.*) ---")
seen = set()
for t in unbound:
    if t in bound_titles or t in seen: continue
    seen.add(t)
    print(f"  unassigned: {t}")

# Also dump lines 5720-5810 (the __init__ start of BreathingEditPanel) to see construction order
print("\n=== BreathingEditPanel.__init__ body lines 5725..5820 ===")
for i in range(5725, min(5820, len(lines) + 1)):
    if i - 1 < len(lines):
        print(f"  {i}: {lines[i-1]}")
