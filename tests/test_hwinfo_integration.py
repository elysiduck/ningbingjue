"""HWiNFO 整合单测 — 五层状态检测 / 12h 倒计时 / INI 不覆盖 / 风扇降级护栏。

全部用假数据 + monkeypatch，不需要真 HWiNFO、不需要管理员、不碰共享内存。

覆盖的 grill 决策：
  * 决策 3「提前预警 + 事后兜底」：倒计时来自进程 uptime，但 uptime 超过 12h
    而共享内存还通着时必须把倒计时判为未知（-1），完全信任 pollTime 新鲜度。
  * 决策 4「释放审计版 INI，绝不覆盖用户改动」。
  * 决策 6「温度源回落必须明示，绝不静默」。
"""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, "src")

from ningbingjue.platform import hwinfo_deploy, hwinfo_status
from ningbingjue.platform.hwinfo_status import (
    SHM_LIMIT_SECS,
    HwinfoStatus,
    probe,
)


# --------------------------------------------------------------------------
# 假数据
# --------------------------------------------------------------------------
class _FakeSnap:
    def __init__(self, ok: bool, fresh: bool, readings: int = 120,
                 temp: float = 62.0, batt: float = -18.5) -> None:
        self.ok = ok
        self._fresh = fresh
        self._readings = readings

    def ok_readings(self) -> int:  # 属性在真类是 property，这里模拟
        return self._readings

    # HWinfoSnapshot.ok_readings 是 property；测试里直接构造 dataclass 更简单
    def fresh(self, stale_after: float = 30.0) -> bool:
        return self._fresh


def _real_snapshot(ok: bool, fresh: bool, readings: int = 120):
    """构造真 HWinfoSnapshot，避免和 dataclass 定义漂移。"""
    from ningbingjue.core.hwinfo_smi import HWinfoSnapshot, HWinfoReading

    if not ok:
        return HWinfoSnapshot(ok=False, err="no sm")
    rd = {}
    for i in range(readings):
        label = f"sensor{i}"
        rd[label] = HWinfoReading(label=label, unit="C", value=50.0,
                                  reading_type=1)
    # 温度 / 电池走语义映射的既有 pattern
    rd["CPU (Tctl/Tdie)"] = HWinfoReading(
        label="CPU (Tctl/Tdie)", unit="°C", value=62.0, reading_type=1)
    rd["电池充电率"] = HWinfoReading(
        label="电池充电率", unit="W", value=-18.5, reading_type=5)
    return HWinfoSnapshot(
        readings=rd,
        poll_time=(time.time() if fresh else time.time() - 3600),
        ok=True,
    )


@pytest.fixture()
def fake_env(monkeypatch, tmp_path):
    """把 HWiNFO 目录指向临时目录，并接管进程探测与共享内存读取。"""
    exe = tmp_path / "HWiNFO64.exe"
    exe.write_bytes(b"MZ\x00\x00")  # 占位，只要求文件存在

    monkeypatch.setattr(hwinfo_deploy, "system_hwinfo_dir", lambda: str(tmp_path))
    # hwinfo_status 在模块导入时 from ... import 了 system_hwinfo_dir，需就地替换
    monkeypatch.setattr(hwinfo_status, "system_hwinfo_dir", lambda: str(tmp_path))
    monkeypatch.setattr(hwinfo_status, "read_version_string", lambda p: "8.48-5990")

    state = {"pid": None, "snap": None}

    def _find():
        return state["pid"]

    monkeypatch.setattr(hwinfo_status, "_find_process", _find)
    return state, str(tmp_path)


def _set_running(state, uptime_s: float):
    state["pid"] = (4321, time.time() - uptime_s)


