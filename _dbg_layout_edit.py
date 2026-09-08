"""Locate each of the 5 page-appearance settings: which panel owns it, what's the widget holder."""
import re

EO = "src/ningbingjue/ui/edit_overlay.py"
src = open(EO, encoding="utf-8").read()
lines = src.splitlines()

def find_class_range(name):
    m = re.search(rf"^class {re.escape(name)}\b", src, re.M)
    if not m: return None
    rest = src[m.start():]
    m_end = re.search(r"\nclass \w+", rest[1:])
    end = m.start() + (m_end.start() + 1 if m_end else len(rest))
    start_line = src[:m.start()].count("\n") + 1
    end_line = src[:end].count("\n") + 1
    return start_line, end_line

panels = ["BreathingEditPanel", "LayoutEffectPanel", "LayoutEditPanel"]
print("=== Top-level panels in edit_overlay.py (line ranges) ===")
for p in panels:
    r = find_class_range(p)
    print(f"  {p}: lines {r[0]}..{r[1]}" if r else f"  {p}: NOT FOUND")

# Look at how LayoutEditPanel stores its card-area, fill, dim, card_bg-related widgets
print("\n=== LayoutEditPanel — _EditCard instantiations and 'card' self assignments ===")
r = find_class_range("LayoutEditPanel")
if r:
    block = src.splitlines()[r[0] - 1: r[1]]
    for i, line in enumerate(block):
        L = r[0] + i
        if "_EditCard(" in line:
            # follow next 2 lines for assignment
            print(f"  {L}: {line.rstrip()[:140]}")
        # assignment to self.<...>_card
        m = re.match(r"\s+self\.(\w*card\w*)\s*=", line)
        if m:
            print(f"  {L}: assigns self.{m.group(1)}")

# Also dump every self.* in LayoutEditPanel that mentions card/fill/dim/bg/tint
print("\n=== LayoutEditPanel — self.* (containing card/fill/dim/bg/tint/mask/scrim) ===")
import ast
tree = ast.parse(src)
for node in tree.body:
    if isinstance(node, ast.ClassDef) and node.name == "LayoutEditPanel":
        seen = {}
        for n in ast.walk(node):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self":
                v = n.attr
                if any(k in v for k in ("card","fill","dim","bg","tint","mask","scrim","tray","editor_dim","_margin","_gap")):
                    seen.setdefault(v, []).append(getattr(n,"lineno",0))
        for k in sorted(seen.keys()):
            print(f"  self.{k}    (lines {seen[k][:8]})")
        break
