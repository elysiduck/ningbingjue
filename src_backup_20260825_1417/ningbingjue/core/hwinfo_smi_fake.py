# -*- coding: utf-8 -*-
"""core/hwinfo_smi_fake.py — 伪造 HWiNFO SMI v2 共享内存（仅测试/本机验证用）。

为什么需要它
============
本机（开发机）没装 HWiNFO，而 `core/hwinfo_smi.py` 的读取器必须端到端验证。
本模块用 CreateFileMappingW + MapViewOfFile 造一段与真实 HWiNFO 完全同布局的
共享内存（默认 ``Local\\HWiNFO_SENS_SM2_TEST``，Local\\ 前缀免管理员），
填入掌机 HWiNFO 8.48 真实传感器名（来自 hwinfo.CSV）的假读数，读取器零改动
即可解析 —— 生产换回 ``Global\\HWiNFO_SENS_SM2`` 就是真 HWiNFO。

生产代码（app 主路径）绝不 import 本模块；PyInstaller 只打包 import 依赖，
故不会进 exe。

用法::

    from ningbingjue.core.hwinfo_smi_fake import FakeHwinfoSmi
    fake = FakeHwinfoSmi()                    # 创建 Local\\HWiNFO_SENS_SM2_TEST
    fake.set_value("GPU 频率 (有效)", 707.0)   # 模拟轮询更新
    fake.make_stale(120)                      # 模拟共享内存停更（12h 停更测试）
    fake.close()                              # 释放
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

from .hwinfo_smi import (_SHARED_MEM2, _SENSOR, _READING, _decode, _TYPE_NAMES)

if sys.platform != "win32":  # pragma: no cover
    raise RuntimeError("hwinfo_smi_fake requires Windows")

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

FILE_MAP_WRITE = 0x0002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# 掌机真实传感器名（HWiNFO 8.48，Strix Point 中文导出） + 类型
DEFAULT_READINGS = [
    # (label, unit, value, type_name)
    ("GPU 频率 (有效)", "MHz", 707.0, "clock"),
    ("GPU 频率", "MHz", 907.0, "clock"),
    ("GPU 温度", "℃", 62.0, "temp"),
    ("CPU 封装功率", "W", 22.5, "power"),
    ("CPU (Tctl/Tdie)", "℃", 71.0, "temp"),
    ("GPU 使用率", "%", 34.0, "usage"),
    ("CPU 总使用率", "%", 18.0, "usage"),
    ("帧率", "FPS", 60.0, "other"),
]

_TYPE_IDS = {v: k for k, v in _TYPE_NAMES.items() if k != 0}


def _type_id(name: str) -> int:
    return _TYPE_IDS.get(name, 8)  # 8 = OTHER


def _buf(text: str, size: int, encoding: str = "utf-8") -> bytes:
    return text.encode(encoding)[: size - 1].ljust(size, b"\x00")


class FakeHwinfoSmi:
    """伪造 HWiNFO SMI v2 段。close() 前段一直存在，可随时改值/改 pollTime。"""

    def __init__(self, map_name: str = "Local\\HWiNFO_SENS_SM2_TEST",
                 readings: "list[tuple]" | None = None,
                 encoding: str = "utf-8",
                 poll_time: "float | None" = None):
        self.map_name = map_name
        self.encoding = encoding
        self.readings = list(readings or DEFAULT_READINGS)
        self._h: int | None = None
        self._p: int | None = None
        # 布局
        self.sensor_size = ctypes.sizeof(_SENSOR)      # 264
        self.reading_size = ctypes.sizeof(_READING)    # 316
        self.sensor_count = 2                          # "GPU" / "CPU"
        self.reading_count = len(self.readings)
        self.header_size = ctypes.sizeof(_SHARED_MEM2)  # 44
        self.sensor_offset = self.header_size
        self.reading_offset = self.sensor_offset + self.sensor_count * self.sensor_size
        self.total_size = self.reading_offset + self.reading_count * self.reading_size
        self._poll_time = float(poll_time if poll_time is not None else time.time())
        self._create()

    # -- 生命周期 ---------------------------------------------------------
    def _create(self) -> None:
        CreateFileMappingW = _kernel32.CreateFileMappingW
        CreateFileMappingW.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                       wintypes.DWORD, wintypes.DWORD,
                                       wintypes.DWORD, wintypes.LPCWSTR]
        CreateFileMappingW.restype = wintypes.HANDLE
        MapViewOfFile = _kernel32.MapViewOfFile
        MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, ctypes.c_size_t]
        MapViewOfFile.restype = ctypes.c_void_p

        h = CreateFileMappingW(INVALID_HANDLE_VALUE, None, 0x04, 0,
                               self.total_size, self.map_name)
        if not h:
            raise OSError(f"CreateFileMappingW failed: {ctypes.get_last_error()}")
        p = MapViewOfFile(h, FILE_MAP_WRITE, 0, 0, 0)
        if not p:
            _kernel32.CloseHandle(h)
            raise OSError(f"MapViewOfFile failed: {ctypes.get_last_error()}")
        self._h, self._p = h, p
        self._write_all()

    def close(self) -> None:
        if self._p:
            _kernel32.UnmapViewOfFile(ctypes.c_void_p(self._p))
            self._p = None
        if self._h:
            _kernel32.CloseHandle(self._h)
            self._h = None

    def __enter__(self) -> "FakeHwinfoSmi":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- 写入 -------------------------------------------------------------
    def _write_all(self) -> None:
        assert self._p is not None
        mem = ctypes.cast(self._p, ctypes.POINTER(ctypes.c_char)).contents  # noqa: F841
        # header
        head = ctypes.cast(self._p, ctypes.POINTER(_SHARED_MEM2)).contents
        head.signature = 0x4F4E4957  # 'WINO' 低字节序（宽松校验用）
        head.version = 0x00000200
        head.revision = 0
        head.pollTime = int(self._poll_time)
        head.sensorOffset = self.sensor_offset
        head.sensorSize = self.sensor_size
        head.sensorCount = self.sensor_count
        head.readingOffset = self.reading_offset
        head.readingSize = self.reading_size
        head.readingCount = self.reading_count
        # sensors：2 组（GPU / CPU）
        for i, name in enumerate(("GPU", "CPU")):
            addr = self._p + self.sensor_offset + i * self.sensor_size
            s = ctypes.cast(addr, ctypes.POINTER(_SENSOR)).contents
            s.sensorId = 100 + i
            s.sensorInst = 0
            s.sensorNameOriginal = _buf(name, 128, self.encoding)
            s.sensorNameUser = b"\x00" * 128
        # readings
        for i, (label, unit, value, tname) in enumerate(self.readings):
            addr = self._p + self.reading_offset + i * self.reading_size
            r = ctypes.cast(addr, ctypes.POINTER(_READING)).contents
            r.readingType = _type_id(tname)
            r.sensorIndex = 0 if i < 4 else 1
            r.readingId = 1000 + i
            r.labelOriginal = _buf(label, 128, self.encoding)
            r.labelUser = b"\x00" * 128
            r.unit = _buf(unit, 16, self.encoding)
            r.value = float(value)
            r.valueMin = float(value) * 0.8
            r.valueMax = float(value) * 1.2
            r.valueAvg = float(value)

    # -- 模拟行为 ---------------------------------------------------------
    def set_value(self, label: str, value: float) -> None:
        """模拟 HWiNFO 轮询更新：改值 + 刷新 pollTime。"""
        self._poll_time = time.time()
        assert self._p is not None
        for i, (lbl, unit, _v, tname) in enumerate(self.readings):
            if lbl == label:
                self.readings[i] = (lbl, unit, float(value), tname)
                self._write_all()
                return
        raise KeyError(label)

    def refresh(self) -> None:
        """只刷新 pollTime（模拟一次正常轮询）。"""
        self._poll_time = time.time()
        assert self._p is not None
        ctypes.cast(self._p, ctypes.POINTER(_SHARED_MEM2)).contents.pollTime = int(self._poll_time)

    def make_stale(self, seconds: float = 120.0) -> None:
        """模拟共享内存停更（免费版 12h 限制）：pollTime 停在旧值。"""
        self._poll_time = time.time() - seconds
        assert self._p is not None
        ctypes.cast(self._p, ctypes.POINTER(_SHARED_MEM2)).contents.pollTime = int(self._poll_time)


def create_default(map_name: str = "Local\\HWiNFO_SENS_SM2_TEST",
                   encoding: str = "utf-8") -> FakeHwinfoSmi:
    """带掌机真实传感器名的假段。"""
    return FakeHwinfoSmi(map_name=map_name, encoding=encoding)


if __name__ == "__main__":
    # 本机冒烟：创建假段 → 读取器解析 → 打印实测通道
    import sys as _sys
    sys.path.insert(0, ".")
    from ningbingjue.core.hwinfo_smi import HWinfoSmi, HWinfoTelemetry

    with create_default() as fake:
        smi = HWinfoSmi(map_name=fake.map_name, stale_after=30.0)
        snap = smi.read()
        print(f"ok={snap.ok} readings={len(snap.readings)} pollTime_age={time.time()-snap.poll_time:.1f}s")
        tel = HWinfoTelemetry().snapshot(snap)
        print(f"gpu_freq={tel.gpu_freq} tdp={tel.tdp} temp_cpu={tel.temp_cpu} "
              f"temp_gpu={tel.temp_gpu} gpu_util={tel.gpu_util} fresh={tel.fresh}")
        fake.make_stale(120)
        snap2 = smi.read()
        tel2 = HWinfoTelemetry().snapshot(snap2)
        print(f"after stale: fresh={tel2.fresh} (期望 False)")
