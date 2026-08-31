# -*- coding: utf-8 -*-
"""platform/hwinfo_status.py — HWiNFO 运行状态五层检测（零新增依赖）。

为什么单独拆一个模块
====================
``core/hwinfo_smi.py`` 只负责「打开共享内存并解析读数」，它**不知道也不需要知道**
HWiNFO 进程本身的状态。但用户要的「检测 HWiNFO 的运行状态」是五层递进的判据，
每一层只能证明上一层为真（见下），所以单独成模块，与读取层解耦。

检测阶梯
========
::

    L1 进程层     HWiNFO64.exe 在不在跑 + 进程启动时间（12h 倒计时的钟）  便宜·可靠
    L2 共享内存层 Global\\HWiNFO_SENS_SM2 能否打开                        已有代码
    L3 数据新鲜层 pollTime 是否在 30 秒内                                 已有代码
    L4 权限层     是否管理员运行 —— 无公开接口，只能按读数数量启发式推断   仅供参考
    L5 Pro 判定层 读不出来（同 SMU 读不回 CO 值），不做                     ——

12 小时限制的处理（grill 决策：提前预警 + 事后兜底）
====================================================
免费版共享内存**连续运行 12 小时后自动停用，且必须手动重新启用**（官方许可页
footnote 4；HWiNFO 设置对话框该选项旁直接标着 ``[12-HOUR LIMIT]``）。

  * **提前预警**：共享内存开启的那一刻≈进程启动时刻，故用 L1 拿到的进程启动时间
    当钟，``remaining = 12h - uptime``，剩 30 分钟转黄、到点转红。
  * **事后兜底**：这个钟会被「用户手动重开共享内存」重置，而重启 HWiNFO 也会重置
    计时——两种情况我们**都检测不到**。所以倒计时只能当预警，**不能当判据**；
    真正的地面真值永远是 L3 的 pollTime 新鲜度。共享内存工作正常但 uptime 已超过
    12h 时，本模块会把 ``remaining`` 置为 ``-1``（未知）并完全信任 L3。

线程与开销
==========
``probe()`` 每次调用都会：psutil 进程扫描（缓存 PID 后是 O(1)）+ 一次共享内存打开
映射（微秒级）。代价足以支撑 ~6s 一次轮询，但**不要**放进 30ms 手柄轮询或 50ms
呼吸定时器。
"""

from __future__ import annotations

import os
import struct
import sys
import time
from dataclasses import dataclass

from .hwinfo_deploy import EXE_NAME, system_hwinfo_dir

# 免费版共享内存运行时上限（秒）。Pro 无此限制，但我们无法探测 Pro 与否。
SHM_LIMIT_SECS = 12 * 3600

# 剩余时间低于此值转为「黄警」，提示用户准备重启 HWiNFO。
WARN_REMAINING_SECS = 30 * 60

# L3 新鲜度窗口，与 core/hwinfo_smi.py 的 stale_after 默认一致。
FRESH_WINDOW_SECS = 30.0

# L4 启发式：正常掌机上 HWiNFO 的读数条目通常上百；非管理员运行时 kernel driver
# 加载不了，读数会大面积缺失。低于此值只作为「可能没提权」的弱提示，不做判定。
LOW_READINGS_HINT = 40


