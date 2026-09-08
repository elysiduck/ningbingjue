"""Regression test for the "switch page via gamepad -> silent crash" bug.

Boots the *real* GUI offscreen and drives repeated page switches through the
exact :meth:`InteractionController._next_page` method the RB/LB shoulder
buttons call, then asserts the process — and the window/controller objects —
stay alive afterwards.

Why a test and not just try/except: the crash is a native access violation
(deleteLater'd widget touched under a translucent frameless window) that Python
cannot catch.  The only reliable detection is "did the process survive?", so we
use a watchdog thread that hard-aborts if the main thread deadlocks, and assert
the C++ objects are not deleted.  If the bug regresses, the test process dies
(non-zero) or the assertions fail — either way CI goes red.

Run:  pytest tests/test_page_switch_survival.py
"""

from __future__ import annotations

import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# MUST be set before importing PyQt6 — offscreen gives us a null surface so the
# GUI builds without a real display (CI / headless).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["NINGBING_ROOT"] = ROOT

import pytest  # noqa: E402
from PyQt6 import sip  # noqa: E402
from PyQt6.QtCore import QEventLoop, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402


def _start_watchdog(timeout: float) -> threading.Thread:
    """Abort hard if the main thread is stuck (deadlock / native hang)."""

    def _run() -> None:
        import faulthandler
        import time

        time.sleep(timeout)
        faulthandler.dump_traceback()
        os._exit(2)  # 2 == "main thread dead/stuck"

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def test_page_switch_via_gamepad_path_survives():
    try:
        import faulthandler

        faulthandler.enable()
    except Exception:
        pass

    app = QApplication.instance() or QApplication(sys.argv)
    _start_watchdog(120)

    from ningbingjue.app import Application

    config = os.path.join(ROOT, "layout.json")
    application = Application(ROOT, headless=False, config_path=config).boot()

    window = application._window
    ctrl = window._controller

    pages = application.nav._pages()
    assert pages, "expected at least one page to switch between"

    # Same call the RB/LB shoulder buttons make; repeat enough to cross both
    # forward wrap-around and back, so every page gets rebuilt at least once.
    switches = min(15, max(6, len(pages) * 3))
    for i in range(switches):
        ctrl._next_page()
        # Flush the deferred _do_page_rebuild (QTimer.singleShot(0, ...)).
        loop = QEventLoop()
        for _ in range(5):
            app.processEvents()
            QTimer.singleShot(20, loop.quit)
            loop.exec()
        # The window + controller C++ objects must still be alive.  A native
        # crash would have already killed the process; this guards the softer
        # case where an object was deleted but the process limped on.
        assert not sip.isdeleted(window), f"window died after switch #{i}"
        assert not sip.isdeleted(ctrl), f"controller died after switch #{i}"

    # One final repaint pass must not raise either.
    app.processEvents()
    assert not sip.isdeleted(window), "window died on final repaint"
    print(f"[survival] did {switches} page switches via gamepad path; "
          f"window + controller alive")
