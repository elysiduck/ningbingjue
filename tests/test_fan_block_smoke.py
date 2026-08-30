"""Smoke test for FanControl block integration.

Verifies:
  1. FanConfig from_dict/to_dict round-trip preserves all fields
  2. FanBlock instantiates without error (headless, mock ec_writer)
  3. FanBlock curve evaluation produces sane PWM for given temperatures
  4. FanBlock register() wires the expected hooks
  5. AppConfig.to_dict()/from_dict() preserves FanConfig across save/load
  6. Application.boot() headless registers "fan" block without error

Run: python -m tests.test_fan_block_smoke  (from ningbingjue/src)
"""
from __future__ import annotations

import os
import sys
import tempfile

# Ensure src/ is on the path so `import ningbingjue` resolves
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
sys.path.insert(0, SRC)

# Stub out non-Windows EC IO + thermal so the test runs on any platform
import types
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
    """Fixed-temperature thermal probe."""
    def __init__(self, temp_c=65.0):
        self._t = temp_c
        self.is_running = True
    def ensure_started(self):
        return True
    def read_temp_c(self):
        return self._t
    def dispose(self):
        pass


def test_fanconfig_roundtrip():
    from ningbingjue.core.config import FanConfig
    src = {
        "Mode": "curve", "ManualPct": 200,
        "Curve": [{"Temp": 50, "Pwm": 100}, {"Temp": 70, "Pwm": 180},
                  {"Temp": 85, "Pwm": 255}],
        "HysteresisC": 5, "UpdateHz": 1,
        "EcEnableAddr": 629, "EcPwmAddr": 6153, "BoardQuirk": "wm2",
    }
    fc = FanConfig.from_dict(src)
    assert fc.mode == "curve", f"mode={fc.mode}"
    assert fc.manual_pct == 200, f"manual_pct={fc.manual_pct}"
    assert fc.hysteresis_c == 5
    assert len(fc.curve) == 3
    assert fc.curve[0] == {"temp": 50, "pwm": 100}
    assert fc.ec_enable_addr == 629
    assert fc.ec_pwm_addr == 6153
    # Round-trip
    out = fc.to_dict()
    fc2 = FanConfig.from_dict(out)
    assert fc2.mode == fc.mode
    assert fc2.curve == fc.curve
    assert fc2.ec_enable_addr == fc.ec_enable_addr
    print("[PASS] FanConfig round-trip")


def test_fanblock_instantiation():
    from ningbingjue.blocks.fan import FanBlock
    from ningbingjue.core.config import AppConfig
    cfg = AppConfig()
    ec = _MockEC()
    # Inject mock thermal
    import ningbingjue.core.thermal as thermal_mod
    orig_instance = thermal_mod.ThermalProbe._instance
    thermal_mod.ThermalProbe._instance = _MockThermal(65.0)
    try:
        blk = FanBlock(cfg, ec_writer=ec)
        assert blk.mode == "auto"  # default
        assert blk.manual_pct == 128
        assert blk.curve  # has default curve
        assert blk.ec_enable_addr == 629
        assert blk.ec_pwm_addr == 6153
        print("[PASS] FanBlock instantiation")
    finally:
        thermal_mod.ThermalProbe._instance = orig_instance


def test_curve_evaluation():
    from ningbingjue.blocks.fan import FanBlock
    from ningbingjue.core.config import AppConfig, FanConfig
    fc = FanConfig(mode="curve", curve=[
        {"temp": 45, "pwm": 0},
        {"temp": 60, "pwm": 80},
        {"temp": 70, "pwm": 140},
        {"temp": 80, "pwm": 200},
        {"temp": 90, "pwm": 255},
    ], hysteresis_c=3)
    cfg = AppConfig()
    cfg.fan_config = fc
    ec = _MockEC()
    import ningbingjue.core.thermal as thermal_mod
    orig_instance = thermal_mod.ThermalProbe._instance
    thermal_mod.ThermalProbe._instance = _MockThermal(75.0)
    try:
        blk = FanBlock(cfg, ec_writer=ec)
        # 75℃ → between 70 and 80, ramps up path band = 70 (pwm=140)
        pwm = blk._eval_curve(75.0)
        assert pwm == 140, f"75℃ expected 140, got {pwm}"
        # 65℃ → between 60 and 70, ramps up path band = 60 (pwm=80)
        pwm2 = blk._eval_curve(65.0)
        assert pwm2 == 80, f"65℃ expected 80, got {pwm2}"
        # 95℃ → beyond all → last band (255)
        pwm3 = blk._eval_curve(95.0)
        assert pwm3 == 255, f"95℃ expected 255, got {pwm3}"
        # 30℃ → below all → first band's predecessor (pwm=0)
        pwm4 = blk._eval_curve(30.0)
        assert pwm4 == 0, f"30℃ expected 0, got {pwm4}"
        print("[PASS] Curve evaluation")
    finally:
        thermal_mod.ThermalProbe._instance = orig_instance


