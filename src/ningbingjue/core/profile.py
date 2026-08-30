"""Opt-in performance profiler for 凝冰结魄 (NingBingJue).

Enabled only when the env var ``NBJ_PROFILE`` is set to a non-empty value
(e.g. ``NBJ_PROFILE=1 launch.bat``).  When disabled every public function is a
no-op and nothing is imported from PyQt, so leaving this module in the tree
costs nothing in normal use.

Goal: capture, on the *real* handheld display, the ground-truth numbers that
explain "HUD sluggish / 卡顿" — which we cannot reproduce headless:

* ``main_paint`` / ``glow_paint`` — number of ``paintEvent`` calls per second on
  the main window (the breathing glow is now its background layer).  A high rate (e.g.
  100+/s) means a *repaint storm* (a known ``setMask``/DWM issue), not a task.
* ``ui_gap_max`` — the largest gap between two consecutive 1 s heartbeat ticks.
  If this is far above 1000 ms the **UI thread was blocked** for that long
  (some synchronous task is starving event handling → buttons/nav feel slow).
* ``sync_all`` / ``refresh`` / ``gp_poll`` / ``subprocess`` — per-second cost of
  the periodic UI-thread work, to confirm it is (or is not) the bottleneck.

Every second a single line is appended to ``logs/nbj_profile.log``.
"""

from __future__ import annotations

import os
import time

ENABLED = bool(os.environ.get("NBJ_PROFILE"))

# Cumulative counters.  Read/reset under _LOCK by the dumper.
_STATS = {
    "main_paint": 0,
    "main_paint_ms": 0.0,
    "glow_paint": 0,
    "glow_paint_ms": 0.0,
    "sync_all": 0,
    "sync_all_ms": 0.0,
    "refresh": 0,
    "refresh_ms": 0.0,
    "gp_poll": 0,
    "subprocess": 0,
}

# Per-second snapshot of the counters (for delta computation).
_PREV = dict(_STATS)
_START = time.monotonic()
_LAST_HEARTBEAT = [_START]
_UI_GAP_MAX = [0.0]

_LOG_PATH: str | None = None


def _log_path() -> str:
    global _LOG_PATH
    if _LOG_PATH is None:
        try:
            root = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..", ".."))
            d = os.path.join(root, "logs")
            os.makedirs(d, exist_ok=True)
            _LOG_PATH = os.path.join(d, "nbj_profile.log")
        except Exception:
            _LOG_PATH = "nbj_profile.log"
    return _LOG_PATH


# -- counters (cheap; called from the UI thread) -------------------------
def tick_main_paint(ms: float) -> None:
    if not ENABLED:
        return
    _STATS["main_paint"] += 1
    _STATS["main_paint_ms"] += ms


def tick_glow_paint(ms: float) -> None:
    if not ENABLED:
        return
    _STATS["glow_paint"] += 1
    _STATS["glow_paint_ms"] += ms


def tick_sync_all(ms: float) -> None:
    if not ENABLED:
        return
    _STATS["sync_all"] += 1
    _STATS["sync_all_ms"] += ms


def tick_refresh(ms: float) -> None:
    if not ENABLED:
        return
    _STATS["refresh"] += 1
    _STATS["refresh_ms"] += ms


def tick_gp_poll() -> None:
    if not ENABLED:
        return
    _STATS["gp_poll"] += 1


def tick_subprocess() -> None:
    if not ENABLED:
        return
    _STATS["subprocess"] += 1


# -- heartbeat + dumper (install on the UI thread) -----------------------
def install_heartbeat() -> None:
    """Install a 1 s QTimer on the UI thread that (a) measures the gap since
    the previous tick (UI-thread starvation probe) and (b) dumps a snapshot.

    Must be called from the UI thread (it creates a QTimer).  No-op when the
    profiler is disabled.
    """
    if not ENABLED:
        return
    try:
        from PyQt6.QtCore import QTimer  # type: ignore
    except Exception:
        return

    def _beat() -> None:
        now = time.monotonic()
        gap = (now - _LAST_HEARTBEAT[0]) * 1000.0
        _LAST_HEARTBEAT[0] = now
        if gap > _UI_GAP_MAX[0]:
            _UI_GAP_MAX[0] = gap
        _dump_snapshot(now)

    t = QTimer()
    t.setInterval(1000)
    t.timeout.connect(_beat)
    t.start()


def _dump_snapshot(now: float) -> None:
    try:
        elapsed = now - _START
        lines = []
        for k in ("main_paint", "glow_paint", "sync_all", "refresh", "gp_poll"):
            cur = _STATS[k]
            delta = cur - _PREV.get(k, 0)
            _PREV[k] = cur
            lines.append(f"  {k:>12}: {delta}/s")
        for k in ("main_paint_ms", "sync_all_ms", "refresh_ms"):
            cur = _STATS[k]
            delta = cur - _PREV.get(k, 0.0)
            _PREV[k] = cur
            lines.append(f"  {k:>12}: {delta:.1f}ms/s")
        ui_gap = _UI_GAP_MAX[0]
        # Reset the max-gap watermark each dump so we see the worst *this* second.
        _UI_GAP_MAX[0] = 0.0
        lines.append(f"  {'ui_gap_max':>12}: {ui_gap:.0f}ms  "
                     f"(>1300ms => UI thread was blocked)")
        ts = time.strftime("%H:%M:%S", time.localtime(now))
        header = f"[{ts}] t+{elapsed:6.1f}s  NBJ_PROFILE"
        out = header + "\n" + "\n".join(lines) + "\n"
        with open(_log_path(), "a", encoding="utf-8") as fh:
            fh.write(out)
    except Exception:
        pass
