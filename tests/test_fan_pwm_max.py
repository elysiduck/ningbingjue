"""回归测试：EC 转速寄存器范围换算（pwm_max）。

根因（2026-09-01）：GPD wm2 系（Win Max 2 全系含 2025 HX370）EC 转速寄存器
0x1809 合法范围是 1..184（Linux 内核 gpd-fan 驱动 gpd_cast_pwm_range() +
MotionAssistant 反编译双源确认），旧代码把内部 0-255 PWM 原样直写，
185-255 属超范围值 → EC 固件饱和 → UI 上 94% 以上再调无效。

验证点：
  1. FanBlock._ec_reg_value 换算公式对齐内核（0→1、255→pwm_max、线性）
  2. wm2 (pwm_max=184) 下 manual 100% 实际写 184 而非 255
  3. pwm_max 未知（=0）时保持旧 0-255 直写行为（向后兼容）
  4. detect_device 命中机型自动回填 ec_pwm_max 并持久化到 FanConfig
  5. set_detected_mapping（暴力测试路径）带 pwm_max；0 时按 quirk 兜底查表
  6. _restore_auto 仍写原始 0（交还 BIOS 控制权不走换算）

Run: python -m pytest tests/test_fan_pwm_max.py  (from ningbingjue)
"""
from __future__ import annotations

import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
sys.path.insert(0, SRC)

from ningbingjue.core.config import FanConfig
from ningbingjue.platform.ec_io import QUIRK_PWM_MAX
from ningbingjue.blocks.fan import FanBlock


class _MockEC:
    """In-memory EC: records writes, allows health_check to pass."""

    def __init__(self):
        self.writes: list[tuple[int, int]] = []
        self.detect_info: dict | None = None

    def health_check(self):
        return True

    def write_ec(self, addr, value):
        self.writes.append((int(addr), int(value) & 0xFF))
        return True

    def read_ec(self, addr):
        return 0

    def shutdown(self):
        pass

    def set_ec_protocol(self, proto):
        pass

    def detect_device(self):
        return self.detect_info or {"matched": False}


def _make_block(**fc_kwargs) -> tuple[FanBlock, _MockEC]:
    fc = FanConfig(**fc_kwargs)
    fc.auto_detect = False  # 关闭启动检测，避免 mock detect_info 干扰
    ec = _MockEC()
    # FanBlock 从 config.fan_config 读配置（AppConfig 形状），用 SimpleNamespace 包一层
    cfg = types.SimpleNamespace(fan_config=fc)
    blk = FanBlock(cfg, ec_writer=ec)
    return blk, ec


# -- 1. 换算公式对齐内核 gpd_cast_pwm_range ---------------------------------

def test_cast_formula_matches_kernel():
    blk, _ = _make_block(ec_pwm_max=184)
    # 内核公式：round(pwm * (max-1) / 255) + 1，范围 1..184
    assert blk._ec_reg_value(0) == 1
    assert blk._ec_reg_value(255) == 184
    assert blk._ec_reg_value(128) == round(128 * 183 / 255) + 1
    # 单调不降 + 永不越界
    vals = [blk._ec_reg_value(p) for p in range(0, 256)]
    assert all(b >= a for a, b in zip(vals, vals[1:]))
    assert min(vals) >= 1 and max(vals) <= 184


def test_cast_other_quirks():
    blk_mini, _ = _make_block(ec_pwm_max=244)
    assert blk_mini._ec_reg_value(255) == 244
    blk_w4, _ = _make_block(ec_pwm_max=127)
    assert blk_w4._ec_reg_value(255) == 127
    assert blk_w4._ec_reg_value(0) == 1


# -- 2. wm2 100% 实际写 184 ---------------------------------------------------

def test_manual_100_writes_184_not_255():
    blk, ec = _make_block(ec_pwm_max=184, ec_enable_addr=629, ec_pwm_addr=6153,
                          mode="manual")
    blk.set_manual_pct(255)
    assert blk.apply(silent=True) is True
    writes = [(a, v) for a, v in ec.writes if a == 6153]
    assert writes == [(6153, 184)]  # 修复前这里写的是 255（超范围）


def test_write_log_includes_reg():
    blk, ec = _make_block(ec_pwm_max=184)
    assert blk._write_fan_pwm(255, reason="t") is True
    # 转速寄存器（6153=默认 wm2 pwm_addr）收到 184
    assert (6153, 184) in ec.writes


# -- 3. pwm_max 未知 → 旧行为直写（向后兼容） --------------------------------

def test_unknown_pwm_max_keeps_legacy_raw_write():
    blk, ec = _make_block(ec_pwm_max=0)
    assert blk.ec_reg_max == 255
    assert blk._ec_reg_value(200) == 200
    assert blk._write_fan_pwm(255) is True
    assert (blk.ec_pwm_addr, 255) in ec.writes


# -- 4. detect_device 回填 + 持久化 -------------------------------------------

def test_detect_device_fills_pwm_max_and_persists():
    blk, ec = _make_block(ec_pwm_max=0)
    ec.detect_info = {
        "matched": True,
        "model": "GPD WinMax2",
        "board_quirk": "wm2",
        "protocol": "4e4f",
        "enable_addr": 629,
        "pwm_addr": 6153,
        "rpm_addr": 6154,
        "pwm_max": 184,
    }
    assert blk.detect_device(silent=True) is True
    assert blk.ec_pwm_max == 184
    assert blk.save_config().ec_pwm_max == 184  # 已持久化到 FanConfig


def test_detect_device_fallback_via_quirk():
    blk, ec = _make_block(ec_pwm_max=0)
    ec.detect_info = {  # 旧格式 info（无 pwm_max 字段）
        "matched": True, "model": "x", "board_quirk": "wm2",
        "protocol": "4e4f", "enable_addr": 629, "pwm_addr": 6153,
        "rpm_addr": 6154,
    }
    assert blk.detect_device(silent=True) is True
    assert blk.ec_pwm_max == QUIRK_PWM_MAX["wm2"] == 184


# -- 5. set_detected_mapping（暴力测试路径） ----------------------------------

def test_set_detected_mapping_accepts_pwm_max():
    blk, _ = _make_block()
    blk.set_detected_mapping("wm2", "4e4f", 629, 6153, 6154,
                             model_hint="暴力测试确认(A)", pwm_max=184,
                             persist=False)
    assert blk.ec_pwm_max == 184


def test_set_detected_mapping_quirk_fallback():
    blk, _ = _make_block()
    blk.set_detected_mapping("win_mini", "4e4f", 0, 1146, 1147,
                             persist=False, pwm_max=0)
    assert blk.ec_pwm_max == QUIRK_PWM_MAX["win_mini"] == 244


# -- 6. _restore_auto 交还控制权不走换算 --------------------------------------

def test_restore_auto_writes_raw_zero():
    blk, ec = _make_block(ec_pwm_max=184, ec_enable_addr=629)
    assert blk._restore_auto() is True
    assert (629, 0) in ec.writes  # 使能寄存器写 0，非 1（换算后最小值）


def test_config_roundtrip_ec_pwm_max():
    fc = FanConfig(ec_pwm_max=184)
    fc2 = FanConfig.from_dict(fc.to_dict())
    assert fc2.ec_pwm_max == 184