def test_register_hooks():
    from ningbingjue.blocks.fan import FanBlock
    from ningbingjue.core.config import AppConfig
    from ningbingjue.core.registry import BlockRegistry
    cfg = AppConfig()
    ec = _MockEC()
    import ningbingjue.core.thermal as thermal_mod
    orig_instance = thermal_mod.ThermalProbe._instance
    thermal_mod.ThermalProbe._instance = _MockThermal(65.0)
    try:
        blk = FanBlock(cfg, ec_writer=ec)
        reg = BlockRegistry()
        blk.register(reg)
        b = reg.get("fan")
        assert b is not None, "fan block not registered"
        assert b.type == "fan"
        assert b.name == "Fan Control"
        expected_hooks = {"GetCardHeight", "OnRefresh", "OnLayout",
                          "OnSyncTitleColor", "OnThemeChange",
                          "OnSaveConfig", "OnLoadConfig",
                          "OnActivateItem", "OnDeactivateItem"}
        missing = expected_hooks - set(b.hooks.keys())
        assert not missing, f"missing hooks: {missing}"
        print("[PASS] register() hooks")
    finally:
        thermal_mod.ThermalProbe._instance = orig_instance


def test_appconfig_persistence():
    from ningbingjue.core.config import AppConfig, FanConfig
    cfg = AppConfig()
    cfg.fan_config = FanConfig(mode="manual", manual_pct=200,
                                hysteresis_c=5, board_quirk="win_mini")
    d = cfg.to_dict()
    assert "FanConfig" in d
    cfg2 = AppConfig.from_dict(d)
    assert cfg2.fan_config.mode == "manual"
    assert cfg2.fan_config.manual_pct == 200
    assert cfg2.fan_config.hysteresis_c == 5
    assert cfg2.fan_config.board_quirk == "win_mini"
    print("[PASS] AppConfig persistence")


def test_application_boot_headless():
    """Verify Application.boot(headless=True) registers the fan block."""
    from ningbingjue.app import Application
    with tempfile.TemporaryDirectory() as td:
        # Use the src/ tree's script_root so vendored ryzenadj / RTSS exist
        script_root = os.path.join(SRC, "ningbingjue")
        app = Application(script_root,
                          config_path=os.path.join(td, "layout.json"),
                          headless=True)
        app.boot()
        assert "fan" in app.blocks, "fan block not in app.blocks"
        blk = app.blocks["fan"]
        assert blk.mode == "auto"
        # Layout.json should now contain FanConfig section
        app.config_manager.save()
        with open(app.config_path, "r", encoding="utf-8") as fh:
            content = fh.read()
        assert "FanConfig" in content, "FanConfig not persisted to layout.json"
        # dispose should not crash (and should attempt _restore_auto)
        blk.dispose()
        print("[PASS] Application.boot() headless registers fan")


def test_dispose_always_restores_auto_regardless_of_mode():
    """退出兜底回归：dispose() 必须无条件把 EC 交还 BIOS，不按 mode 门控。

    修复前只在 mode ∈ {manual,curve,silent} 时才 _restore_auto。这漏掉两类真实
    场景：(1) 用户把模式切到 AUTO 但没按 Apply，物理上 EC 仍被占用；(2) 默认
    就是 AUTO。两种情况下退出都不应把风扇钉死在手动转速。
    """
    from ningbingjue.blocks.fan import FanBlock
    from ningbingjue.core.config import AppConfig, FanConfig
    import ningbingjue.core.thermal as thermal_mod

    orig_instance = thermal_mod.ThermalProbe._instance
    thermal_mod.ThermalProbe._instance = _MockThermal(65.0)
    try:
        # 1) 默认 mode=auto（enable_addr=629 的 WM2 映射）：退出仍应写 enable=0
        cfg = AppConfig()
        ec = _MockEC()
        blk = FanBlock(cfg, ec_writer=ec)
        assert blk.mode == "auto"
        blk.dispose()
        assert (629, 0) in ec.writes, f"dispose 未交还 BIOS：{ec.writes}"

        # 2) 切到 AUTO 但没 Apply（applied=False）：物理上仍占 EC，退出必须归还
        ec2 = _MockEC()
        cfg2 = AppConfig()
        cfg2.fan_config = FanConfig(mode="auto")
        blk2 = FanBlock(cfg2, ec_writer=ec2)
        blk2.applied = False
        blk2.dispose()
        assert (629, 0) in ec2.writes, f"AUTO未Apply 时未交还 BIOS：{ec2.writes}"

        # 3) manual 模式：退出同样交还（enable_addr=0 的 Win Mini 走 pwm=0）
        ec3 = _MockEC()
        cfg3 = AppConfig()
        cfg3.fan_config = FanConfig(mode="manual", manual_pct=200,
                                   board_quirk="win_mini",
                                   ec_enable_addr=0, ec_pwm_addr=1146)
        blk3 = FanBlock(cfg3, ec_writer=ec3)
        blk3.dispose()
        assert (1146, 0) in ec3.writes, f"manual 退出未交还 BIOS：{ec3.writes}"
        print("[PASS] dispose 无条件交还 BIOS（mode 不门控）")
    finally:
        thermal_mod.ThermalProbe._instance = orig_instance


def main():
    print("=" * 60)
    print("FanControl Smoke Test")
    print("=" * 60)
    test_fanconfig_roundtrip()
    test_fanblock_instantiation()
    test_curve_evaluation()
    test_register_hooks()
    test_appconfig_persistence()
    test_application_boot_headless()
    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
