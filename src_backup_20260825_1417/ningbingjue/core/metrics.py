"""core/metrics.py — 共享指标捕获器（singleton，零 PyQt 依赖）。

为什么需要它
============
负载门控降压、power 卡片的 GPU 浮动锁频、未来的自动锁帧，这三类功能都
需要实时 CPU/GPU 指标。如果每家各起一套采样（各 spawn PowerShell /
typeperf），既浪费资源又互相打架。本模块提供一个**全应用唯一的捕获器**：

  - 后台 1 个合并 PowerShell 流（SharedMonitor 宿主，跑 COMBINED_PS）：
    GPU 占用（\\GPU Engine(*)\\Utilization Percentage 之和）、每核占用
    （\\Processor(*)\\% Processor Time）、ACPI 温度，同一进程每 tick 一行
    前缀行输出（G/C/T）。
  - 1 个前台进程 CPU 占用跟踪线程（GetProcessTimes delta）。
  - 全部写入进程内共享状态；消费者（UndervoltController / gpu_float /
    自动锁帧）只读内存中的 ``snapshot``，**零额外采样成本**。

开销
====
  - 常驻 1 个轻量后台进程（合并后的 PowerShell ~10~20 MB 常驻，CPU 占用
    几乎为 0，只是每秒读一次 PDH 计数器并 printf 三行）。
  - 每个消费者读取 ``snapshot`` 是 O(1) 的内存读，不 spawn 任何进程。
  - 对比旧实现：GPU 浮动锁频原本每 tick 调 ``typeperf -sc 1``（每次
    spawn 一个进程、5~15 ms）；改为共享采样器后反而**省了**这笔开销。
  - PowerShell 启动（~50~100 ms）只在宿主 ``start()`` 时发生一次。
  - 2026-08-19 起由「每信号一个 PowerShell」（2~5 个）合并为「一个宿主」
    （1 个），省内存与进程数；CPU 开销本就近 0，不在优化目标内。

实测环境（掌机）下，整个捕获器对前台游戏的帧时间影响不可测量。

模块只依赖 stdlib + ctypes，可被独立脚本 import（与 game_detect 同约束）。
"""
from __future__ import annotations

import atexit
import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

try:
    from .game_detect import find_foreground_pid
except Exception:  # pragma: no cover - 仅在极恶劣环境下
    find_foreground_pid = None  # type: ignore[assignment]

# 采样统一走合并宿主（core/perf_probe.SharedMonitor），不再各自 spawn。
from .perf_probe import SharedMonitor


# --------------------------------------------------------------------------
# Windows API：按 PID 取进程自身 CPU%（GetProcessTimes delta）
# --------------------------------------------------------------------------
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32),
                ("dwHighDateTime", ctypes.c_uint32)]

    def as_int64(self) -> int:
        return (self.dwHighDateTime << 32) | self.dwLowDateTime


_OpenProcess = _kernel32.OpenProcess
_OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_OpenProcess.restype = wintypes.HANDLE

_CloseHandle = _kernel32.CloseHandle
_CloseHandle.argtypes = [wintypes.HANDLE]
_CloseHandle.restype = wintypes.BOOL

_GetProcessTimes = _kernel32.GetProcessTimes
_GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME),
    ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME),
]
_GetProcessTimes.restype = wintypes.BOOL


# --- Windows Job Object：父进程（含硬崩溃）退出时自动杀掉采样子进程 ---------
# 崩溃安全网已上移到 core/perf_probe.SharedMonitor（合并采样宿主统一挂接）。
# MetricSampler 通过订阅共享宿主采样，自身不再直接 spawn PowerShell；
# 硬崩溃孤儿进程由宿主的「关闭即杀」Job Object 统一兜底。
_METRIC_SAMPLER_ATEXIT_REGISTERED = False


@dataclass
class MetricSnapshot:
    """某一时刻的共享指标快照（只读，消费者持有的是副本）。"""
    max_core: float | None = None     # 最忙核心占用（抓单核瓶颈型，如绝区零）
    mean_core: float | None = None    # 平均核心占用（抓均衡型，如原神）
    gpu_pct: float | None = None      # 系统 GPU 占用（防抖守卫）
    prog_cpu: float | None = None     # 前台进程 CPU 占用（自动锁帧未来用）
    cores: list[float] | None = None  # 全核原始占用（诊断/画图）
    ts: float = 0.0                   # 采样时刻（monotonic）
    ok: bool = False                  # 任一信号成功更新过即为 True