# --------------------------------------------------------------------------
# 数据对象
# --------------------------------------------------------------------------
@dataclass
class HwinfoStatus:
    """一次探测的完整结果。所有字段都保证有值，调用方无需防御。"""

    state: str = "missing"      # missing | stopped | no_sm | stale | warning | ok
    text: str = ""              # 一行中文状态，直接给 UI 显示
    exe_path: str = ""
    version: str = ""           # 形如 "8.48.5990.0"；读不到为 ""
    pid: int = 0
    uptime_s: float = 0.0       # 进程已运行秒数；未运行为 0
    remaining_s: float = -1.0   # 12h 剩余秒数；-1 = 未知（见模块 docstring）
    sm_ok: bool = False         # L2 共享内存是否打开
    fresh: bool = False         # L3 数据是否新鲜
    readings: int = 0           # 解析出的读数条数
    temp_cpu: "float | None" = None    # CPU (Tctl/Tdie) 温度 ℃
    batt_w: "float | None" = None      # 电池功率 W（正=充电，负=放电）
    maybe_not_elevated: bool = False   # L4 弱提示：可能没以管理员运行

    @property
    def running(self) -> bool:
        return self.pid > 0

    @property
    def usable(self) -> bool:
        """NBJ 能不能真的用上它的数据（L1+L2+L3 全通）。"""
        return self.sm_ok and self.fresh

    def remaining_text(self) -> str:
        """12h 倒计时的显示文本。未知返回 '—'。"""
        if self.remaining_s < 0:
            return "—"
        h = int(self.remaining_s // 3600)
        m = int((self.remaining_s % 3600) // 60)
        return f"{h}h{m:02d}m"


# --------------------------------------------------------------------------
# L1：进程层
# --------------------------------------------------------------------------
_cached_pid: "int | None" = None


def _find_process() -> "tuple[int, float] | None":
    """返回 ``(pid, create_time_unix)``；没在跑返回 ``None``。

    psutil 优先（已缓存 PID 时是 O(1) 的 ``pid_exists``）；psutil 不可用时回退
    ctypes 的 ``CreateToolhelp32Snapshot`` + ``GetProcessTimes``，不拉起子进程。
    对齐 ``platform/rtss_cli.py:189`` 的既有写法。
    """
    global _cached_pid
    if sys.platform != "win32":
        return None

    # -- O(1) 快路径：缓存的 PID 还活着就直接用 --------------------------
    if _cached_pid is not None:
        got = _probe_pid(_cached_pid)
        if got is not None:
            return got
        _cached_pid = None

    # -- 全量扫描 ---------------------------------------------------------
    for pid in _iter_pids_by_name(EXE_NAME):
        got = _probe_pid(pid)
        if got is not None:
            _cached_pid = pid
            return got
    return None


def _probe_pid(pid: int) -> "tuple[int, float] | None":
    """确认 ``pid`` 确实是 HWiNFO64.exe 并取得其启动时间。"""
    try:
        import psutil
        p = psutil.Process(pid)
        if (p.name() or "").lower() != EXE_NAME.lower():
            return None
        return (pid, float(p.create_time()))
    except Exception:
        pass
    # psutil 不可用 → ctypes 兜底
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            class _FT(ctypes.Structure):
                _fields_ = [("dwLowDateTime", wintypes.DWORD),
                            ("dwHighDateTime", wintypes.DWORD)]
            creation = _FT()
            exit_ = _FT()
            kernel = _FT()
            user = _FT()
            ok = k32.GetProcessTimes(h, ctypes.byref(creation), ctypes.byref(exit_),
                                     ctypes.byref(kernel), ctypes.byref(user))
            if not ok:
                return None
            ft = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            if ft == 0:
                return None
            # FILETIME → Unix 秒（11644473600 = 1601-01-01 到 1970-01-01 的秒数）
            return (pid, ft / 1e7 - 11644473600.0)
        finally:
            k32.CloseHandle(h)
    except Exception:
        return None


def _iter_pids_by_name(exe_name: str) -> "list[int]":
    """枚举所有名为 ``exe_name`` 的进程 PID。psutil 优先，ctypes 兜底。"""
    target = exe_name.lower()
    # psutil
    try:
        import psutil
        out = []
        for p in psutil.process_iter(["name", "pid"]):
            if (p.info.get("name") or "").lower() == target:
                out.append(int(p.info["pid"]))
        return out
    except Exception:
        pass
    # ctypes: CreateToolhelp32Snapshot
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_ulonglong),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap in (INVALID_HANDLE_VALUE, 0, None):
            return []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            out = []
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                if (entry.szExeFile or "").lower() == target:
                    out.append(int(entry.th32ProcessID))
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
            return out
        finally:
            k32.CloseHandle(snap)
    except Exception:
        return []


# --------------------------------------------------------------------------
# 版本字符串（version.dll）
# --------------------------------------------------------------------------
def read_version_string(path: str) -> str:
    """读 PE 的 ``FileVersion`` **原始字符串**（如 ``8.48-5990``）。

    ``rtss_deploy.get_file_version`` 会把它拆成整数字元组，对 HWiNFO 这种
    ``8.48-5990`` 格式会退化成 ``(8, 0, 0, 0)``，显示价值低。这里保留原始串。
    读不到返回空串。
    """
    if sys.platform != "win32" or not path or not os.path.exists(path):
        return ""
    try:
        import ctypes
        from ctypes import wintypes
        ver = ctypes.windll.version
        sz = ver.GetFileVersionInfoSizeW(ctypes.c_wchar_p(path),
                                         ctypes.byref(wintypes.DWORD(0)))
        if not sz:
            return ""
        buf = ctypes.create_string_buffer(sz)
        if not ver.GetFileVersionInfoW(ctypes.c_wchar_p(path), 0, sz, buf):
            return ""
        ptr = ctypes.c_void_p(0)
        vsz = wintypes.UINT(0)
        if not ver.VerQueryValueW(buf, ctypes.c_wchar_p(r"\VarFileInfo\Translation"),
                                  ctypes.byref(ptr), ctypes.byref(vsz)):
            return ""
        lang, codepage = struct.unpack("<HH", ctypes.string_at(ptr, 4))
        sub = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\FileVersion"
        ptr2 = ctypes.c_void_p(0)
        vsz2 = wintypes.UINT(0)
        if not ver.VerQueryValueW(buf, ctypes.c_wchar_p(sub),
                                  ctypes.byref(ptr2), ctypes.byref(vsz2)):
            return ""
        raw = ctypes.string_at(ptr2, vsz2.value).split(b"\x00", 1)[0]
        return raw.decode("utf-8", "replace").strip()
    except Exception:
        return ""


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def probe(stale_after: float = FRESH_WINDOW_SECS) -> HwinfoStatus:
    """一次完整探测。绝不抛异常——任何失败都退化成对应状态的 ``HwinfoStatus``。"""
    out = HwinfoStatus()

    # -- 定位 exe（L0）----------------------------------------------------
    try:
        d = system_hwinfo_dir()
    except Exception:
        d = None
    if not d:
        out.state = "missing"
        out.text = "HWiNFO 未安装"
        return out
    out.exe_path = os.path.join(d, EXE_NAME)
    out.version = read_version_string(out.exe_path) or ""

    # -- L1 进程层 --------------------------------------------------------
    proc = _find_process()
    if proc is None:
        out.state = "stopped"
        out.text = "HWiNFO 未运行"
        return out
    out.pid, create_ts = proc
    out.uptime_s = max(0.0, time.time() - create_ts)
    # 提前预警的钟：共享内存开启时刻≈进程启动时刻。
    # 若 uptime 已超过 12h 而共享内存还通着（说明用户手动重开过），这个钟就
    # 不可信了——见模块 docstring，届时置 -1 并完全信任 L3。
    out.remaining_s = max(0.0, SHM_LIMIT_SECS - out.uptime_s)

    # -- L2 + L3：共享内存与新鲜度 ----------------------------------------
    try:
        from ..core.hwinfo_smi import HWinfoSmi, HWinfoTelemetry
        snap = HWinfoSmi().read()
        out.sm_ok = bool(snap.ok)
        if snap.ok:
            out.readings = snap.ok_readings
            out.fresh = bool(snap.fresh(stale_after))
            tel = HWinfoTelemetry().snapshot(snap)
            out.temp_cpu = tel.temp_cpu
            out.batt_w = tel.batt_rate
    except Exception:
        out.sm_ok = False
        out.fresh = False

    # -- L4 权限层（弱提示，不参与判定）----------------------------------
    out.maybe_not_elevated = out.sm_ok and 0 < out.readings < LOW_READINGS_HINT

    # -- 综合判定 ---------------------------------------------------------
    if not out.sm_ok:
        if out.uptime_s >= SHM_LIMIT_SECS:
            out.state = "no_sm"
            out.text = "共享内存已到期（免费版 12h 限制），需重启 HWiNFO"
        else:
            out.state = "no_sm"
            out.text = "共享内存未开启（设置里勾选 Shared Memory Support）"
        out.remaining_s = -1.0
        return out

    if not out.fresh:
        out.state = "stale"
        out.text = "数据已停止更新"
        out.remaining_s = -1.0
        return out

    # 到这里 L1/L2/L3 全通
    if out.uptime_s >= SHM_LIMIT_SECS:
        # 超过 12h 却还通着 → 计时被重置过，钟不可信，交给 L3 兜底。
        out.remaining_s = -1.0
        out.state = "ok"
        out.text = "运行中"
    elif out.remaining_s <= WARN_REMAINING_SECS:
        out.state = "warning"
        out.text = f"共享内存即将到期（剩余 {out.remaining_text()}）"
    else:
        out.state = "ok"
        out.text = "运行中"
    return out


def reset_pid_cache() -> None:
    """丢弃缓存的 PID（重启 HWiNFO 后调用，避免探到已退出的旧进程）。"""
    global _cached_pid
    _cached_pid = None
