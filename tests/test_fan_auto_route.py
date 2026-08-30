"""Regression test for the fan AUTO restore routing + curve no-temp fallback.

Covers two fixes (2026-08-19):

  1. ``_restore_auto()`` must route by ``ec_enable_addr``:
       * ``ec_enable_addr != 0`` (WM2)     -> write ``(ec_enable_addr, 0)``
       * ``ec_enable_addr == 0`` (WinMini) -> write ``(ec_pwm_addr, 0)``
       Old bug: always wrote ``ec_enable_addr``, which is 0 on Win Mini /
       Win4 / Duo -> wrote to EC address 0 (a no-op) -> AUTO mode never
       actually handed control back to the BIOS.

  2. ``_fan_tick_locked()`` with ``temp is None`` must assert EC control by
     writing ``curve[0].pwm`` instead of silently keeping the last PWM.
     Old bug: no write at all -> fan stayed on BIOS control -> user thought
     CURVE mode was dead.

Run: python -m tests.test_fan_auto_route  (from ningbingjue/src)
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
sys.path.insert(0, SRC)

import types  # noqa: E402
if sys.platform != "win32":
    ec_stub = types.ModuleType("ningbingjue.platform.ec_io")
    ec_stub.health_check = lambda: False
    ec_stub.available = lambda: False
    ec_stub.write_ec = lambda addr, value: False
    ec_stub.read_ec = lambda addr: None
    ec_stub.shutdown = lambda: None
    ec_stub._is_admin = lambda: True  # type: ignore[attr-defined]
    sys.modules["ningbingjue.platform.ec_io"] = ec_stub


class _MockEC:
    """In-memory EC: records writes, allows health_check to pass."""
    def __init__(self):
        self.writes = []  # [(addr, value)]
        self._healthy = True
        self._admin = True
    def health_check(self):
        return self._healthy
    def write_ec(self, addr, value):
        self.writes.append((addr, value))
        return True
    def read_ec(self, addr):
        return 0
    def shutdown(self):
        pass
    def _is_admin(self):
        return self._admin


class _MockThermal:
    """Fixed-temperature thermal probe (None = temp unavailable)."""
    def __init__(self, temp_c=None):
        self._t = temp_c
        self.is_running = True
    def ensure_started(self):
        return True
    def read_temp_c(self):
        return self._t
    def dispose(self):
        pass


def _make_block(mode="auto", enable_addr=629, pwm_addr=6153,
                temp=None, curve=None):
    from ningbingjue.core.config import AppConfig, FanConfig
    fc = FanConfig(mode=mode, ec_enable_addr=enable_addr,
                   ec_pwm_addr=pwm_addr, hysteresis_c=3)
    if curve is not None:
        fc.curve = curve
    cfg = AppConfig()
    cfg.fan_config = fc
    ec = _MockEC()
    import ningbingjue.core.thermal as thermal_mod
    orig = thermal_mod.ThermalProbe._instance
    thermal_mod.ThermalProbe._instance = _MockThermal(temp)
    try:
        from ningbingjue.blocks.fan import FanBlock
        blk = FanBlock(cfg, ec_writer=ec)
        return blk, ec
    finally:
        thermal_mod.ThermalProbe._instance = orig


def test_restore_auto_wm2_writes_enable_addr():
    """WM2 (enable=629, pwm=6153): AUTO 交还必须写 enable_addr=0。"""
    blk, ec = _make_block(mode="auto", enable_addr=629, pwm_addr=6153)
    ok = blk._restore_auto()
    assert ok, "restore_auto returned False"
    assert ec.writes == [(629, 0)], f"WM2 应写 (629,0)，实际 {ec.writes}"
    print("[PASS] WM2 restore_auto writes (enable_addr, 0)")


def test_restore_auto_winmini_writes_pwm_addr():
    """Win Mini (enable=0, pwm=1146): 无独立使能寄存器，必须写 pwm_addr=0。

    旧实现写 (0,0) = 写到地址 0 = no-op，AUTO 失效的根因。
    """
    blk, ec = _make_block(mode="auto", enable_addr=0, pwm_addr=1146)
    ok = blk._restore_auto()
    assert ok, "restore_auto returned False"
    assert ec.writes == [(1146, 0)], f"Win Mini 应写 (1146,0)，实际 {ec.writes}"
    print("[PASS] Win Mini restore_auto writes (pwm_addr, 0)")


def test_restore_auto_winmini_apply_routes_through_restore():
    """apply(AUTO) 在 enable_addr=0 机型走 _restore_auto -> 写 pwm_addr=0。"""
    blk, ec = _make_block(mode="auto", enable_addr=0, pwm_addr=1146)
    blk._ec_health = lambda: True  # 直接短路健康检查
    ok = blk.apply(silent=True)
    assert ok, "apply(auto) failed"
    assert (1146, 0) in ec.writes, f"writes={ec.writes}"
    print("[PASS] apply(auto) on Win Mini writes pwm_addr=0")


def test_curve_tick_no_temp_falls_back_to_floor():
    """温度不可用时曲线 tick 必须写 curve[0].pwm 断言控制权（不再悬空）。"""
    curve = [{"temp": 45, "pwm": 0}, {"temp": 60, "pwm": 80},
             {"temp": 75, "pwm": 160}, {"temp": 90, "pwm": 255}]
    blk, ec = _make_block(mode="curve", temp=None, curve=curve)
    ok = blk._fan_tick_locked()
    assert ok, "curve tick with no temp returned False"
    assert ec.writes, "温度缺失时必须有一次 EC 写入（断言控制权）"
    addr, value = ec.writes[-1]
    assert value == 0, f"应写 curve[0].pwm=0，实际 value={value}"
    assert addr == 6153, f"应写 pwm_addr=6153，实际 addr={addr}"
    print("[PASS] curve tick no-temp falls back to curve[0].pwm")


def test_curve_tick_with_temp_writes_band_pwm():
    """温度正常时曲线 tick 按档位写 pwm（不回归）。"""
    curve = [{"temp": 45, "pwm": 0}, {"temp": 60, "pwm": 80},
             {"temp": 75, "pwm": 160}, {"temp": 90, "pwm": 255}]
    blk, ec = _make_block(mode="curve", temp=70.0, curve=curve)
    ok = blk._fan_tick_locked()
    assert ok
    addr, value = ec.writes[-1]
    # 70℃ 落在 60-75 档 -> 60 档 pwm=80
    assert value == 80, f"70℃ 应写 pwm=80，实际 {value}"
    print("[PASS] curve tick with temp writes band pwm")


def main():
    print("=" * 60)
    print("Fan AUTO-Route & Curve-Fallback Regression Test")
    print("=" * 60)
    test_restore_auto_wm2_writes_enable_addr()
    test_restore_auto_winmini_writes_pwm_addr()
    test_restore_auto_winmini_apply_routes_through_restore()
    test_curve_tick_no_temp_falls_back_to_floor()
    test_curve_tick_with_temp_writes_band_pwm()
    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
