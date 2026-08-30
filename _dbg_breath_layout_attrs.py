"""Inspect what 'card' / 'border' attributes BreathingEditPanel and LayoutEffectPanel actually expose."""
import ast, re

EO = "src/ningbingjue/ui/edit_overlay.py"
src = open(EO, encoding="utf-8").read()
tree = ast.parse(src)

# Helper: find class by name in any module, then dump its body lines
def find_class(name):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
        if isinstance(node, ast.ClassDef):
            for sub in ast.walk(node):
                if isinstance(sub, ast.ClassDef) and sub.name == name:
                    return sub
    return None

def get_class_lines(name, code_lines):
    cls = find_class(name)
    if cls is None:
        return None, None
    start = cls.lineno  # 1-based
    end = cls.end_lineno or start + 200
    return (start, end), "\n".join(code_lines[start - 1: end])

lines = src.splitlines()

def dump(name, around=200):
    rng, body = get_class_lines(name, lines)
    if rng is None:
        print(f"=== {name}: NOT FOUND ===")
        return
    print(f"=== {name}  lines {rng[0]}..{rng[1]} ===")
    print(body[:around * 60])

# Just dump attribute assignments in each target class
def collect_self_attrs(name):
    cls = find_class(name)
    if cls is None: return []
    attrs = {}
    for node in ast.walk(cls):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            v = node.attr
            if v.startswith("_"):
                attrs.setdefault(v, []).append(getattr(node, "lineno", 0))
    return attrs

for cls_name in ["BreathingEditPanel", "_TrayPreview", "_BreathPreview",
                 "LayoutEffectPanel", "_LayoutEffectCanvas", "MarginRowPanel"]:
    attrs = collect_self_attrs(cls_name)
    if not attrs:
        print(f"[{cls_name}] (not found or no self attrs)")
        continue
    card_keys = [k for k in attrs if "card" in k.lower() or "border" in k.lower() or "margin" in k.lower() or "bg" in k.lower() or "fill" in k.lower() or "dim" in k.lower() or "tray" in k.lower() or "image" in k.lower()]
    print(f"=== {cls_name}: relevant self attrs (card/border/margin/bg/fill/dim/tray/image) ===")
    for k in sorted(card_keys):
        print(f"  self.{k}  (lines {attrs[k][:5]})")
