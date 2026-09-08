"""Process-level DPI awareness setup (Windows-only).

Why this lives in src/ningbingjue/core/:
    The bundled ``python.exe`` shipped with 凝冰结魄 does **not** embed a
    ``PerMonitorV2`` manifest, and the frozen ``NingBingJue.exe`` on the
    handheld runs ``run.py`` baked into the bundle — neither can be relied on
    to declare DPI awareness at process start.  On a 1.5x / fractional DPR
    panel this means the whole window gets bitmap-stretched by Windows and
    every label looks soft.

    ``run.py`` historically tried to set DPI awareness itself, but:

      * ``run.py`` is **not** inside ``src/``, so it is **not** shadowed by
        the ``_ext/src/`` override path the dev machine relies on for
        remote-sync testing.  Fixing ``run.py`` therefore lands only on a
        rebuilt exe — useless for everyday iteration.
      * the call had a real bug (missing ``argtypes`` → 32-bit truncation
        on 64-bit Windows → silently returns FALSE without raising).

This module fixes the bug AND lives inside ``src/`` so the override path
picks it up.  It is invoked from ``ningbingjue/__init__.py`` so every
``from ningbingjue...`` (incl. ``from ningbingjue.app import Application``
in ``run.py`` and any other future entry point) gets the call before any
QApplication is ever instantiated.

It is idempotent — calling it twice is safe (the OS rejects re-setting DPI
awareness with ``E_ACCESSDENIED``, which we swallow).

CRITICAL (post-mortem 2026-08-27): the previous version *swallowed every
exception silently*.  When the runtime call failed on the handheld we had
no signal and the process stayed UNAWARE → the whole HUD stayed blurry and
the window overflowed the screen.  This version tries FOUR API variants in
order and **logs every attempt + the resulting awareness level** to
``%TEMP%/ningbingjue_dpi.log`` (and stderr) so a failure is finally visible.
"""
from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import time

_AWARE_NAMES = {
    0: "UNAWARE",
    1: "SYSTEM_AWARE",
    2: "PER_MONITOR",
    3: "PER_MONITOR_V2",
}

_DPI_LOG = os.path.join(tempfile.gettempdir(), "ningbingjue_dpi.log")


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        with open(_DPI_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001 - logging must never break startup
        pass
    try:
        print("[ningbingjue._dpi] " + msg, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def _get_awareness() -> int:
    try:
        shcore = ctypes.windll.shcore
        shcore.GetProcessDpiAwareness.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        shcore.GetProcessDpiAwareness.restype = ctypes.c_long
        out = ctypes.c_int(0)
        shcore.GetProcessDpiAwareness(
            ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(out))
        return int(out.value)
    except Exception:  # noqa: BLE001
        return -1


def _get_system_dpi() -> int:
    """Real system DPI (e.g. 144 for 150%, 96 for 100%), read via the public
    Win32 API so it is *independent* of this process's DPI awareness.

    Under DPI-UNAWARE the OS *lies* to the process (``devicePixelRatio()``
    reports 1.0 / 96 dpi even on a 150% panel), so the only way to learn the
    true scale is ``GetDpiForSystem()``.  Returns -1 if unavailable.
    """
    try:
        user32 = ctypes.windll.user32
        user32.GetDpiForSystem.argtypes = []
        user32.GetDpiForSystem.restype = ctypes.c_uint
        return int(user32.GetDpiForSystem())
    except Exception:  # noqa: BLE001
        return -1


def _try_user32_v2() -> bool:
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_int
        ok = user32.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(-4 & 0xFFFFFFFFFFFFFFFF))
        _log(f"user32.SetProcessDpiAwarenessContext(V2) -> {ok}")
        return bool(ok)
    except Exception as e:  # noqa: BLE001
        _log(f"user32.SetProcessDpiAwarenessContext(V2) FAILED: {type(e).__name__}: {e}")
        return False


def _try_shcore_v2() -> bool:
    try:
        shcore = ctypes.windll.shcore
        shcore.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        shcore.SetProcessDpiAwarenessContext.restype = ctypes.c_long
        ok = shcore.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(-4 & 0xFFFFFFFFFFFFFFFF))
        _log(f"shcore.SetProcessDpiAwarenessContext(V2) -> {ok}")
        return bool(ok)
    except Exception as e:  # noqa: BLE001
        _log(f"shcore.SetProcessDpiAwarenessContext(V2) FAILED: {type(e).__name__}: {e}")
        return False