class MetricSampler:
    """全应用唯一的指标捕获器（单例）。

    用法::

        sampler = MetricSampler.instance()
        sampler.start()                 # 通常在 app 启动时调用一次
        ...
        snap = sampler.snapshot         # 任意消费者随时读，O(1)
        if snap.max_core is not None and snap.max_core >= 60: ...
        sampler.stop()                  # app 退出时
    """

    _instance: "MetricSampler | None" = None

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[Any], None]] = []
        self._state: dict[str, Any] = {
            "gpu": None, "cores": None, "prog_cpu": None,
            "ok": False, "ts": 0.0,
        }
        self._running = False
        self._n_cores = os.cpu_count() or 1
        # 前台进程 CPU 跟踪状态
        self._prog_pid: int | None = None
        self._prog_h: int = 0
        self._prog_prev_k: int | None = None
        self._prog_prev_u: int | None = None
        self._prog_prev_t: float = 0.0

    @classmethod
    def instance(cls) -> "MetricSampler":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # -- 快照读取（O(1)，消费者入口）-----------------------------------
    @property
    def snapshot(self) -> MetricSnapshot:
        with self._lock:
            s = self._state
            cores = s["cores"]
            maxc = meanc = None
            if cores:
                maxc = max(cores.values())
                meanc = sum(cores.values()) / len(cores)
            return MetricSnapshot(
                max_core=maxc,
                mean_core=meanc,
                gpu_pct=s["gpu"],
                prog_cpu=s["prog_cpu"],
                cores=list(cores.values()) if cores else None,
                ts=s["ts"],
                ok=s["ok"],
            )

    @property
    def is_running(self) -> bool:
        return self._running

    # -- 采样回调（消费者挂载点）---------------------------------------
    # 消费者（GPU 浮动锁频、未来自动锁帧等）可注册回调，每次采样更新后
    # 在后台读线程里被调用，参数是当次 MetricSnapshot。回调在锁外触发，
    # 且任一回调异常不影响其它回调/采样循环。
    def register_callback(self, cb: "Callable[[MetricSnapshot], None]") -> None:
        with self._lock:
            if cb not in self._callbacks:
                self._callbacks.append(cb)

    def unregister_callback(self, cb: "Callable[[MetricSnapshot], None]") -> None:
        with self._lock:
            if cb in self._callbacks:
                self._callbacks.remove(cb)

    def _emit_sample(self) -> None:
        """触发所有采样回调（在锁外调用，避免回调重入死锁）。"""
        snap = self.snapshot
        for cb in list(self._callbacks):
            try:
                cb(snap)
            except Exception:  # pragma: no cover - 单个消费者异常不应拖垮采样器
                pass

    # -- 生命周期 --------------------------------------------------------
    def start(self) -> None:
        """启动后台采样。幂等（重复调用无副作用）。非 Windows 直接返回。"""
        if self._running:
            return
        if sys.platform != "win32":
            return
        self._running = True
        mon = SharedMonitor.instance()
        mon.subscribe("gpu", self._on_gpu)
        mon.subscribe("cores", self._on_cores)
        t = threading.Thread(target=self._prog_loop, daemon=True)
        t.start()
        self._register_atexit()

    def stop(self) -> None:
        """停止后台采样（app 退出时调用）。"""
        self._running = False
        with self._lock:
            self._callbacks.clear()
        # 注销订阅；若这是最后一个订阅者，SharedMonitor 会自动终止 PowerShell 宿主。
        mon = SharedMonitor.instance()
        mon.unsubscribe("gpu", self._on_gpu)
        mon.unsubscribe("cores", self._on_cores)
        if self._prog_h:
            try:
                _CloseHandle(self._prog_h)
            except OSError:
                pass
            self._prog_h = 0
            self._prog_pid = None

    # -- 采样回调（SharedMonitor reader 线程调用）-------------------------
    def _on_gpu(self, v: float) -> None:
        with self._lock:
            self._state["gpu"] = v
            self._state["ok"] = True
            self._state["ts"] = time.monotonic()
        self._emit_sample()

    def _on_cores(self, d: dict) -> None:
        with self._lock:
            self._state["cores"] = d
            self._state["ok"] = True
            self._state["ts"] = time.monotonic()
        self._emit_sample()

    def _register_atexit(self) -> None:
        """注册 atexit 兜底（仅一次），确保不经过 closeEvent 的退出路径也能终止子进程。"""
        global _METRIC_SAMPLER_ATEXIT_REGISTERED
        if _METRIC_SAMPLER_ATEXIT_REGISTERED:
            return
        _METRIC_SAMPLER_ATEXIT_REGISTERED = True
        try:
            atexit.register(self.stop)
        except Exception:  # noqa: BLE001
            pass

    def _read_proc_times(self, h: int):
        ct, et = _FILETIME(), _FILETIME()
        kt, ut = _FILETIME(), _FILETIME()
        if not _GetProcessTimes(h, ctypes.byref(ct), ctypes.byref(et),
                                ctypes.byref(kt), ctypes.byref(ut)):
            return None
        return kt.as_int64(), ut.as_int64()

    def _prog_loop(self) -> None:
        """跟踪前台进程的 CPU 占用（相对总逻辑核数）。"""
        while self._running:
            try:
                if find_foreground_pid is None:
                    time.sleep(0.5)
                    continue
                pid = find_foreground_pid()
                # 前台进程变了 -> 重新基线（本帧跳过）
                if pid != self._prog_pid:
                    if self._prog_h:
                        _CloseHandle(self._prog_h)
                        self._prog_h = 0
                    self._prog_pid = None
                    if pid:
                        h = _OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
                                         False, pid)
                        if h:
                            base = self._read_proc_times(h)
                            if base is not None:
                                self._prog_pid = pid
                                self._prog_h = h
                                self._prog_prev_k, self._prog_prev_u = base
                                self._prog_prev_t = time.monotonic()
                if self._prog_h and self._prog_pid:
                    times = self._read_proc_times(self._prog_h)
                    if times is None:
                        _CloseHandle(self._prog_h)
                        self._prog_h = 0
                        self._prog_pid = None
                    else:
                        k, u = times
                        now = time.monotonic()
                        dt = now - self._prog_prev_t
                        if dt > 0.05:
                            cpu = ((k - self._prog_prev_k + u - self._prog_prev_u)
                                   / 10_000_000.0 / dt / self._n_cores * 100.0)
                            with self._lock:
                                self._state["prog_cpu"] = cpu
                                self._state["ok"] = True
                                self._state["ts"] = now
                            self._prog_prev_k, self._prog_prev_u, self._prog_prev_t = k, u, now
            except Exception:  # pragma: no cover
                pass
            time.sleep(0.4)
