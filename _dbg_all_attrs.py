"""Dump all self._* attributes of BreathingEditPanel + LineEffectPanel, then dump their __init__ bodies."""
import ast

EO = "src/ningbingjue/ui/edit_overlay.py"
src = open(EO, encoding="utf-8").read()
tree = ast.parse(src)
lines = src.splitlines()

def find_class(name):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
        if isinstance(node, ast.ClassDef):
            for sub in ast.walk(node):
                if isinstance(sub, ast.ClassDef) and sub.name == name:
                    return sub
    return None

def dump_init(name):
    cls = find_class(name)
    if cls is None:
        print(f"=== {name}: NOT FOUND ==="); return
    for n in cls.body:
        if isinstance(n, ast.FunctionDef) and n.name == "__init__":
            start = n.lineno; end = n.end_lineno or start + 100
            print(f"=== {name}.__init__  lines {start}..{end} ===")
            print("\n".join(lines[start - 1: end]))
            return
    print(f"=== {name}: NO __init__ ===")

# Dump ALL self.* assignments in BreathingEditPanel
for cls_name in ["BreathingEditPanel", "LayoutEffectPanel", "_LayoutEffectPanel"]:
    cls = find_class(cls_name)
    if cls is None: continue
    print(f"--- {cls_name}: all self.* (assignments AND attribute reads) ---")
    seen = {}
    for n in ast.walk(cls):
        if isinstance(n, ast.Attribute):
            v = n.attr
            seen.setdefault(v, []).append(getattr(n, "lineno", 0))
    for k in sorted(seen.keys()):
        print(f"  {k}    (lines {seen[k][:6]})")
    print()
