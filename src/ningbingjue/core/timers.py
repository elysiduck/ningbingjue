"""Background timers + breathing-light math.

The original has three independent animation mechanisms:

* **Scanline breathing** — a 30ms timer; phase advances by ``0.08`` each tick and
  ``alpha = 0.6 + 0.4 * sin(phase)`` (period ≈ 2.36 s).
* **Slash glow** — a 16ms timer; ``alpha`` eases toward ``{0, 1}`` (not a sine
  breath).  *Removed* — the HUD never started it, so it was dead code; the pure
  ``slash_step`` helper has been deleted.
* **DrawInnerGlow** — multi-layer neon drawn by the C# control (UI only).

``scanline_alpha`` is pure and unit-tested.  ``RepeatingTimer`` is the
pause/resume-capable background timer used by the GUI layer.
"""

from __future__ import annotations

import math
import threading
import traceback
from typing import Callable


# -- pure breathing math ----------------------------------------------------
# The trough is fixed so the glow always returns to a dim baseline between
# peaks (otherwise a low max brightness would kill the "breathing" feel).
# 2026-08-29 (甲方案): 0.2 -> 0.1，加大呼吸落差（0.1↔1.0 比 0.2↔1.0 更明显），
# 让边框呼吸在游戏前台/HUD 非激活时也清晰可感。仅作用于边框 overlay 呼吸
# （scanline_alpha 只被 BreathingController 调用，而它只驱动 _GlowBorderOverlay）。
_BREATH_FLOOR = 0.1


def scanline_alpha(phase: float, max_brightness: float = 1.0,
                   floor: float | None = None) -> float:
    """Scanline breathing alpha in [floor, max_brightness] (sine based).

    ``floor`` 省略时沿用模块常量 ``_BREATH_FLOOR``，因此所有既有调用点的行为
    完全不变（向后兼容）。显式传值即可让呼吸谷值可配置 —— 2026-08-30 呼吸灯
    面板新增「呼吸谷值」滑块后由 ``BreathingController`` 传入。

    谷值被夹在 [0, 峰值] 内：若 floor 高于 max_brightness 会令曲线反向，
    直接按峰值截断，灯就变成恒定满亮（等价于关掉脉动），不会出现异常亮度。
    """
    m = max(0.0, min(1.0, max_brightness))
    fl = _BREATH_FLOOR if floor is None else float(floor)
    fl = max(0.0, min(m, fl))
    a = fl + (m - fl) * (0.5 + 0.5 * math.sin(phase))
    return max(0.0, min(1.0, a))


def scanline_advance(phase: float, step: float = 0.08) -> float:
    """Advance the scanline phase by ``step`` radians (default 0.08/tick)."""
    return phase + step


def scanline_step_for_period(period_s: float, tick_ms: float = 30.0) -> float:
    """Phase increment per ``tick_ms`` tick that yields a full breath in
    ``period_s`` seconds (one period = 2π radians)."""
    p = max(0.1, period_s)
    return (2.0 * math.pi * (tick_ms / 1000.0)) / p


# -- pause/resume-capable background timer ---------------------------------
class RepeatingTimer:
    """A restart-able timer that supports Pause/Resume (mirrors the original
    ``Pause-BackgroundTimers`` / ``Resume-BackgroundTimers`` semantics)."""

    def __init__(self, interval_ms: int, callback: Callable[[], None]) -> None:
        self.interval = max(1, interval_ms) / 1000.0
        self.callback = callback
        self._timer: threading.Timer | None = None
        self._running = False
        self._paused = False
        self._lock = threading.Lock()

    def _tick(self) -> None:
        with self._lock:
            if not self._running or self._paused:
                return
        try:
            self.callback()
        except Exception:  # noqa: BLE001
            # 回调异常绝不应静默吞掉——否则定时任务"假死"且无法排查。
            traceback.print_exc()
        with self._lock:
            if self._running and not self._paused:
                self._timer = threading.Timer(self.interval, self._tick)
                self._timer.daemon = True
                self._timer.start()

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._paused = False
            self._timer = threading.Timer(self.interval, self._tick)
            self._timer.daemon = True
            self._timer.start()

    def pause(self) -> None:
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            if self._running and self._paused:
                self._paused = False
                self._timer = threading.Timer(self.interval, self._tick)
                self._timer.daemon = True
                self._timer.start()

    def stop(self) -> None:
        with self._lock:
            self._running = False
            self._paused = False
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