def _patch_smi(monkeypatch, ok: bool, fresh: bool, readings: int = 120):
    """替换 core.hwinfo_smi 的读取入口，返回可控快照。"""
    import ningbingjue.core.hwinfo_smi as smi

    class _Smi:
        def __init__(self, *a, **k):
            pass

        def read(self):
            return _real_snapshot(ok, fresh, readings)

    monkeypatch.setattr(smi, "HWinfoSmi", _Smi)


# --------------------------------------------------------------------------
# L1 进程层
# --------------------------------------------------------------------------
def test_stopped_when_process_not_running(fake_env, monkeypatch):
    state, _ = fake_env
    state["pid"] = None
    _patch_smi(monkeypatch, ok=False, fresh=False)

    st = probe()
    assert st.state == "stopped"
    assert st.text == "HWiNFO 未运行"
    assert not st.running
    assert not st.usable


# --------------------------------------------------------------------------
# L2/L3 共享内存与新鲜度
# --------------------------------------------------------------------------
def test_no_sm_when_shared_memory_closed(fake_env, monkeypatch):
    state, _ = fake_env
    _set_running(state, uptime_s=60)
    _patch_smi(monkeypatch, ok=False, fresh=False)

    st = probe()
    assert st.state == "no_sm"
    assert "共享内存未开启" in st.text
    assert not st.usable


def test_no_sm_blames_12h_limit_when_uptime_exceeded(fake_env, monkeypatch):
    """uptime ≥ 12h 且共享内存打不开 → 文案必须点名 12h 限制。"""
    state, _ = fake_env
    _set_running(state, uptime_s=SHM_LIMIT_SECS + 60)
    _patch_smi(monkeypatch, ok=False, fresh=False)

    st = probe()
    assert st.state == "no_sm"
    assert "12h" in st.text


def test_stale_when_polltime_old(fake_env, monkeypatch):
    state, _ = fake_env
    _set_running(state, uptime_s=120)
    _patch_smi(monkeypatch, ok=True, fresh=False, readings=120)

    st = probe()
    assert st.state == "stale"
    assert st.sm_ok and not st.fresh
    assert not st.usable


# --------------------------------------------------------------------------
# 12h 倒计时：决策 3
# --------------------------------------------------------------------------
def test_ok_with_countdown(fake_env, monkeypatch):
    state, _ = fake_env
    _set_running(state, uptime_s=3600)          # 已跑 1h → 剩 11h
    _patch_smi(monkeypatch, ok=True, fresh=True, readings=120)

    st = probe()
    assert st.state == "ok"
    assert st.usable
    assert 10.5 * 3600 < st.remaining_s < 11.5 * 3600
    # 39599.99s 会截断成 "10h59m"，故只校验格式与量级，不钉死字面量。
    import re
    assert re.fullmatch(r"1[01]h\d{2}m", st.remaining_text()), st.remaining_text()


def test_warning_when_under_30min_left(fake_env, monkeypatch):
    state, _ = fake_env
    _set_running(state, uptime_s=SHM_LIMIT_SECS - 900)   # 剩 15 分钟
    _patch_smi(monkeypatch, ok=True, fresh=True, readings=120)

    st = probe()
    assert st.state == "warning"
    assert "即将到期" in st.text
    assert st.remaining_text().startswith("0h")


def test_countdown_unknown_when_uptime_past_12h_but_sm_alive(fake_env, monkeypatch):
    """决策 3 的核心：钟被重置过就不可信，置 -1 并完全信任新鲜度。

    场景：用户手动在 HWiNFO 里重新勾了 Shared Memory Support（或重启了它），
    共享内存活得好好的，但进程 uptime 已经超过 12h。此时若还按 uptime 报警
    就是误报——判据只能是 pollTime。
    """
    state, _ = fake_env
    _set_running(state, uptime_s=SHM_LIMIT_SECS + 7200)   # 跑了 14h
    _patch_smi(monkeypatch, ok=True, fresh=True, readings=120)

    st = probe()
    assert st.state == "ok", "共享内存通着就不能报警，倒计时必须让位给新鲜度"
    assert st.remaining_s == -1.0
    assert st.remaining_text() == "—"


