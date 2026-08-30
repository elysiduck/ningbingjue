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

Phase model (2026-08-30 修订 · 乙方案废弃)
------------------------------------------------
The breath phase is NO LONGER derived from wall-clock time.  It is an internal
accumulator (``_elapsed``) that advances by the real time between consecutive
*ticks* — and only while the breath is actually running and not frozen.  Two
consequences:

* During normal running the period stays accurate (we still add real ``dt``),
  so the 2026-08-29 fix against "越卡越慢" drift is preserved.
* During an operation (scroll / arrow-key / gamepad focus switch) the app calls
  ``note_activity()``, which opens a short "freeze window".  While that window is
  open the tick still fires but the accumulator is held — the brightness pins to
  wherever it was and resumes seamlessly afterwards, with NO brightness jump.
  This is exactly the "卡住就行、结束无缝续接" behaviour the user asked for.

As a safety net beyond the explicit freeze window, a starved tick (main thread
busy long enough that the timer was delayed) is clamped to ``_MAX_CATCHUP`` so it
can never advance more than ~2 frames at once — i.e. even an un-forecast stall
can only produce an imperceptible step, never a visible jump.

Pause/resume semantics (used when the window loses focus / is hidden / locked)
live on the controller via :meth:`pause` / :meth:`resume` and fully stop the
accumulator; those are separate from the operation freeze and are untouched.
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Optional

try:  # PyQt6 is present in the GUI runtime; absent only in headless test shims.
    from PyQt6.QtCore import Qt, QTimer
    _HAS_QT = True
except Exception:  # pragma: no cover - headless import shim
    QTimer = None
    _HAS_QT = False

from ..core.timers import (
    RepeatingTimer,
    scanline_alpha,
    scanline_step_for_period,
)

# Fixed tick interval for the scanline timer (ms).  The breath *period* is
# derived from this + the phase step, so the tick rate stays constant while
# the period is freely adjustable.
#
# 2026-08-29 perf: lowered 40 -> 50 (≈20fps).  Plan C: the breathing glow is
# painted in the main window's background layer (a cheap border-band repaint of
# the main window, see _breath_edge_region / _paint_breath_frame), and the main
# window is opaque (no WA_TranslucentBackground), so the DWM does not
# alpha-composite the whole surface each tick.  A slower tick still cuts the
# constant repaint load ~20% with no visible change to a slow "breath".
_SCANLINE_TICK_MS = 50

# Catch-up clamp: a tick delayed by more than this much wall time (one frame)
# only advances the breath accumulator by this amount.  Prevents a starved tick
# (main thread fully blocked during an operation) from producing a visible
# brightness jump after an un-forecast stall.  The deliberate operation-freeze
# is handled separately by note_activity(); this is only the backstop.
_MAX_CATCHUP = _SCANLINE_TICK_MS / 1000.0

# Default length of the "operation freeze" window opened by note_activity().
_ACTIVITY_FREEZE_S = 0.18


