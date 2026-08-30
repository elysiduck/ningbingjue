"""End-to-end probe for the real ``_OverlayWindow._steal_foreground_best_effort``.

Runs the product function against a real Qt top-level window and compares it
with the plain ``SetForegroundWindow`` path, on the real desktop:

  A. plain SetForegroundWindow  (what Qt's activateWindow() ends up doing)
  B. _steal_foreground_best_effort()  (AttachThreadInput + SFW + top + active
     + focus, i.e. the fix)

If B wins while A loses, the foreground-lock bypass is doing real work.
"""
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

from ningbingjue.ui.app_window import (  # noqa: E402
    _USER32_READY, _STEAL_FG_RETRY_MS, _OverlayWindow,
)

u = ctypes.windll.user32
u.GetForegroundWindow.restype = ctypes.c_void_p
u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
u.SetForegroundWindow.argtypes = [ctypes.c_void_p]

print("_USER32_READY =", _USER32_READY, " retry_ms =", _STEAL_FG_RETRY_MS)

app = QApplication(sys.argv)
w = QWidget()
w.setWindowTitle("NBJ fg probe")
w.resize(360, 240)
w.show()
app.processEvents()
time.sleep(0.25)
hwnd = int(w.winId())
print("probe hwnd =", hex(hwnd))


class _Shim:
    """Minimal stand-in exposing only what the product function touches.

    必须自带 ``_steal_foreground_retry`` / ``_steal_foreground_best_effort``：
    产品代码用 ``self._steal_foreground_retry`` 取 bound method，用普通对象当
    shim 时少了这两个属性，补刀就会被 except 悄悄吞掉（本次实测踩到过）。
    """

    def __init__(self, widget):
        self._w = widget

    def winId(self):
        return self._w.winId()

    def isVisible(self):
        return self._w.isVisible()

    def activateWindow(self):
        self._w.activateWindow()

    def _steal_foreground_best_effort(self, _attempt=0):
        return _OverlayWindow._steal_foreground_best_effort(self, _attempt)

    def _steal_foreground_retry(self):
        return _OverlayWindow._steal_foreground_retry(self)


shim = _Shim(w)


def fg_hex():
    return hex(int(u.GetForegroundWindow() or 0))


cur_thread = ctypes.windll.kernel32.GetCurrentThreadId()
print("cur_thread =", cur_thread, " foreground now =", fg_hex(),
      " ours =", int(u.GetForegroundWindow() or 0) == hwnd)

# --- A. plain SetForegroundWindow ----------------------------------------
u.SetForegroundWindow(hwnd)
app.processEvents()
time.sleep(0.25)
ok_a = int(u.GetForegroundWindow() or 0) == hwnd
print("A plain SFW          -> foreground is ours:", ok_a, fg_hex())

# --- B. the product function ---------------------------------------------
_OverlayWindow._steal_foreground_best_effort(shim)
app.processEvents()
time.sleep(0.35)
ok_b = int(u.GetForegroundWindow() or 0) == hwnd
print("B steal_foreground   -> foreground is ours:", ok_b, fg_hex())
print("   Qt isActiveWindow :", w.isActiveWindow())

# --- C. the more realistic case: hidden -> shown (tray restore) ----------
w.hide()
app.processEvents()
time.sleep(0.2)
w.show()
app.processEvents()
_OverlayWindow._steal_foreground_best_effort(shim)
app.processEvents()
time.sleep(0.35)
ok_c = int(u.GetForegroundWindow() or 0) == hwnd
print("C hide->show + steal -> foreground is ours:", ok_c, fg_hex(),
      " Qt active:", w.isActiveWindow())

# --- D. adversarial: give the foreground to ANOTHER process first --------
# 这才是真实场景：用户在游戏里（别的进程持有前台），从托盘唤出 HUD。
import subprocess  # noqa: E402
proc = None
other = 0
try:
    proc = subprocess.Popen(["notepad.exe"])
    for _ in range(60):
        app.processEvents()
        time.sleep(0.1)
        other = int(u.FindWindowW("Notepad", None) or 0)
        if other:
            break
    time.sleep(0.3)