# --------------------------------------------------------------------------
# L4 权限弱提示
# --------------------------------------------------------------------------
def test_low_readings_flags_maybe_not_elevated(fake_env, monkeypatch):
    state, _ = fake_env
    _set_running(state, uptime_s=60)
    _patch_smi(monkeypatch, ok=True, fresh=True, readings=5)  # 读数异常少

    st = probe()
    assert st.maybe_not_elevated is True
    # 但不参与判定：状态依然是 ok
    assert st.state == "ok"


# --------------------------------------------------------------------------
# 语义读数透传
# --------------------------------------------------------------------------
def test_telemetry_passthrough(fake_env, monkeypatch):
    state, _ = fake_env
    _set_running(state, uptime_s=60)
    _patch_smi(monkeypatch, ok=True, fresh=True, readings=120)

    st = probe()
    assert st.temp_cpu == pytest.approx(62.0)
    assert st.batt_w == pytest.approx(-18.5)


# --------------------------------------------------------------------------
# 决策 4：INI 策略
# --------------------------------------------------------------------------
def test_ensure_ini_writes_only_four_good_keys(tmp_path):
    hwinfo_deploy.ensure_ini(str(tmp_path))
    txt = (tmp_path / "HWiNFO64.INI").read_text(encoding="utf-8")

    assert "SensorsSM=1" in txt        # 共享内存必须开
    assert "SensorsOnly=1" in txt
    assert "MinimalizeSensors=1" in txt
    assert "AutoUpdate=0" in txt
    # 危险关闭项一律不写（EC=0 会让掌机电池传感器整体消失）
    for bad in ("EC=0", "SVID_VR=0", "SMBus=0", "LPC=0", "GPUI2C=0", "SMART=0"):
        assert bad not in txt


def test_ensure_ini_never_overwrites_user_values(tmp_path):
    """用户把 SensorsSM 关掉 + 加了自定义项，重跑部署绝不能改回去。"""
    p = tmp_path / "HWiNFO64.INI"
    p.write_text("[Settings]\r\nSensorsSM=0\r\nMyCustomKey=42\r\n", encoding="utf-8")

    hwinfo_deploy.ensure_ini(str(tmp_path))
    txt = p.read_text(encoding="utf-8")

    assert "SensorsSM=0" in txt, "用户主动关掉共享内存，不得擅自打开"
    assert "MyCustomKey=42" in txt, "用户自定义项不得丢失"
    assert txt.count("SensorsSM=") == 1, "不得写出重复键"


def test_ensure_ini_backfills_only_missing_keys(tmp_path):
    p = tmp_path / "HWiNFO64.INI"
    p.write_text("[Settings]\r\nSensorsSM=1\r\n", encoding="utf-8")

    hwinfo_deploy.ensure_ini(str(tmp_path))
    txt = p.read_text(encoding="utf-8")

    assert "SensorsOnly=1" in txt, "缺失的白名单键应补写"
    assert txt.count("SensorsSM=") == 1


# --------------------------------------------------------------------------
# 决策 6：风扇温度源回落护栏
# --------------------------------------------------------------------------
def test_fan_temp_degraded_flag_on_fallback(monkeypatch):
    """TempSource=hwinfo 但读不到 → temp_degraded 必须置 True 且文案点名。"""
    from ningbingjue.blocks.fan import FanBlock

    blk = FanBlock.__new__(FanBlock)          # 不走完整 __init__（避免拉 EC/驱动）
    blk.temp_source = "hwinfo"
    blk._temp_src_used = "hwinfo"
    blk.temp_degraded = False
    blk.ec_temp_addr = 0

    # hwinfo 读不到 → None；acpi 能读到
    monkeypatch.setattr(blk, "_read_temp_hwinfo", lambda: None)
    monkeypatch.setattr(blk, "_read_temp_acpi", lambda: 45.0)
    monkeypatch.setattr(blk, "_read_temp_ec", lambda: None)

    val = blk._read_temp()
    assert val == 45.0, "必须回落到 ACPI，风扇控制不能停摆"
    assert blk.temp_degraded is True
    txt = blk.temp_degraded_text
    assert "ACPI" in txt and "HWiNFO" in txt, "文案必须点名是从哪个源回落的"