class BreathingController:
    def __init__(self, period_s: float = 2.36, max_brightness: float = 1.0,
                 enabled: bool = True, floor: float | None = None) -> None:
        self._scan_phase = 0.0
        self._scan_timer: Optional[Any] = None
        self.on_scanline: Optional[Callable[[float], None]] = None
        self._period_s = float(period_s)
        self._max = max(0.0, min(1.0, float(max_brightness)))
        # 呼吸谷值（最暗值）。None = 沿用 core.timers._BREATH_FLOOR，旧行为不变。
        # 2026-08-30 起可由呼吸灯面板「呼吸谷值」滑块实时调整。
        self._floor = floor
        self._enabled = bool(enabled)
        self._step = scanline_step_for_period(self._period_s, _SCANLINE_TICK_MS)

        # -- phase accumulator model (replaces the old wall-clock phase) ------
        # phase = (_phase_base + 2π · _elapsed / period) mod 2π
        #   _phase_base : phase offset, re-pinned by set_phase / rebase.
        #   _elapsed    : accumulated breathing seconds; advances ONLY on ticks
        #                 while running and not frozen/paused.  This is what
        #                 makes the breath freeze during operations instead of
        #                 jumping to "where the wall clock says it should be".
        #   _last_tick  : monotonic time of the last processed tick.
        #   _freeze_until: while monotonic() < this, the tick holds _elapsed.
        self._phase_base = 0.0
        self._elapsed = 0.0
        self._last_tick = time.monotonic()
        self._freeze_until = 0.0
        # Set when a frozen tick is the most recent one; the first tick after the
        # freeze window "releases" the hold (emits the frozen value, no advance)
        # so the glow resumes from EXACTLY where it froze — never jumps.
        self._just_unfroze = False

    # -- runtime tuning (no restart needed: tick rate is constant) -------
    def set_period(self, period_s: float) -> None:
        # 先把「当前相位」固化为新基准，再改周期，避免相位突然跳变。
        self._rebase()
        self._period_s = max(0.1, float(period_s))
        self._step = scanline_step_for_period(self._period_s, _SCANLINE_TICK_MS)

    def _current_phase(self) -> float:
        """Phase (radians, mod 2π) derived from the internal accumulator.

        Because ``_elapsed`` only advances on running, non-frozen ticks, this
        returns the *frozen* phase during an operation and resumes from exactly
        that point afterwards — no wall-clock jump.
        """
        p = max(0.1, self._period_s)
        return (self._phase_base + 2.0 * math.pi * self._elapsed / p) % (2.0 * math.pi)

    def _rebase(self) -> None:
        """Bake the current phase into ``_phase_base`` and reset the accumulator.

        After this, ``_current_phase()`` returns the same value it does now, but
        the breath continues from a clean zero accumulator.  Called before
        changing the period, pinning a phase, and on timer (re)start so a stall
        that happened before the (re)start is not counted as breath time.
        """
        self._phase_base = self._current_phase()
        self._elapsed = 0.0
        self._last_tick = time.monotonic()

    def note_activity(self, duration_s: float = _ACTIVITY_FREEZE_S) -> None:
        """Open (or extend) a short freeze window during which the breath phase
        is held.

        Call this from any user-input path that can stall the GUI thread — scroll,
        arrow-key navigation, gamepad focus switch.  While the window is open the
        tick keeps firing but does not advance the accumulator, so the glow pins
        to its current brightness and resumes seamlessly when the operation ends.
        Idle gamepad polls must NOT call this (only real input edges), or the
        breath would be frozen permanently while a pad is connected.
        """
        self._freeze_until = max(self._freeze_until,
                                  time.monotonic() + float(duration_s))

    def set_max_brightness(self, max_brightness: float) -> None:
        self._max = max(0.0, min(1.0, float(max_brightness)))
        # When the pulse is disabled the glow is held at a steady peak; re-emit
        # so a live brightness change still reflects immediately.
        if not self._enabled and self.on_scanline:
            self.on_scanline(self._max)

    def set_floor(self, floor: float | None) -> None:
        """Set the breathing trough (minimum alpha).  ``None`` restores the
        module default ``core.timers._BREATH_FLOOR``.

        Re-emits immediately when the pulse is disabled so a live edit still
        shows up, mirroring :meth:`set_max_brightness`.
        """
        self._floor = None if floor is None else max(0.0, min(1.0, float(floor)))
        if not self._enabled and self.on_scanline:
            self.on_scanline(self._max)

    def set_phase(self, phase_rad: float) -> None:
        """Pin the phase to ``phase_rad`` (radians) and restart timing from now.

        Used by the "flash to peak then breathe" hand-off: the HUD fades the
        glow to full brightness itself, then pins the phase to ``pi/2`` (the
        sine peak) before resuming, so the pulse continues *downwards from the
        brightest point* instead of jumping to a random phase.
        """
        self._phase_base = float(phase_rad) % (2.0 * math.pi)
        self._elapsed = 0.0
        self._last_tick = time.monotonic()
        self._scan_phase = self._phase_base

    # -- enabled master switch ------------------------------------------
    def set_enabled(self, enabled: bool) -> None:
        """Toggle the breathing pulse.  When disabled the glow is held at a
        steady (peak) brightness instead of pulsing; when re-enabled the scanline
        timer restarts from the current phase."""
        enabled = bool(enabled)
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if enabled:
            self.start_scanline()
        else:
            if self._scan_timer is not None:
                self._scan_timer.stop()
            self._scan_timer = None
            if self.on_scanline:
                self.on_scanline(self._max)

    # -- scanline (20fps, sine) --------------------------------------------
    def _scan_tick(self) -> None:
        if not self._enabled:
            if self.on_scanline:
                self.on_scanline(self._max)
            return
        now = time.monotonic()
        dt = now - self._last_tick
        self._last_tick = now
        # Operation freeze window: hold the phase (do NOT advance _elapsed) so
        # the glow pins to its current brightness.  We still emit the frozen
        # alpha so the consumer's logical value stays consistent.
        if now < self._freeze_until:
            self._just_unfroze = True
            if self.on_scanline:
                self.on_scanline(
                    scanline_alpha(self._current_phase(), self._max, self._floor))
            return
        # First tick after the freeze window: release the hold WITHOUT advancing,
        # so the glow stays exactly at the frozen brightness (zero jump); later
        # ticks resume the breath normally.
        if self._just_unfroze:
            self._just_unfroze = False
            if self.on_scanline:
                self.on_scanline(
                    scanline_alpha(self._current_phase(), self._max, self._floor))
            return
        # Safety net: a tick the timer could not fire on time (main thread fully
        # blocked during an operation with no freeze window open) is clamped so
        # it can advance at most one frame.  This kills any residual brightness
        # jump from an un-forecast stall without freezing the breath during
        # legitimately smooth running.
        if dt > _MAX_CATCHUP:
            dt = _MAX_CATCHUP
        self._elapsed += dt
        self._scan_phase = self._current_phase()
        if self.on_scanline:
            self.on_scanline(
                scanline_alpha(self._scan_phase, self._max, self._floor))

    def start_scanline(self) -> None:
        if not self._enabled:
            # Breathing disabled: emit a single steady (peak) value so the glow
            # stays lit but does NOT pulse, and do not start the timer.
            if self.on_scanline:
                self.on_scanline(self._max)
            return
        self._rebase()
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
            t.setTimerType(Qt.TimerType.PreciseTimer)
            t.setInterval(interval_ms)
            t.timeout.connect(slot)
            return t
        return RepeatingTimer(interval_ms, slot)

    def pause(self) -> None:
        if self._scan_timer is not None:
            self._scan_timer.stop()
        # 固化当前相位：暂停期间不再推进 _elapsed（timer 已停，tick 不触发），
        # resume 后从此处继续。
        self._phase_base = self._current_phase()
        self._elapsed = 0.0
        self._last_tick = time.monotonic()

    def resume(self) -> None:
        if self._scan_timer is not None:
            # 接上暂停时的相位（_phase_base 已在 pause 固化、_elapsed 归零），
            # 只把计时起点拉回「现在」，避免把暂停时长算进呼吸。
            self._last_tick = time.monotonic()
            self._scan_timer.start()

    def stop(self) -> None:
        if self._scan_timer is not None:
            self._scan_timer.stop()
        self._phase_base = self._current_phase()
        self._elapsed = 0.0
        self._last_tick = time.monotonic()
        self._scan_timer = None

    # -- pure helpers for unit tests --------------------------------------
    @staticmethod
    def scanline_sample(phase: float, max_brightness: float = 1.0) -> float:
        return scanline_alpha(phase, max_brightness)
