"""core/thermal.py — Temperature probe (WMI MSAcpi_ThermalZoneTemperature).

2026-08-19: 温度采样已并入 ``core/perf_probe.SharedMonitor`` 合并采样宿主
（COMBINED_PS 每 tick 输出 ``T <temp_c>`` 一行，°C 换算在 PowerShell 侧完成）。
本模块保留 ``ThermalProbe`` 单例外壳，把 temp 通道的订阅/注销与快照状态
转换封装给消费方（风扇控制等），**对外 API 不变**：``ensure_started`` /
``snapshot`` / ``warmed`` / ``is_running`` / ``read_temp_c`` / ``dispose``。
旧实现（THERMAL_PS + 独立 PowerShell 进程 + _read_loop）已删除——它没有挂
Job Object，硬崩溃会留孤儿进程；合并后由宿主统一兜底。

Why WMI:
  - Zero native dependency (no LHM DLL, no HWiNFO, no inpoutx64)
  - Works on every Windows machine out-of-box
  - ``MSAcpi_ThermalZoneTemperature`` is exposed by ACPI (every modern mobo)
  - Reads ``CurrentTemperature`` in tenths of Kelvin → convert to °C

Accuracy caveat (user memory 2026-07-24):
  - On Strix Point / HX370 this is the ACPI thermal zone reading, which approximates
    CPU skin/package temp.  Precision is good enough for fan hysteresis control
    (3°C tolerance, MA uses the same family of source).  For absolute core
    junction temp use HWiNFO instead; this is a *control signal*, not a
    *measurement*.

Non-Windows → subscription is a no-op; ``ensure_started`` returns False.
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Optional

from .perf_probe import SharedMonitor


class ThermalProbe:
    """Singleton thermal probe fed by the shared merged monitor.

    State shape (unchanged)::

      ``{"temp_c": float|None, "ok": bool, "err": str, "ts": float}``

    Thread-safe reads via the snapshot property.  Idempotent ``ensure_started``
    — safe to call from any thread, multiple times.
    """
    _instance: "ThermalProbe | None" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._state: dict = {"temp_c": None, "ok": False, "err": ""}
        self._subscribed = False

    @classmethod
    def instance(cls) -> "ThermalProbe":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @property
    def snapshot(self) -> dict:
        """Return the latest sample dict (always returns the same object;
        readers should copy out fields if they need stable snapshots)."""
        return self._state

    @property
    def warmed(self) -> bool:
        """True once at least one valid temperature sample has been received."""
        return bool(self._state.get("ok"))

    @property
    def is_running(self) -> bool:
        return self._subscribed and SharedMonitor.instance().is_running

    def ensure_started(self) -> bool:
        """Subscribe to the merged monitor's temp channel. Idempotent."""
        if not sys.platform == "win32":
            return False
        if self._subscribed:
            return True
        SharedMonitor.instance().subscribe("temp", self._on_temp)
        self._subscribed = True
        return True

    def _on_temp(self, c: float) -> None:
        """Reader-thread callback: newest thermal zone temp in °C.

        The PowerShell side already picked the hottest zone and converted
        tenths/centi-Kelvin → °C, so this is a plain float write.
        """
        self._state["temp_c"] = float(c)
        self._state["ok"] = True
        self._state["err"] = ""
        self._state["ts"] = time.monotonic()

    def dispose(self) -> None:
        """Unsubscribe from the merged monitor. Safe to call multiple times."""
        if self._subscribed:
            SharedMonitor.instance().unsubscribe("temp", self._on_temp)
        self._subscribed = False
        self._state = {"temp_c": None, "ok": False, "err": ""}

    def read_temp_c(self) -> Optional[float]:
        """Convenience: return the latest temperature in °C, or None."""
        if not self._subscribed:
            self.ensure_started()
        s = self._state
        if s.get("ok") and s.get("temp_c") is not None:
            return float(s["temp_c"])
        return None
