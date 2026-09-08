"""ec_io.detect_device() 机型板号匹配单测（无需管理员/驱动）。

验证 GPD 真实 DMI 板号（G1617-xx 等）能正确路由到对应 board_quirk，
修复「Win Mini 被误识别成 WM2」的回归。

来源对照：Linux 主线 drivers/hwmon/gpd-fan.c 的 dmi_table。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import ningbingjue.platform.ec_io as ec


def _detect_for_model(model: str) -> dict:
    """用固定机型串跑一次 detect_device（mock 掉 PowerShell 子进程）。"""
    orig = ec.read_system_model
    ec.read_system_model = lambda: model
    try:
        return ec.detect_device()
    finally:
        ec.read_system_model = orig


def test_win_mini_hx370():
    r = _detect_for_model("GPD G1617-02 G1617-02 GPD G1617-02")
    assert r["matched"] is True, r
    assert r["board_quirk"] == "win_mini", r
    assert r["pwm_addr"] == 1146, r


def test_win_mini_hx370_l():
    r = _detect_for_model("GPD G1617-02-L G1617-02-L GPD G1617-02-L")
    assert r["matched"] is True, r
    assert r["board_quirk"] == "win_mini", r


def test_win_mini_8840u():
    r = _detect_for_model("GPD G1617-01 G1617-01 GPD G1617-01")
    assert r["matched"] is True, r
    assert r["board_quirk"] == "win_mini", r


def test_win_max2_8840u():
    r = _detect_for_model("GPD G1619-04 G1619-04 GPD G1619-04")
    assert r["matched"] is True, r
    assert r["board_quirk"] == "wm2", r
    assert r["pwm_addr"] == 6153, r


def test_win_max2_hx370():
    r = _detect_for_model("GPD G1619-05 G1619-05 GPD G1619-05")
    assert r["matched"] is True, r
    assert r["board_quirk"] == "wm2", r


def test_win4():
    r = _detect_for_model("GPD G1618-04 G1618-04 GPD G1618-04")
    assert r["matched"] is True, r
    assert r["board_quirk"] == "win4", r
    assert r["protocol"] == "2e2f", r


def test_duo():
    r = _detect_for_model("GPD G1622-01 G1622-01 GPD G1622-01")
    assert r["matched"] is True, r
    assert r["board_quirk"] == "duo", r


def test_pocket4_reuses_win_mini():
    r = _detect_for_model("GPD G1628-04 G1628-04 GPD G1628-04")
    assert r["matched"] is True, r
    assert r["board_quirk"] == "win_mini", r


def test_non_gpd_falls_back_unmatched():
    # 非 GPD 机型：不得误判为已知设备，matched=False（board_quirk 仍回退 wm2 默认）
    r = _detect_for_model("ASUSTeK ROG Zephyrus G14 GA403UV GA403UV")
    assert r["matched"] is False, r


def test_human_readable_fallback_still_works():
    # 兜底：某些固件真的上报人读名
    r = _detect_for_model("GPD WinMini GPD WinMini")
    assert r["matched"] is True, r
    assert r["board_quirk"] == "win_mini", r


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("=== ALL fan detect_device tests passed ===")
