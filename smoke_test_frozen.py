#!/usr/bin/env python3
"""Smoke test the frozen bundle WITHOUT triggering the UAC manifest.

We emulate PyInstaller's frozen layout by pointing NINGBING_ROOT at the
_internal directory (exactly what frozen_launcher.py does at runtime) and
importing the bundled package/entry from there. This exercises the real
import graph, resource resolution (ryzenadj), and config write-back.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
INTERNAL = os.path.join(HERE, "dist", "NingBingJue", "_internal")
if not os.path.isdir(INTERNAL):
    print("FAIL: _internal not found at", INTERNAL)
    sys.exit(2)

# Emulate frozen launcher behaviour.
os.environ["NINGBING_ROOT"] = INTERNAL
if INTERNAL not in sys.path:
    sys.path.insert(0, INTERNAL)

errors = []

def check(label, cond):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        errors.append(label)

# 1) Import graph (the real entry module).
try:
    import run
    check("import run (entry module)", True)
except Exception as e:  # noqa
    check("import run (entry module): " + repr(e), False)

# 2) Import the package + key submodules.
try:
    from ningbingjue import app, platform as _plat  # noqa
    check("import ningbingjue.app + platform", True)
except Exception as e:  # noqa
    check("import ningbingjue.app + platform: " + repr(e), False)

# 3) ryzenadj resource resolution points at the bundled binary.
try:
    from ningbingjue import app as nb_app
    adj = nb_app.build_ryzenadj(INTERNAL)
    check("ryzenadj bundled binary available", adj.available)
    exe = adj._resolve_exe()
    check("ryzenadj._resolve_exe() -> bundled binary",
          exe is not None and os.path.exists(exe))
    print("     ->", exe)
except Exception as e:  # noqa
    check("ryzenadj resolution: " + repr(e), False)

# 4) Config write-back to NINGBING_ROOT (portable data).
try:
    cfg_dir = os.path.join(INTERNAL, "config")
    os.makedirs(cfg_dir, exist_ok=True)
    probe = os.path.join(cfg_dir, "_smoke_probe.json")
    with open(probe, "w", encoding="utf-8") as f:
        f.write("{}")
    check("config write-back to NINGBING_ROOT/config", os.path.exists(probe))
    try:
        os.remove(probe)
    except OSError:
        pass  # sandbox may intercept delete; write-back itself succeeded
except Exception as e:  # noqa
    check("config write-back: " + repr(e), False)

# 5) Headless engine self-test boot (exercises boot() without a display).
try:
    rc = run.run_headless(INTERNAL)
    check("run.run_headless() engine self-test passes", rc == 0)
    print("     -> rc =", rc)
except SystemExit as e:
    check("run.run_headless() SystemExit clean", e.code in (0, None))
    print("     -> SystemExit code =", e.code)
except Exception as e:  # noqa
    check("run.run_headless(): " + repr(e), False)

print()
if errors:
    print("SMOKE RESULT: FAILED ->", "; ".join(errors))
    sys.exit(1)
print("SMOKE RESULT: PASS")
