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
from typing import Callable


# -- pure breathing math ----------------------------------------------------
# The trough is fixed so the glow always returns to a dim baseline between
# peaks (otherwise a low max brightness would kill the "breathing" feel).
_BREATH_FLOOR = 0.2


def scanline_alpha(phase: float, max_brightness: float = 1.0) -> float:
    """Scanline breathing alpha in [floor, max_brightness] (sine based).

    With the default ``max_brightness=1.0`` this reproduces the original
    ``0.6 + 0.4*sin(phase)`` curve (trough 0.2, peak 1.0).  Lowering
    ``max_brightness`` scales the peak down while keeping the floor, so the
    glow still pulses between dim and bright.
    """
    m = max(0.0, min(1.0, max_brightness))
    a = _BREATH_FLOOR + (m - _BREATH_FLOOR) * (0.5 + 0.5 * math.sin(phase))
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
        except Exception:
            pass
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