def test_fan_temp_not_degraded_when_hwinfo_works(monkeypatch):
    from ningbingjue.blocks.fan import FanBlock

    blk = FanBlock.__new__(FanBlock)
    blk.temp_source = "hwinfo"
    blk._temp_src_used = "hwinfo"
    blk.temp_degraded = True
    blk.ec_temp_addr = 0

    monkeypatch.setattr(blk, "_read_temp_hwinfo", lambda: 78.0)
    monkeypatch.setattr(blk, "_read_temp_acpi", lambda: 45.0)

    val = blk._read_temp()
    assert val == 78.0
    assert blk.temp_degraded is False
    assert blk.temp_degraded_text == ""


def test_fan_acpi_source_never_reports_degraded(monkeypatch):
    """默认 acpi 来源不算降级——用户从没要求更高精度，没什么可降的。"""
    from ningbingjue.blocks.fan import FanBlock

    blk = FanBlock.__new__(FanBlock)
    blk.temp_source = "acpi"
    blk._temp_src_used = "acpi"
    blk.temp_degraded = True
    blk.ec_temp_addr = 0

    monkeypatch.setattr(blk, "_read_temp_acpi", lambda: 45.0)

    blk._read_temp()
    assert blk.temp_degraded is False
    assert blk.temp_degraded_text == ""


# --------------------------------------------------------------------------
# Block 层
# --------------------------------------------------------------------------
def test_block_probe_is_throttled(fake_env, monkeypatch):
    """面板每秒 sync，真探测必须被 TTL 节流，不能每次都扫进程。"""
    from ningbingjue.blocks.hwinfo import HwinfoBlock

    state, _ = fake_env
    _set_running(state, uptime_s=60)
    _patch_smi(monkeypatch, ok=True, fresh=True, readings=120)

    calls = {"n": 0}
    real = hwinfo_status.probe

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(hwinfo_status, "probe", counting)

    blk = HwinfoBlock()
    blk.probe_status(force=True)      # 第 1 次必探
    for _ in range(20):
        blk.probe_status()            # 面板高频调用
    assert calls["n"] == 1, "TTL 内不得重复探测"

    blk.probe_status(force=True)
    assert calls["n"] == 2, "force 必须穿透 TTL"


def test_block_register_exposes_hooks():
    from ningbingjue.blocks.hwinfo import HwinfoBlock
    from ningbingjue.core.registry import BlockRegistry

    blk = HwinfoBlock()
    reg = BlockRegistry()
    blk.register(reg)

    b = reg.get("hwinfo")
    assert b is not None
    assert b.type == "hwinfo"
    for hook in ("GetCardHeight", "OnRefresh", "OnSaveConfig",
                 "OnLoadConfig", "OnPowerResume"):
        assert callable(b.hook(hook)), f"缺少钩子 {hook}"


def test_block_restart_aborts_when_exe_missing(monkeypatch, tmp_path):
    """找不到 exe 时必须优雅失败并弹 toast，绝不抛异常。"""
    from ningbingjue.blocks.hwinfo import HwinfoBlock

    monkeypatch.setattr(hwinfo_status, "system_hwinfo_dir", lambda: None)
    blk = HwinfoBlock()
    toasts: list = []
    # 签名必须含 success 关键字——生产代码以关键字方式调用。
    blk.show_toast = lambda text, success=True: toasts.append((text, success))

    assert blk.restart_hwinfo() is False
    assert toasts and toasts[-1][1] is False
