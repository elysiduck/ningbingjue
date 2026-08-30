"""HWiNFO SMI v2 读取器单测 — 用假共享内存段端到端验证（无需真 HWiNFO）。

覆盖：端到端解析 / 语义映射 / 候选优先级 / 新鲜度（12h 停更）/ 值更新 /
GBK 编码（中文系统 HWiNFO 实际编码）/ 无段降级 / 损坏布局 sanity。
"""

from __future__ import annotations

import ctypes
import sys
import time

import pytest

sys.path.insert(0, "src")
from ningbingjue.core.hwinfo_smi import (
    HWinfoSmi, HWinfoTelemetry, DEFAULT_SENSOR_PATTERNS,
)
from ningbingjue.core.hwinfo_smi_fake import FakeHwinfoSmi, create_default

MAP = "Local\\HWiNFO_SENS_SM2_TEST"


@pytest.fixture()
def fake():
    with create_default(map_name=MAP) as f:
        yield f


def test_parse_end_to_end(fake):
    smi = HWinfoSmi(map_name=fake.map_name, stale_after=30.0)
    snap = smi.read()
    assert snap.ok, snap.err
    assert len(snap.readings) == 8
    assert abs(time.time() - snap.poll_time) < 60


def test_telemetry_maps_all_channels(fake):
    tel = HWinfoTelemetry().snapshot(HWinfoSmi(map_name=fake.map_name).read())
    assert tel.source_ok
    assert tel.fresh
    assert tel.gpu_freq == pytest.approx(707.0)
    assert tel.tdp == pytest.approx(22.5)
    assert tel.temp_cpu == pytest.approx(71.0)
    assert tel.temp_gpu == pytest.approx(62.0)
    assert tel.gpu_util == pytest.approx(34.0)
    assert "GPU 频率 (有效)" in tel.matched["gpu_freq"]


def test_pattern_priority_picks_effective_clock(fake):
    """gpu_freq 候选第一个是「(有效)」，必须命中它而非普通 GPU 频率。"""
    tel = HWinfoTelemetry().snapshot(HWinfoSmi(map_name=fake.map_name).read())
    assert tel.gpu_freq == pytest.approx(707.0)  # 有效频率 707，普通 907


def test_value_update_via_poll(fake):
    smi = HWinfoSmi(map_name=fake.map_name)
    fake.set_value("GPU 频率 (有效)", 450.0)
    tel = HWinfoTelemetry().snapshot(smi.read())
    assert tel.gpu_freq == pytest.approx(450.0)


def test_stale_after_free_edition_12h_limit(fake):
    smi = HWinfoSmi(map_name=fake.map_name, stale_after=30.0)
    assert smi.read().fresh() is True
    fake.make_stale(7200)  # 模拟免费版 12h 停更
    snap = smi.read()
    assert snap.ok                       # 段还在（HWiNFO 进程活着）
    assert snap.fresh(30.0) is False     # 但数据陈旧 → 触发守卫/回退


def test_no_map_graceful():
    smi = HWinfoSmi(map_name="Local\\NO_SUCH_HWINFO_MAP_XYZ")
    snap = smi.read()
    assert not snap.ok
    assert "OpenFileMappingA" in snap.err


def test_gbk_encoding_readings():
    """中文系统 HWiNFO 以 ANSI(GBK) 写 label：_decode 必须能兜底。"""
    gbk_readings = [
        ("GPU 频率 (有效)", "MHz", 800.0, "clock"),
        ("CPU 封装功率", "W", 25.0, "power"),
        ("GPU 温度", "℃", 60.0, "temp"),
    ]
    with FakeHwinfoSmi(map_name="Local\\HWiNFO_SENS_SM2_GBK", encoding="gbk",
                       readings=gbk_readings) as f:
        smi = HWinfoSmi(map_name=f.map_name)
        snap = smi.read()
        assert snap.ok
        tel = HWinfoTelemetry().snapshot(snap)
        assert tel.gpu_freq == pytest.approx(800.0)
        assert tel.tdp == pytest.approx(25.0)
        assert tel.temp_gpu == pytest.approx(60.0)


def test_corrupt_layout_sanity_rejected(fake):
    """readingOffset 与 sensor 区重叠 → sanity 拒绝，不得崩溃。"""
    smi = HWinfoSmi(map_name=fake.map_name)
    # 直接改内存里的 header：readingOffset 在 header 第 32 字节
    # （signature4+version4+revision4+pollTime8+sensorOffset4+sensorSize4+
    #  sensorCount4 = 32）
    ctypes.memmove(ctypes.cast(fake._p + 32, ctypes.POINTER(ctypes.c_uint32)),
                   ctypes.byref(ctypes.c_uint32(8)), 4)
    snap = smi.read()
    assert not snap.ok
    assert "overlaps" in snap.err


def test_default_patterns_have_en_and_cn():
    """默认配置必须中英双语候选（防 HWiNFO 语言/命名漂移）。"""
    assert len(DEFAULT_SENSOR_PATTERNS["gpu_freq"]) >= 3
    assert any("Clock" in p for p in DEFAULT_SENSOR_PATTERNS["gpu_freq"])
    assert any("GPU" in p for p in DEFAULT_SENSOR_PATTERNS["gpu_freq"])