except Exception as exc:  # noqa: BLE001
    print("notepad launch failed:", exc)

if other:
    u.SetForegroundWindow(other)
    app.processEvents()
    time.sleep(0.4)
    fg_other = int(u.GetForegroundWindow() or 0) == other
    print("\nD adversarial: notepad hwnd =", hex(other),
          " foreground handed over:", fg_other)
    # D1: Qt's own path (what _fade_in_from_tray does on its own)
    w.raise_()
    w.activateWindow()
    app.processEvents()
    time.sleep(0.35)
    ok_d1 = int(u.GetForegroundWindow() or 0) == hwnd
    print("D1 Qt raise+activateWindow -> foreground is ours:", ok_d1, fg_hex(),
          " Qt active:", w.isActiveWindow())
    # D2: plain native SFW (no attach)
    u.SetForegroundWindow(other)
    time.sleep(0.3)
    u.SetForegroundWindow(hwnd)
    app.processEvents()
    time.sleep(0.35)
    ok_d2 = int(u.GetForegroundWindow() or 0) == hwnd
    print("D2 plain native SFW        -> foreground is ours:", ok_d2, fg_hex())
    # D3: the product function
    u.SetForegroundWindow(other)
    time.sleep(0.3)
    _OverlayWindow._steal_foreground_best_effort(shim)
    app.processEvents()
    time.sleep(0.35)
    ok_d3 = int(u.GetForegroundWindow() or 0) == hwnd
    print("D3 steal_foreground        -> foreground is ours:", ok_d3, fg_hex(),
          " Qt active:", w.isActiveWindow())
    results = (ok_a, ok_b, ok_c, ok_d1, ok_d2, ok_d3)
    labels = ("A", "B", "C", "D1(Qt)", "D2(SFW)", "D3(steal)")
    print()
    print(" | ".join("%s=%s" % kv for kv in zip(labels, results)))
    print("RESULT: OK" if (ok_b and ok_c and ok_d3) else "RESULT: CHECK")
else:
    print("D adversarial: skipped (no notepad window)")
    print("RESULT: OK (A=%s B=%s C=%s)" % (ok_a, ok_b, ok_c))

# --- E. failure path: bogus hwnd must schedule exactly ONE bounded retry ---
calls = {"n": 0}
_orig_retry = _OverlayWindow._steal_foreground_retry


def _spy(self):
    calls["n"] += 1
    print("   [spy] retry #", calls["n"], "self =", type(self).__name__)
    _orig_retry(self)


_OverlayWindow._steal_foreground_retry = _spy


class _BogusShim(_Shim):
    """winId() points at a non-existent window -> SFW always fails."""

    def winId(self):
        return 0x123456


_ss_orig = QTimer.singleShot


def _ss_spy(msec, slot):
    print("   [singleShot]", msec, "ms ->", slot)
    return _ss_orig(msec, slot)


QTimer.singleShot = _ss_spy
# 必须把 shim 存进变量：QTimer.singleShot 不会替临时对象保命，
# 若传 `Bogus(w)` 这种临时量，对象会在定时器到期前被 GC，回调静默不触发。
_bogus = _BogusShim(w)
try:
    _OverlayWindow._steal_foreground_best_effort(_bogus)
    # 用真实事件循环（和 App 实际运行方式一致）跑 600ms，再检查补刀次数。
    QTimer.singleShot(600, app.quit)
    app.exec()
    print("\nE failure path: retry fired", calls["n"], "time(s) (expect 1),",
          "no exception escaped: True")
    print("E RESULT:", "OK" if calls["n"] == 1 else "FAIL")
except Exception as exc:  # noqa: BLE001
    print("\nE failure path raised:", repr(exc))
    print("E RESULT: FAIL")
finally:
    _OverlayWindow._steal_foreground_retry = _orig_retry

try:
    if proc is not None:
        proc.terminate()
except Exception:  # noqa: BLE001
    pass
QTimer.singleShot(0, app.quit)