def _try_shcore_v1() -> bool:
    try:
        shcore = ctypes.windll.shcore
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        shcore.SetProcessDpiAwareness.restype = ctypes.c_long
        shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        _log("shcore.SetProcessDpiAwareness(V1=2) called")
        return True
    except Exception as e:  # noqa: BLE001
        _log(f"shcore.SetProcessDpiAwareness(V1) FAILED: {type(e).__name__}: {e}")
        return False


def _try_user32_legacy() -> bool:
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDPIAware.argtypes = [ctypes.c_int]
        user32.SetProcessDPIAware.restype = ctypes.c_int
        ok = user32.SetProcessDPIAware(2)  # 2 = per-monitor on Vista+
        _log(f"user32.SetProcessDPIAware(2) -> {ok}")
        return bool(ok)
    except Exception as e:  # noqa: BLE001
        _log(f"user32.SetProcessDPIAware(2) FAILED: {type(e).__name__}: {e}")
        return False


def setup_process_dpi_awareness() -> bool:
    """Declare Per-Monitor DPI awareness for this process.

    Tries four Win32 API variants in order (V2 via user32, V2 via shcore,
    V1 via shcore, legacy via user32) and **logs every attempt and the
    resulting awareness level** so a silent failure can finally be seen.

    Returns ``True`` if a per-monitor awareness level (V1 or V2) ended up
    active, ``False`` if the process is still UNAWARE.
    Never raises.
    """
    if sys.platform != "win32":
        _log("non-win32 platform -> skip (no DPI awareness needed)")
        return False

    before = _get_awareness()
    _log(f"before: awareness={before} ({_AWARE_NAMES.get(before, '?')})")

    set_by_us = False
    if _try_user32_v2():
        set_by_us = True
    elif _try_shcore_v2():
        set_by_us = True
    elif _try_shcore_v1():
        set_by_us = True
    elif _try_user32_legacy():
        set_by_us = True

    after = _get_awareness()
    _log(f"after: awareness={after} ({_AWARE_NAMES.get(after, '?')}) "
         f"set_by_us={set_by_us}")

    # Real display scale, read independent of this process's awareness.  Under
    # DPI-UNAWARE the OS reports 96/1.0 to the app, so this is the ONLY way to
    # know whether we *should* be rendering at 1.5x but aren't.
    sys_dpi = _get_system_dpi()
    if sys_dpi > 0:
        _log(f"system DPI={sys_dpi} (display scale={sys_dpi / 96.0:.2f}x)")
    else:
        _log("system DPI=unknown (GetDpiForSystem unavailable)")

    if after in (2, 3):
        _log("RESULT: DPI-aware (PER_MONITOR) -> HUD renders at native device "
             "resolution; small text (incl. CJK parentheses) is crisp on "
             "scaled displays.")
    else:
        _log("!!! RESULT: DPI-UNAWARE -> Windows bitmap-stretches the WHOLE "
             "window by the display scale, so ALL text looks grainy / dotty "
             "(parentheses become dotted curves). This is set by the frozen "
             "exe's embedded manifest and CANNOT be raised by a runtime API "
             "call. FIX: (a) rebuild the exe so nbj_manifest.xml (PerMonitorV2) "
             "is embedded, or (b) run 'python run.py' directly (the runtime "
             "call sets awareness before QApplication is created).")
    # Final state is what matters: per-monitor (2) or V2 (3) is the goal.
    return after in (2, 3)
