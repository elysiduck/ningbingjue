"""Battery status probe — mirrors the original ``Update-Battery``.

The original reads ``SystemInformation::PowerStatus`` and renders a 5-cell
energy bar.  Here we expose the same data as a plain tuple:

    ``(percent: int, charging: bool, available: bool)``

Resolution order:
1. ``psutil.sensors_battery()`` when psutil is installed.
2. The Windows ``SYSTEM_POWER_STATUS`` API via ``ctypes`` (no dependency).
3. ``None`` when there is no battery / the platform is unsupported
   (headless server, desktop without a battery, non-Windows).

The function is pure and dependency-free on the common path, so it is safe
to call from the GUI thread on a timer and from headless logic tests.
"""

from __future__ import annotations

import sys
from typing import Optional, Tuple


def get_battery_status() -> Optional[Tuple[int, bool, bool]]:
    """Return ``(percent, charging, available)`` or ``None`` if no battery."""
    # 1) psutil (preferred, cross-platform, no ctypes quirks).
    try:  # pragma: no cover - depends on optional dependency
        import psutil

        batt = psutil.sensors_battery()
        if batt is None:
            return None
        pct = int(batt.percent)
        if pct > 100:
            pct = 100
        if pct < 0:
            pct = 0
        return (pct, bool(batt.power_plugged), True)
    except Exception:
        pass

    # 2) Windows SYSTEM_POWER_STATUS (no extra dependency).
    if sys.platform.startswith("win"):
        try:  # pragma: no cover - Windows-only path
            import ctypes

            class _SYSTEM_POWER_STATUS(ctypes.Structure):
                _fields_ = [
                    ("ACLineStatus", ctypes.c_byte),
                    ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte),
                    ("Reserved1", ctypes.c_byte),
                    ("BatteryLifeTime", ctypes.c_ulong),
                    ("BatteryFullLifeTime", ctypes.c_ulong),
                ]

            stat = _SYSTEM_POWER_STATUS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(  # type: ignore[attr-defined]
                ctypes.byref(stat)
            ):
                # BatteryFlag & 0x80 == "no system battery".
                if stat.BatteryFlag & 0x80:
                    return None
                pct = int(stat.BatteryLifePercent)
                if pct > 100:
                    pct = 100
                if pct < 0:
                    pct = 0
                charging = stat.ACLineStatus == 1
                return (pct, charging, True)
            return None
        except Exception:
            pass

    # 3) No battery / unsupported.
    return None
