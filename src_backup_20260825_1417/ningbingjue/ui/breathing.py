"""Breathing scanline animation — ties the pure math in ``core.timers`` to widgets.

A single effect: a sine "scanline breathing" (~2.36 s period by default) that
drives a glow alpha used for the window's neon border + top scanline.  The
original's second "slash glow" effect was dead code in the HUD (never started)
and has been removed.

``BreathingController`` runs a ``QTimer`` on the GUI thread and invokes a
callback with the current alpha so the UI layer can repaint.  Because the timer
fires on the GUI thread, the callback is allowed to touch widgets (e.g. call
``widget.update()``) without crossing into a worker thread.  A threaded
``RepeatingTimer`` is kept only as a headless import shim for environments that
lack PyQt6 (logic tests never start the timer, so this never matters in practice).

Pause/resume semantics (used when the window loses focus / is hidden / locked)
live on the controller via :meth:`pause` / :meth:`resume`.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

try:  # PyQt6 is present in the GUI runtime; absent only in headless test shims.
    from PyQt6.QtCore import QTimer
    _HAS_QT = True
except Exception:  # pragma: no cover - headless import shim
    QTimer = None
    _HAS_QT = False

from ..core.timers import (
    RepeatingTimer,
    scanline_alpha,
    scanline_advance,
    scanline_step_for_period,
)

# Fixed tick interval for the scanline timer (ms).  The breath *period* is
# derived from this + the phase step, so the tick rate stays constant while
# the period is freely adjustable.
_SCANLINE_TICK_MS = 40


class BreathingController:
    def __init__(self, period_s: float = 2.36, max_brightness: float = 1.0) -> None:
        self._scan_phase = 0.0
        self._scan_timer: Optional[Any] = None
        self.on_scanline: Optional[Callable[[float], None]] = None
        self._period_s = float(period_s)
        self._max = max(0.0, min(1.0, float(max_brightness)))
        self._step = scanline_step_for_period(self._period_s, _SCANLINE_TICK_MS)

    # -- runtime tuning (no restart needed: tick rate is constant) -------
    def set_period(self, period_s: float) -> None:
        self._period_s = max(0.1, float(period_s))
        self._step = scanline_step_for_period(self._period_s, _SCANLINE_TICK_MS)

    def set_max_brightness(self, max_brightness: float) -> None:
        self._max = max(0.0, min(1.0, float(max_brightness)))

    # -- scanline (30ms, sine) --------------------------------------------
    def _scan_tick(self) -> None:
        self._scan_phase = scanline_advance(self._scan_phase, self._step)
        if self.on_scanline:
            self.on_scanline(scanline_alpha(self._scan_phase, self._max))

    def start_scanline(self) -> None:
        self._scan_timer = self._make_timer(_SCANLINE_TICK_MS, self._scan_tick)
        self._scan_timer.start()

    def _make_timer(self, interval_ms: int, slot: Callable[[], None]) -> Any:
        """Build the tick timer on the *GUI thread*.

        ``QTimer`` fires its ``timeout`` on the thread it was created on, so a
        ``QTimer`` built on the GUI thread lets the callback safely touch
        widgets (e.g. call ``widget.update()``).  The threaded ``RepeatingTimer``
        is only a fallback for headless import shims that lack PyQt6.
        """
        if _HAS_QT:
            t: Any = QTimer()
            t.setInterval(interval_ms)
            t.timeout.connect(slot)
            return t
        return RepeatingTimer(interval_ms, slot)

    def pause(self) -> None:
        if self._scan_timer is not None:
            self._scan_timer.stop()

    def resume(self) -> None:
        if self._scan_timer is not None:
            self._scan_timer.start()

    def stop(self) -> None:
        if self._scan_timer is not None:
            self._scan_timer.stop()
        self._scan_timer = None

    # -- pure helpers for unit tests --------------------------------------
    @staticmethod
    def scanline_sample(phase: float, max_brightness: float = 1.0) -> float:
        return scanline_alpha(phase, max_brightness)
