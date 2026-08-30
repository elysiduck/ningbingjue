#!/usr/bin/env python3
"""Pinpoint the page-1 -> page-2 switch hang/crash without app.exec().

We boot the GUI (offscreen), then synchronously drive the EXACT rebuild path
the gamepad shoulder button takes, with a hard watchdog thread so a deadlock
cannot hide.  Prints a trace after every step so we can see which call blocks
or raises.
"""
import os
import sys
import faulthandler
import threading

faulthandler.enable()

ROOT = r"C:\Users\13567\WorkBuddy\2026-07-18-10-22-49\ningbingjue"
os.environ["NINGBING_ROOT"] = ROOT
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(ROOT, "src"))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt, QTimer

QApplication.setHighDpiScaleFactorRoundingPolicy(
    Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
)
app = QApplication(sys.argv)

# Headless: the admin-warning QMessageBox.warning() blocks forever waiting for
# a click that never comes offscreen.  Stub it so boot's showEvent can finish.
from PyQt6.QtWidgets import QMessageBox
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)


def _log(msg):
    print("[repro]", msg, flush=True)


# Watchdog: if the main thread is stuck for >25s, dump stacks and exit.
def _watchdog():
    import time
    time.sleep(25)
    _log("WATCHDOG fired — main thread appears deadlocked")
    faulthandler.dump_traceback()
    os._exit(2)


threading.Thread(target=_watchdog, daemon=True).start()

_log("importing Application")
from ningbingjue.app import Application

_log("booting")
config = os.path.join(ROOT, "layout.json")
application = Application(ROOT, headless=False, config_path=config).boot()
_log("boot done")
window = application._window
ctrl = window._controller
_log("pages = %r" % application.render_pages())
_log("current_page = %d" % application.nav.current_page)

_log("driving REAL gamepad path ctrl._next_page() (advances nav.current_page) ...")
try:
    ctrl._next_page()
except Exception:
    import traceback
    _log("EXCEPTION in _next_page:")
    traceback.print_exc()
_log("_next_page returned; current_page = %d" % application.nav.current_page)
_log("panels now = %r" % [type(p).__name__ for p in window.panels])

from PyQt6.QtCore import QEventLoop
_log("processEvents x5 to fire deferred sync_all + paints + smt thread ...")
loop = QEventLoop()
for i in range(5):
    app.processEvents()
    QTimer.singleShot(20, loop.quit)
    loop.exec()
_log("processEvents done; current_page = %d" % application.nav.current_page)
_log("panels after = %r" % [type(p).__name__ for p in window.panels])
_log("REPRO_OK")
