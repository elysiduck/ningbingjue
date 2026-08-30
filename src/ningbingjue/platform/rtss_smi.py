# -*- coding: utf-8 -*-
"""platform/rtss_smi.py — RTSS 共享内存帧率读取器（零三方依赖）。

读 RTSS (RivaTuner Statistics Server) 发布的共享内存，取每个被 hook 的
3D 应用（游戏）的**实测帧率**：

    FPS = dwFrames * 1000 / (dwTime1 - dwTime0)   # dwTime* 单位毫秒

定位（凝冰结魄架构约定）
========================
  - FPS「双重检验」的独立实测参照：RTSS 锁帧设了 30/60，**是否真的生效**
    由本模块的实测 FPS 判定。用户拍板：以 RTSS 自己的帧计数为准（比 HWiNFO
    的 ETW frametime 更直接、更贴近游戏本体的 present 流）。
  - 与 platform/rtss_cli.py（**写**：设锁帧/解除）配合，本模块只**读**。
  - RTSS 不在线 → available=False，上层回退显示控制值/占位，绝不报错。
  - 额外福利：RTSS 共享内存暴露「哪些进程被 hook（3D 应用在跑）」，可作
    自动锁帧游戏检测的**互证信号**（见 RtssMonitor.any_3d）。

共享内存布局（RTSSSharedMemoryV2，字段偏移来自
Kaldaien/BMF 提取的 RTSSSharedMemory.h，社区公开）：
  header:
    dwSignature u32 'RTSS' | dwVersion u32
    dwAppEntrySize u32 | dwAppArrOffset u32 | dwAppArrSize u32
  app entry（字段按固定偏移访问，兼容版本间尾部字段增减）：
    dwProcessID @0 | szName[260] @4 | dwFlags @264
    dwTime0 @268 | dwTime1 @272 | dwFrames @276

映射名：RTSS 以当前会话权限创建；先试 "RTSSSharedMemoryV2"（同会话），
失败再试 "Global\\\\RTSSSharedMemoryV2"（RTSS 以管理员运行、app 普通权限时）。
只依赖 stdlib + ctypes；非 Windows 导入安全。
"""
from __future__ import annotations

import ctypes
import sys

from ctypes import wintypes
from dataclasses import dataclass

if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:  # pragma: no cover
    _kernel32 = None

FILE_MAP_READ = 0x0004

RTSS_SM_SESSION = "RTSSSharedMemoryV2"
RTSS_SM_GLOBAL = "Global\\RTSSSharedMemoryV2"

# app entry 字段偏移（相对 entry 起始）
_OFF_PID = 0
_OFF_NAME = 4          # char[260]
_OFF_FLAGS = 264
_OFF_TIME0 = 268
_OFF_TIME1 = 272
_OFF_FRAMES = 276
_ENTRY_MIN_SIZE = 280

_HEADER_SIZE = 20      # dwSignature..dwAppArrSize
_SIG_RTSS = 0x53535452  # 'RTSS'（小端 'RTSS' 即 0x53535452）


@dataclass
class RTSSAppFps:
    """一个被 RTSS hook 的应用的实测帧率样本。"""
    pid: int
    name: str
    fps: float | None
    frames: int = 0
    time0: int = 0
    time1: int = 0

    @property
    def valid(self) -> bool:
        return self.fps is not None


@dataclass
class RtssSnapshot:
    apps: list  # list[RTSSAppFps]
    ok: bool = False
    err: str = ""
    version: int = 0

    def fps_for(self, pid: "int | None") -> "float | None":
        """指定进程的实测 FPS（PID 匹配）；None 表示拿不到。"""
        if not self.ok or not pid:
            return None
        best: RTSSAppFps | None = None
        for a in self.apps:
            if a.pid == pid and a.valid:
                if best is None or a.time1 >= best.time1:  # 取最新样本
                    best = a
        return best.fps if best else None

    @property
    def any_3d(self) -> bool:
        """是否有 3D 应用被 RTSS hook（可作自动锁帧互证信号）。"""
        return self.ok and bool(self.apps)


