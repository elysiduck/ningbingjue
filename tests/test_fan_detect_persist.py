"""回归测试：启动自动识别的结果不被 OnLoadConfig 覆盖。

根因：FanBlock.__init__ 里 detect_device(silent=True) 识别成功但原先不
save_config()，随后 boot() 调 OnLoadConfig(=load_config) 用旧 board_quirk
覆盖回来 → 重装后启动默认 wm2、要手动点识别。

修复：detect_device 匹配成功即写回 config（无论 silent）。本测试验证：
构造 FanBlock(默认 wm2 配置) → __init__ 自动识别 win_mini → load_config()
重读 config 后仍是 win_mini（不被覆盖回 wm2）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class _MockEC:
    """Mock ec_io：detect_device 恒返回 Win Mini，其余方法 no-op。"""
    def detect_device(self):
        return {
            "matched": True,
            "model": "GPD G1617-02 G1617-02 GPD G1617-02",
            "board_quirk": "win_mini",
            "protocol": "4e4f",
            "enable_addr": 0,
            "pwm_addr": 1146,
            "rpm_addr": 1147,
        }
    def set_ec_protocol(self, proto):
        pass
    def health_check(self):
        return True
    def write_ec(self, addr, value):
        return True
    def read_ec(self, addr):
        return 0
    def shutdown(self):
        pass


class _MockThermal:
    def __init__(self, temp_c=65.0):
        self._t = temp_c
        self.is_running = True
    def ensure_started(self):
        return True
    def read_temp_c(self):
        return self._t
    def dispose(self):
        pass


def test_detect_result_survives_load_config():
    import ningbingjue.core.thermal as thermal_mod
    from ningbingjue.core.config import AppConfig
    from ningbingjue.blocks.fan import FanBlock

    cfg = AppConfig()  # fan_config 默认 board_quirk="wm2", auto_detect=True
    assert cfg.fan_config.board_quirk == "wm2"
    assert cfg.fan_config.auto_detect is True

    orig = thermal_mod.ThermalProbe._instance
    thermal_mod.ThermalProbe._instance = _MockThermal(65.0)
    try:
        blk = FanBlock(cfg, ec_writer=_MockEC())
        # __init__ 里 auto_detect=True → detect_device(silent=True) → 识别 win_mini
        assert blk.board_quirk == "win_mini", blk.board_quirk
        assert blk.detected_model != "", "detected_model 应为非空"
        # 关键：识别结果已写回 config.fan_config（save_config 无条件执行）
        assert cfg.fan_config.board_quirk == "win_mini", cfg.fan_config.board_quirk

        # 模拟 boot() 的 OnLoadConfig：load_config 从 config 重读，不得覆盖回 wm2
        blk.load_config()
        assert blk.board_quirk == "win_mini", f"load_config 覆盖了识别结果: {blk.board_quirk}"
        assert cfg.fan_config.board_quirk == "win_mini"
        print("[PASS] detect result survives load_config")
    finally:
        thermal_mod.ThermalProbe._instance = orig


if __name__ == "__main__":
    test_detect_result_survives_load_config()
    print("=== ALL detect-persist tests passed ===")
