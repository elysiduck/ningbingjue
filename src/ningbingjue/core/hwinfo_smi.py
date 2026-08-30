# -*- coding: utf-8 -*-
"""core/hwinfo_smi.py — HWiNFO Shared Memory Interface v2 读取器（零三方依赖）。

用途与定位（凝冰结魄架构约定）
==============================
  - 只做「独立测量参照」：提供 gpu_freq_actual / tdp_actual / temp_cpu /
    temp_gpu / gpu_util 等实测通道，供 perfmon 卡片显示「目标 vs 实测」与
    偏差告警（双重检验）。
  - **绝不进入控制闭环**：GPU 浮动锁频 / 自动锁帧 / 负载门控降压继续吃
    PDH 自愈通道（core/perf_probe.SharedMonitor）。HWiNFO 是外部进程，
    轮询周期全局一档（掌机 INI SensorInterval=3000ms），数据陈旧/回退
    跳变会污染迟滞与阈值状态机——这是架构红线。
  - HWiNFO 不在线 / 数据陈旧 → available=False / fresh=False，上层回退
    显示控制值或占位，绝不报错。

共享内存布局（SMI v2，pack=1）
==============================
  Global\\HWiNFO_SENS_SM2（HWiNFO 设置里开 Shared Memory Support，
  掌机 HWiNFO64.INI 的 SensorsSM=1 已开；免费版共享内存在运行 12 小时后
  自动停用，需靠 pollTime 新鲜度守卫发现并重启 HWiNFO）：

  HWINFO_SENSORS_SHARED_MEM2:
    signature u32 | version u32 | revision u32 | pollTime i64(Unix 秒)
    sensorOffset u32 | sensorSize u32 | sensorCount u32
    readingOffset u32 | readingSize u32 | readingCount u32
  SENSOR:   sensorId u32 | sensorInst u32 | sensorNameOriginal[128] | sensorNameUser[128]
  READING:  readingType u32 | sensorIndex u32 | readingId u32
            labelOriginal[128] | labelUser[128] | unit[16]
            value f64 | valueMin f64 | valueMax f64 | valueAvg f64

  字符串编码：HWiNFO 按系统 ANSI 写（中文系统=GBK）；解码按 utf-8 →
  gbk → replace 依次尝试。labelUser 优先（用户自定义名），否则 labelOriginal。

  每次 read() 重新 OpenFileMapping+MapViewOfFile，微秒级开销，天然处理
  「HWiNFO 中途重启」（新句柄拿到新段）。map_name 可配置：生产固定
  Global\\HWiNFO_SENS_SM2；单测/本机验证用 Local\\ 假段（免管理员）。

模块只依赖 stdlib + ctypes；非 Windows 导入安全（方法全部降级 no-op）。
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field


if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:  # pragma: no cover - 非 Windows 占位
    _kernel32 = None

FILE_MAP_READ = 0x0004

# 生产环境共享内存名（HWiNFO 64 位，SMI v2）
HWINFO_SM2_MAP = "Global\\HWiNFO_SENS_SM2"

# READING.readingType 枚举（HWiNFO SMI spec）
RT_NONE = 0
RT_TEMP = 1
RT_VOLTAGE = 2
RT_FAN = 3
RT_CURRENT = 4
RT_POWER = 5
RT_CLOCK = 6
RT_USAGE = 7
RT_OTHER = 8

_TYPE_NAMES = {
    RT_NONE: "none", RT_TEMP: "temp", RT_VOLTAGE: "volt", RT_FAN: "fan",
    RT_CURRENT: "current", RT_POWER: "power", RT_CLOCK: "clock",
    RT_USAGE: "usage", RT_OTHER: "other",
}


# --------------------------------------------------------------------------
# ctypes 布局（SMI v2）
# --------------------------------------------------------------------------
class _SHARED_MEM2(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("signature", ctypes.c_uint32),
        ("version", ctypes.c_uint32),
        ("revision", ctypes.c_uint32),
        ("pollTime", ctypes.c_int64),
        ("sensorOffset", ctypes.c_uint32),
        ("sensorSize", ctypes.c_uint32),
        ("sensorCount", ctypes.c_uint32),
        ("readingOffset", ctypes.c_uint32),
        ("readingSize", ctypes.c_uint32),
        ("readingCount", ctypes.c_uint32),
    ]


class _SENSOR(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("sensorId", ctypes.c_uint32),
        ("sensorInst", ctypes.c_uint32),
        ("sensorNameOriginal", ctypes.c_char * 128),
        ("sensorNameUser", ctypes.c_char * 128),
    ]


class _READING(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("readingType", ctypes.c_uint32),
        ("sensorIndex", ctypes.c_uint32),
        ("readingId", ctypes.c_uint32),
        ("labelOriginal", ctypes.c_char * 128),
        ("labelUser", ctypes.c_char * 128),
        ("unit", ctypes.c_char * 16),
        ("value", ctypes.c_double),
        ("valueMin", ctypes.c_double),
        ("valueMax", ctypes.c_double),
        ("valueAvg", ctypes.c_double),
    ]


def _decode(raw: bytes) -> str:
    """SMI 字符串解码：utf-8 → gbk → replace。"""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8", errors="strict").rstrip("\x00").strip()
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gbk", errors="strict").rstrip("\x00").strip()
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace").rstrip("\x00").strip()


# --------------------------------------------------------------------------
# 数据对象
# --------------------------------------------------------------------------
@dataclass
class HWinfoReading:
    """单个 HWiNFO 读数。label 为显示名（labelUser 优先）。"""
    label: str
    unit: str
    value: float
    value_min: float | None = None
    value_max: float | None = None
    value_avg: float | None = None
    reading_type: int = RT_OTHER
    sensor_index: int = -1
    reading_id: int = -1

    @property
    def type_name(self) -> str:
        return _TYPE_NAMES.get(self.reading_type, "other")

    def text(self, nd: int = 1) -> str:
        """'123.4 MHz' 形式的显示文本；None 值 → '--'。"""
        if self.value is None:
            return "--"
        return f"{self.value:.{nd}f} {self.unit}".strip()


@dataclass
class HWinfoSnapshot:
    """一次读出的共享内存快照（只读副本）。"""
    readings: "dict[str, HWinfoReading]" = field(default_factory=dict)
    poll_time: float = 0.0          # HWiNFO 侧最后轮询时刻（Unix 秒）
    ok: bool = False                # 映射打开且解析成功
    err: str = ""                   # 失败原因（ok=False 时）

    @property
    def ok_readings(self) -> int:
        return len(self.readings)

    def fresh(self, stale_after: float = 30.0) -> bool:
        """数据是否新鲜（pollTime 在 stale_after 秒内）。"""
        if not self.ok or self.poll_time <= 0:
            return False
        return (time.time() - self.poll_time) <= stale_after


@dataclass
class TelemetrySnapshot:
    """语义化实测通道（配置驱动子串匹配的结果）。"""
    gpu_freq: float | None = None       # 实际 GPU 频率 [MHz]
    tdp: float | None = None            # 实测封装功耗 [W]
    temp_cpu: float | None = None       # CPU 封装温度 [℃]
    temp_gpu: float | None = None       # GPU 温度 [℃]
    gpu_util: float | None = None       # GPU 使用率 [%]
    fps_hwinfo: float | None = None     # HWiNFO 帧率（备用；正式 FPS 走 RTSS）
    batt_rate: float | None = None      # HWiNFO 电池充电率（W，负=放电 正=充电）
    source_ok: bool = False             # 任一实测通道命中
    fresh: bool = False                 # 共享内存数据新鲜
    matched: "dict[str, str]" = field(default_factory=dict)  # key -> 命中的 label
    raw: "dict[str, HWinfoReading]" = field(default_factory=dict)

    def have(self, key: str) -> bool:
        return getattr(self, key, None) is not None


# --------------------------------------------------------------------------
# 读取器
# --------------------------------------------------------------------------
class HWinfoSmi:
    """HWiNFO SMI v2 共享内存读取器。

    用法::

        smi = HWinfoSmi()                       # 生产：Global\\HWiNFO_SENS_SM2
        snap = smi.read()                       # 每次现开现读，微秒级
        if snap.ok and snap.fresh:
            for r in snap.readings.values():
                print(r.label, r.value, r.unit)
    """

    def __init__(self, map_name: str = HWINFO_SM2_MAP, stale_after: float = 30.0):
        self.map_name = map_name
        self.stale_after = stale_after

    # -- 底层映射 --------------------------------------------------------
    def _open_map(self):
        """返回 (map_view_ptr, handles...) 或抛 OSError。"""
        if _kernel32 is None:  # pragma: no cover
            raise OSError("hwinfo_smi requires Windows")
        OpenFileMappingA = _kernel32.OpenFileMappingA
        OpenFileMappingA.argtypes = [wintypes.DWORD, wintypes.BOOL, ctypes.c_char_p]
        OpenFileMappingA.restype = wintypes.HANDLE
        MapViewOfFile = _kernel32.MapViewOfFile
        MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, ctypes.c_size_t]
        MapViewOfFile.restype = ctypes.c_void_p
        UnmapViewOfFile = _kernel32.UnmapViewOfFile
        UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        UnmapViewOfFile.restype = wintypes.BOOL
        CloseHandle = _kernel32.CloseHandle
        CloseHandle.argtypes = [wintypes.HANDLE]
        CloseHandle.restype = wintypes.BOOL

        h = OpenFileMappingA(FILE_MAP_READ, False, self.map_name.encode("ascii"))
        if not h:
            raise OSError(f"OpenFileMappingA failed: {ctypes.get_last_error()} "
                          f"(HWiNFO not running / Shared Memory Support off)")
        try:
            p = MapViewOfFile(h, FILE_MAP_READ, 0, 0, 0)
            if not p:
                raise OSError(f"MapViewOfFile failed: {ctypes.get_last_error()}")
            return p, h
        except OSError:
            CloseHandle(h)
            raise

    @staticmethod
    def _close_map(p, h) -> None:
        try:
            _kernel32.UnmapViewOfFile(ctypes.c_void_p(p))
        except Exception:  # noqa: BLE001
            pass
        try:
            _kernel32.CloseHandle(h)
        except Exception:  # noqa: BLE001
            pass

    # -- 解析 -------------------------------------------------------------
    def read(self) -> HWinfoSnapshot:
        """读取并解析一次共享内存。任何失败返回 ok=False 的快照（不抛）。"""
        if sys.platform != "win32":  # pragma: no cover
            return HWinfoSnapshot(err="not windows")
        try:
            p, h = self._open_map()
        except OSError as e:
            return HWinfoSnapshot(err=str(e))
        try:
            return self._parse(p)
        finally:
            self._close_map(p, h)

    def _parse(self, p: int) -> HWinfoSnapshot:
        head = ctypes.cast(p, ctypes.POINTER(_SHARED_MEM2)).contents
        # sanity 校验（宽松：字段必须自洽，防读到损坏/非 SMI 段）
        s_off, s_size, s_cnt = head.sensorOffset, head.sensorSize, head.sensorCount
        r_off, r_size, r_cnt = head.readingOffset, head.readingSize, head.readingCount
        if s_size < 264 or r_size < 316 or s_cnt > 4096 or r_cnt > 65536:
            return HWinfoSnapshot(err=f"bad layout s={s_size} r={r_size}")
        if r_off < s_off + s_cnt * s_size:
            return HWinfoSnapshot(err="reading offset overlaps sensors")

        readings: dict[str, HWinfoReading] = {}
        for i in range(r_cnt):
            addr = p + r_off + i * r_size
            rd = ctypes.cast(addr, ctypes.POINTER(_READING)).contents
            label = _decode(rd.labelUser) or _decode(rd.labelOriginal)
            if not label:
                continue
            val = rd.value
            if val != val:  # NaN
                continue
            readings[label] = HWinfoReading(
                label=label,
                unit=_decode(rd.unit),
                value=float(val),
                value_min=float(rd.valueMin) if rd.valueMin == rd.valueMin else None,
                value_max=float(rd.valueMax) if rd.valueMax == rd.valueMax else None,
                value_avg=float(rd.valueAvg) if rd.valueAvg == rd.valueAvg else None,
                reading_type=int(rd.readingType),
                sensor_index=int(rd.sensorIndex),
                reading_id=int(rd.readingId),
            )
        if not readings:
            return HWinfoSnapshot(err="no readings parsed", poll_time=float(head.pollTime))
        return HWinfoSnapshot(
            readings=readings,
            poll_time=float(head.pollTime),
            ok=True,
        )

    # -- 便捷 -------------------------------------------------------------
    def available(self) -> bool:
        """共享内存是否存在（HWiNFO 在线 + 共享内存开启）。"""
        return self.read().ok

    def find(self, pattern: str) -> HWinfoReading | None:
        """按子串（大小写不敏感）找第一个匹配读数。"""
        snap = self.read()
        if not snap.ok:
            return None
        low = pattern.lower()
        for label, r in snap.readings.items():
            if low in label.lower():
                return r
        return None


# --------------------------------------------------------------------------
# 语义映射（配置驱动）
# --------------------------------------------------------------------------
DEFAULT_SENSOR_PATTERNS: "dict[str, list[str]]" = {
    # 候选按优先级；掌机真实名（hwinfo.CSV 8.48 中文导出）+ 常见英文名双保险
    "gpu_freq": [
        "GPU 频率 (有效)", "GPU Clock (effective)", "GPU 频率", "GPU Clock",
    ],
    "tdp": [
        "CPU 封装功率", "CPU Package Power", "APU 封装功率", "APU Package Power",
        "CPU 总功率", "CPU Package",
    ],
    "temp_cpu": [
        "CPU (Tctl/Tdie)", "CPU Package", "CPU Tctl", "CPU 封装", "CPU 温度",
    ],
    "temp_gpu": [
        "GPU 温度", "GPU Temperature", "GPU 核心温度",
    ],
    "gpu_util": [
        "GPU 使用率", "GPU Utilization", "GPU 占用",
    ],
    "fps": [
        "帧率", "Framerate", "FPS",
    ],
    "batt_rate": [
        "电池充电率", "Battery Charge Rate", "电池放电率", "Battery Discharge Rate",
        # 中文"速率/速度"变体：HWiNFO 中文系统常写作「电池充电速率/充电速度」，
        # 与"充电率"三字不连续，子串"电池充电率"匹配不上 → 必须单列。
        "电池充电速率", "充电速率", "放电速率",
        "电池充电速度", "充电速度", "放电速度",
        "充电率", "放电率",
        "Battery Charge", "Battery Power", "Battery Current",
        "放电功率", "Charge Rate",
    ],
}

# HWiNFO readingType 白名单：允许的度量类型（temp/clock/power/usage/fan）
_ALLOWED_TYPES = (RT_TEMP, RT_CLOCK, RT_POWER, RT_USAGE, RT_FAN)


class HWinfoTelemetry:
    """配置驱动的语义映射：把 readings 按子串模式映射到实测通道。

    用法::

        tel = HWinfoTelemetry(patterns)     # 缺省用 DEFAULT_SENSOR_PATTERNS
        snap = tel.snapshot(smi.read())
        snap.gpu_freq   # None 或 float（MHz）
        snap.fresh      # 共享内存数据是否新鲜
    """

    def __init__(self, patterns: "dict[str, list[str]] | None" = None):
        self.patterns = patterns or DEFAULT_SENSOR_PATTERNS

    def snapshot(self, raw: HWinfoSnapshot) -> TelemetrySnapshot:
        out = TelemetrySnapshot()
        out.fresh = raw.fresh(self.stale_after)
        if not raw.ok:
            return out
        for key, pats in self.patterns.items():
            for pat in pats:
                low = pat.lower()
                for label, r in raw.readings.items():
                    if low in label.lower():
                        out.raw[key] = r
                        out.matched[key] = label
                        out.source_ok = True
                        break
                if key in out.raw:
                    break
        # 电池充放电率兜底：子串未命中时，按「电池语境 + 单位(W/A)」抓取。
        # 覆盖掌机 HWiNFO 把 label 命名为「电池充电速率/充电速度/Battery Current」
        # 等、不在子串表里的情况；负值=放电、正值=充电，UI 已处理符号。
        # 计分：含 charge/充/放 最优先（充放电率本身），含 power/功率/current/电流
        # 次之（排除电压 V、容量 mWh 等无关 W/A 读数）。
        if "batt_rate" not in out.raw:
            best = None  # (score, label, reading)
            for label, r in raw.readings.items():
                ll = label.lower()
                if ("battery" not in ll) and ("电池" not in ll):
                    continue
                if r.unit.strip().upper() in ("W", "A") or r.reading_type in (RT_POWER, RT_CURRENT):
                    score = 0
                    if "charge" in ll or "充" in ll or "放" in ll:
                        score = 2
                    elif "power" in ll or "功率" in ll or "current" in ll or "电流" in ll:
                        score = 1
                    if best is None or score > best[0]:
                        best = (score, label, r)
            if best is not None:
                out.raw["batt_rate"] = best[2]
                out.matched["batt_rate"] = best[1]
                out.source_ok = True
        if "gpu_freq" in out.raw:
            out.gpu_freq = out.raw["gpu_freq"].value
        if "tdp" in out.raw:
            out.tdp = out.raw["tdp"].value
        if "temp_cpu" in out.raw:
            out.temp_cpu = out.raw["temp_cpu"].value
        if "temp_gpu" in out.raw:
            out.temp_gpu = out.raw["temp_gpu"].value
        if "gpu_util" in out.raw:
            out.gpu_util = out.raw["gpu_util"].value
        if "fps" in out.raw:
            out.fps_hwinfo = out.raw["fps"].value
        if "batt_rate" in out.raw:
            out.batt_rate = out.raw["batt_rate"].value
        return out

    stale_after = 30.0  # 与 HWinfoSmi 默认一致（可覆盖）


# --------------------------------------------------------------------------
# 兼容旧调用方：单例便捷入口
# --------------------------------------------------------------------------
_instance: "HWinfoTelemetry | None" = None


def telemetry() -> HWinfoTelemetry:
    """全局 HWinfoTelemetry 单例（进程内共享配置）。"""
    global _instance
    if _instance is None:
        _instance = HWinfoTelemetry()
    return _instance


def read_telemetry() -> TelemetrySnapshot:
    """一次读出语义化实测通道（perfmon 卡片等消费者入口，O(n) 无阻塞）。"""
    return telemetry().snapshot(HWinfoSmi().read())