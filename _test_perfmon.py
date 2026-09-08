import os
from ningbingjue.app import Application

root = os.path.abspath(".")
app = Application(script_root=root, headless=True)
app.boot()
print("registry has perfmon:", "perfmon" in app.registry)
print("blocks has perfmon:", "perfmon" in app.blocks)
order = app.expanded_order() if hasattr(app, "expanded_order") else None
print("expanded_order includes perfmon:", "perfmon" in (order or app.config_manager.config.cards))
cfg = app.config_manager.config
print("config.cards tail:", cfg.cards[-3:])

from ningbingjue.ui.panels import make_panel
panel = make_panel("perfmon", app.blocks["perfmon"], "性能监控", "#7fb8ff",
                   block_key="perfmon", cfg=cfg)
print("panel built:", type(panel).__name__, "| nav_items:", len(panel.nav_items()))
print("panel._rows keys:", list(panel._rows.keys()))
panel.sync()
print("sync (no window) ok, window label:", panel._rows["window"].text())

# Verify the perfmon_snapshot aggregation on the real window if GUI built.
win = getattr(app, "_window", None)
if win is not None and hasattr(win, "perfmon_snapshot"):
    snap = win.perfmon_snapshot()
    print("perfmon_snapshot keys:", sorted(snap.keys()))
    print("  load/state default:", snap["load"], "| frame_state:", snap["frame_state"],
          "| gpu_lock_state:", snap["gpu_lock_state"])
else:
    print("no GUI window built (headless), skipping perfmon_snapshot live call")
print("DONE")
