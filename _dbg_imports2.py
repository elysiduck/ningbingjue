"""Find HintButton / NavItem imports and confirm core/__init__.py + no name clash with 'style'."""
import os, re, glob
EO = "src/ningbingjue/ui/edit_overlay.py"
text = open(EO, encoding="utf-8").read()
print("=== HintButton / NavItem imports ===")
for m in re.finditer(r"^\s*(from|import)\s+[^\n]*\b(HintButton|NavItem)\b[^\n]*", text, re.M):
    print("  ", m.group(0).strip())
print("\n=== 'style' name already used in edit_overlay.py? ===")
print("  occurrences of standalone 'style':", len(re.findall(r"(?<![\w.])style(?![\w.])", text)))
print("\n=== core/__init__.py exists? ===")
print("  core/__init__.py:", os.path.exists("src/ningbingjue/core/__init__.py"))
print("  core/style.py would be new; core has:", [f for f in os.listdir("src/ningbingjue/core") if f.endswith('.py')])
print("\n=== top imports region (lines 60..90) ===")
for i, l in enumerate(text.splitlines()[59:90], 60):
    print(f"  {i}: {l}")