class RtssSmi:
    """RTSS 共享内存读取器。每次 read() 现开现读，微秒级。"""

    def __init__(self, session_first: bool = True):
        self.session_first = session_first

    # -- 底层 -------------------------------------------------------------
    def _open(self, map_name: str):
        if _kernel32 is None:  # pragma: no cover
            raise OSError("rtss_smi requires Windows")
        OpenFileMappingA = _kernel32.OpenFileMappingA
        OpenFileMappingA.argtypes = [wintypes.DWORD, wintypes.BOOL, ctypes.c_char_p]
        OpenFileMappingA.restype = wintypes.HANDLE
        MapViewOfFile = _kernel32.MapViewOfFile
        MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, ctypes.c_size_t]
        MapViewOfFile.restype = ctypes.c_void_p
        CloseHandle = _kernel32.CloseHandle
        CloseHandle.argtypes = [wintypes.HANDLE]
        CloseHandle.restype = wintypes.BOOL

        names = ([RTSS_SM_SESSION, RTSS_SM_GLOBAL]
                 if self.session_first else [RTSS_SM_GLOBAL, RTSS_SM_SESSION])
        last_err = 0
        for n in names:
            h = OpenFileMappingA(FILE_MAP_READ, False, n.encode("ascii"))
            if h:
                p = MapViewOfFile(h, FILE_MAP_READ, 0, 0, 0)
                if p:
                    return p, h, n
                CloseHandle(h)
            else:
                last_err = ctypes.get_last_error()
        raise OSError(f"OpenFileMappingA failed ({last_err}): RTSS not running")

    @staticmethod
    def _close(p, h) -> None:
        try:
            _kernel32.UnmapViewOfFile(ctypes.c_void_p(p))
        except Exception:  # noqa: BLE001
            pass
        try:
            _kernel32.CloseHandle(h)
        except Exception:  # noqa: BLE001
            pass

    # -- 解析 -------------------------------------------------------------
    def read(self) -> RtssSnapshot:
        if sys.platform != "win32":  # pragma: no cover
            return RtssSnapshot(apps=[], err="not windows")
        try:
            p, h, _name = self._open(RTSS_SM_SESSION)
        except OSError as e:
            return RtssSnapshot(apps=[], err=str(e))
        try:
            return self._parse(p)
        finally:
            self._close(p, h)

    def _parse(self, p: int) -> RtssSnapshot:
        raw = ctypes.string_at(p, _HEADER_SIZE)
        sig, ver, entry_size, arr_off, arr_size = (
            int.from_bytes(raw[0:4], "little"),
            int.from_bytes(raw[4:8], "little"),
            int.from_bytes(raw[8:12], "little"),
            int.from_bytes(raw[12:16], "little"),
            int.from_bytes(raw[16:20], "little"),
        )
        if sig != _SIG_RTSS:
            return RtssSnapshot(apps=[], err=f"bad signature 0x{sig:08x}")
        if entry_size < _ENTRY_MIN_SIZE or arr_size > 256 * entry_size:
            return RtssSnapshot(apps=[], err=f"bad layout entry={entry_size} size={arr_size}")
        n = min(arr_size // entry_size, 256) if entry_size else 0
        apps: list[RTSSAppFps] = []
        for i in range(n):
            base = p + arr_off + i * entry_size
            pid = int.from_bytes(ctypes.string_at(base + _OFF_PID, 4), "little")
            name_raw = ctypes.string_at(base + _OFF_NAME, 260).split(b"\x00")[0]
            name = name_raw.decode("ascii", errors="replace").strip()
            t0 = int.from_bytes(ctypes.string_at(base + _OFF_TIME0, 4), "little")
            t1 = int.from_bytes(ctypes.string_at(base + _OFF_TIME1, 4), "little")
            frames = int.from_bytes(ctypes.string_at(base + _OFF_FRAMES, 4), "little")
            fps = None
            if t1 > t0 and frames > 0:
                fps = frames * 1000.0 / (t1 - t0)
            apps.append(RTSSAppFps(pid=pid, name=name, fps=fps,
                                   frames=frames, time0=t0, time1=t1))
        return RtssSnapshot(apps=apps, ok=True, version=ver)

    # -- 便捷 -------------------------------------------------------------
    def available(self) -> bool:
        return self.read().ok


# 全局单例（与 rtss_cli 风格一致）
_instance: RtssSmi | None = None


def instance() -> RtssSmi:
    global _instance
    if _instance is None:
        _instance = RtssSmi()
    return _instance


def read_fps(pid: "int | None" = None) -> "float | None":
    """前台/指定进程的实测 FPS；RTSS 不在线或匹配不到 → None。"""
    snap = instance().read()
    if pid is not None:
        return snap.fps_for(pid)
    return snap.fps_for(None)


if __name__ == "__main__":  # pragma: no cover - 交互冒烟
    snap = instance().read()
    print(f"ok={snap.ok} version=0x{snap.version:x} apps={len(snap.apps)} err={snap.err}")
    for a in snap.apps:
        fps = f"{a.fps:.1f}" if a.valid else "--"
        print(f"  pid={a.pid:<8} {a.name:<32} fps={fps}")