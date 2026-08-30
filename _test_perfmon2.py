import os, sys, traceback

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_perfmon_log.txt")
def log(*a):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(" ".join(str(x) for x in a) + "\n")

log("start")
try:
    root = os.path.abspath(".")
    from ningbingjue.app import Application
    log("imported Application")
    app = Application(script_root=root, headless=True)
    log("created app")
    app.boot()
    log("booted")
    log("registry perfmon:", "perfmon" in app.registry)
    log("blocks perfmon:", "perfmon" in app.blocks)
    cfg = app.config_manager.config
    log("cards tail:", cfg.cards[-3:])
    from ningbingjue.ui.panels import make_panel
    panel = make_panel("perfmon", app.blocks["perfmon"], "性能监控", "#7fb8ff",
                       block_key="perfmon", cfg=cfg)
    log("panel:", type(panel).__name__, "nav:", len(panel.nav_items()),
         "rows:", list(panel._rows.keys()))
    panel.sync()
    log("sync ok, window label:", panel._rows["window"].text())
    log("DONE")
except Exception:
    log("EXC:\n" + traceback.format_exc())
