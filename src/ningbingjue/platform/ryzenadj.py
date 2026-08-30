"""ryzenadj adapter — Ryzen APU power/clock control.

Mirrs the power block: builds ``ryzenadj.exe`` arguments from a ``PowerConfig``
and runs it through a single ``invoke`` wrapper (matching the original
``Invoke-RyzenAdj``).  The original treats exit code ``0xC0000005`` (STATUS_ACCESS_VIOLATION,
unsigned ``3221225477`` / signed ``-1073741819``) as *success* — we
preserve that quirk.  On Windows, Python's ``subprocess`` reports it as the
*unsigned* value ``3221225477``, so the OK set lists both forms.
The exe is copied into ``%LOCALAPPDATA%`` to dodge non-ASCII install paths.

安全模型对齐说明：对 ``ryzenadj.exe``（WinRing0 全局 ring0 驱动）的并发访问，
采用「进程内 threading.Lock + 系统级命名互斥」双层守卫，**对齐掌机底层 EC 的
EcMutex 命名互斥安全模型**——任意时刻全系统只允许一个调用方驱动 WinRing0，
杜绝跨进程抢驱动导致的竞态崩溃（详见下方 ``_SYS_MUTEX`` 注释与 ``_RYZENADJ_CALL_LOCK``）。
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
import sys
import ctypes
from ctypes import wintypes
import threading
import time
import winreg


from .base import ExternalAdapter
from ..core.config import PowerConfig
from ..core.perf_probe import SharedMonitor

# ryzenadj 在 Core Isolation（内存完整性）开启环境下，SMU 频率写入已在 ring0
# 驱动上下文落地，但进程退出 / 驱动卸载时用户态触发访问冲突，退出码即
# 0xC0000005 (STATUS_ACCESS_VIOLATION)。此码下频率实际已改变 = 行为成功。
# 注意：Python subprocess 在 Windows 上把该 NTSTATUS 当作无符号 32 位返回
# （3221225477），而非有符号 -1073741819；为兼容两种表示，两者与 0 一并列入白名单。
RYZENADJ_OK_EXIT_CODES = frozenset([0, 0xC0000005, -1073741819])  # 0xC0000005 == 3221225477

# 统一派发器：--set-coall 的 mV→编码（与 platform.undervolt.COALL_BASE 同源）。
# 负偏移=降压：编码 = COALL_BASE - |mV|（-20 → 1048556，-50 → 1048526，0 → 1048576）。
COALL_BASE = 1048576

# --- 安全层（修复崩溃-重启正反馈，2026-07-26）------------------------------
# 根因：silent（GPU 浮动/保活）路径原用 fire-and-forget ``Popen`` 不 wait，
# ryzenadj 在 Core Isolation 下以 0xC0000005 退出（频率已设但退出崩），崩溃进程
# 变孤儿 + WinRing0 驱动句柄残留；每秒再 spawn 新实例抢同一全局驱动 → 竞态崩溃 →
# 堆积拖垮系统（事件日志实锤：45 秒内 15 次 ryzenadj 崩溃 + Kernel-Power 41 硬重启）。
# 修复：① 全局单实例（同一时刻只允许一个 ryzenadj 进程，WinRing0 是全局 ring0 驱动）；
#       ② silent 改后台线程回收（不阻塞 UI）；③ 熔断（连续真失败即停手，防雪崩）。
RYZENADJ_FAIL_LIMIT = 3          # 连续真失败达到此数即熔断（不再自动调用）
RYZENADJ_SILENT_REAP_TIMEOUT = 10.0  # 后台回收等待上限（秒）
RYZENADJ_BREAKER_COOLDOWN_S = 20.0   # 熔断后自动恢复冷却（秒）：瞬态驱动问题消退后自恢复
_RYZENADJ_CALL_LOCK = threading.Lock()  # 保护单实例标志（进程内）
_RYZENADJ_BUSY = False            # 是否已有 ryzenadj 进程在运行（全局单实例）

# 跨进程系统级命名互斥（对齐 EC 的 EcMutex，2026-07-28 加固）：
# 上面的 ``_RYZENADJ_CALL_LOCK`` 只是【进程内】锁，拦得住同进程多线程，拦不住
# 「两个 凝冰结魄 实例」或「凝冰结魄 + GPD 自带工具 / MotionAssistant / 别的
# ryzenadj 调用方」跨进程并发抢 WinRing0 全局 ring0 驱动——那正是崩溃-重启竞态的
# 跨进程残留风险。故叠加一层系统级命名互斥，让任意时刻全系统只有一个 ryzenadj 在
# 驱动 WinRing0（silent 路径下互斥持有到子进程退出，由 reaper 释放）。
# 降级：非 Windows / 创建失败 → 退化为 no-op，仅保留进程内锁（不致命）。
_RYZENADJ_SYS_MUTEX_NAME = "NingBingJue_RyzenAdj_WinRing0"
_RYZENADJ_SYS_MUTEX_TIMEOUT_MS = 1500  # 争用则最多等 1.5s，仍抢不到→SKIP（不阻塞、不雪崩）


class _NamedSemaphore:
    """跨进程命名信号量（count=1，等价互斥，但**允许任意线程释放**）。

    为什么不用 Mutex（2026-08-21 修复 GPU 浮动「卡上限」真凶）：
    凝冰结魄的 silent（GPU 浮动）路径在 worker 线程 ``try_acquire`` 成功，
    却把释放职责交给后台 reaper 线程（``_reap_silent`` 的 finally）——
    而 Windows 的 ``ReleaseMutex`` **只允许持有线程调用**，跨线程释放必返
    ``ERROR_NOT_OWNER``（288），旧实现把失败静默吞掉 → 互斥被永久「占用」
    （所有者线程还活着）→ 此后所有调用（含手动）``try_acquire`` 全部超时
    SKIP。表现即：**手动 power settings 能改、GPU 浮动永远卡在上限**，重启
    后第一次浮动调用又锁死、依旧卡住（与用户掌机 4 分钟全 SKIP 日志铁证吻合）。

    信号量（Semaphore）没有「所有者线程」概念，``ReleaseSemaphore`` 可被
    任意线程调用——获取/释放跨线程天然安全，语义等价（count=1 即互斥），
    持有进程崩溃时内核对象随进程销毁、自动释放。故用它替换 Mutex 根治。
    变量名 ``_SYS_MUTEX`` 沿用于历史兼容（打桩脚本 sim_gpu_float_mutex 等）。
    ``try_acquire`` 返回 ``'degraded'``（本平台不支持，放行且不持锁）/
    ``'acquired'``（已持锁）/ ``'busy'``（争用超时，调用方应 SKIP）。
    ``release`` 幂等且吞异常。
    """

    def __init__(self, name: str, timeout_ms: int = _RYZENADJ_SYS_MUTEX_TIMEOUT_MS):
        self._name = name
        self._timeout_ms = int(timeout_ms)
        self._kernel32 = None
        self._handle = None
        try:
            k32 = ctypes.windll.kernel32
            # 显式声明 64 位 HANDLE / 类型，避免 ctypes.windll 默认 32 位截断
            k32.CreateSemaphoreW.restype = wintypes.HANDLE
            k32.CreateSemaphoreW.argtypes = [wintypes.LPVOID, wintypes.LONG,
                                             wintypes.LONG, wintypes.LPCWSTR]
            k32.WaitForSingleObject.restype = wintypes.DWORD
            k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            k32.ReleaseSemaphore.restype = wintypes.BOOL
            k32.ReleaseSemaphore.argtypes = [wintypes.HANDLE, wintypes.LONG,
                                             ctypes.POINTER(wintypes.LONG)]
            self._kernel32 = k32
            self._handle = k32.CreateSemaphoreW(None, 1, 1, name)
            if not self._handle:
                self._kernel32 = None
                self._handle = None
        except Exception:  # noqa: BLE001
            self._kernel32 = None
            self._handle = None

    def try_acquire(self) -> str:
        if self._kernel32 is None or not self._handle:
            return "degraded"
        # WAIT_OBJECT_0=0（获占）；信号量无 WAIT_ABANDONED；其余（0x102 超时/
        # 0xFFFFFFFF 失败）一律视为争用 busy，调用方 SKIP 不阻塞不雪崩。
        rc = self._kernel32.WaitForSingleObject(self._handle, self._timeout_ms)
        if rc == 0:
            return "acquired"
        return "busy"

    def release(self) -> None:
        if self._kernel32 is not None and self._handle:
            try:
                # 信号量可被任意线程释放（Mutex 的 ReleaseMutex 仅限持有线程）。
                self._kernel32.ReleaseSemaphore(self._handle, 1, None)
            except Exception:  # noqa: BLE001
                pass


_SYS_MUTEX = _NamedSemaphore(_RYZENADJ_SYS_MUTEX_NAME)


# --- HVCI / 内存完整性（Core Isolation）诊断 -------------------------------
# TDP / GPU 频率控制失效的最常见根因：Windows「内存完整性」(HVCI / Core
# Isolation / 内核隔离) 开启时，会拦截 ryzenadj 依赖的 WinRing0x64.sys ring0
# 驱动加载，导致 ryzenadj 报 "Unable to get os_access Obj"——**即便以管理员
# 身份运行也失败**（管理员只解决「权限」问题，解决不了「驱动被内核拦截」问题）。
# 此函数用注册表快速判定 HVCI 是否开启（无需提权、无需 PowerShell，毫秒级），
# 供失败诊断给出正确指引（而不是误导用户「请以管理员身份运行」）。
def hvci_enabled() -> bool:
    """Windows 内存完整性(HVCI / Core Isolation)是否开启。

    开启时系统会拦截 ryzenadj 的 WinRing0x64.sys ring0 驱动，导致
    'Unable to get os_access'——即便管理员也失败。这是 TDP/GPU 控制失效的
    头号根因。非 Windows / 注册表读取失败均保守返回 False。
    """
    if sys.platform != "win32":
        return False
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios"
            r"\HypervisorEnforcedCodeIntegrity",
        )
        val, _ = winreg.QueryValueEx(key, "Enabled")
        winreg.CloseKey(key)
        return val == 1
    except Exception:
        return False


# --- invoke 日志（诊断用）-------------------------------------------------
# 记录每一次下发给 ryzenadj 的指令 + 返回值，落到项目 logs/ryzenadj_invoke.log，
# 方便在「应用成功但频率不变」这类问题上确认：到底发了什么、ryzenadj 回了什么。
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_MODULE_DIR, "..", "..", ".."))
_INVOKE_LOG = os.path.join(_PROJECT_ROOT, "logs", "ryzenadj_invoke.log")
# GPU 利用率诊断日志：记录每次读取的来源与原始值，定位「util 永远 0」问题。
_GPU_UTIL_LOG = os.path.join(_PROJECT_ROOT, "logs", "gpu_util.log")
_GPU_UTIL_DIAG_TS = 0.0  # 模块级节流时间戳
_GPU_UTIL_HEADER_WRITTEN = False  # 是否已写过「新代码生效」头部（只写一次）


def _log_gpu_util(source: str, value: float, extra: str = "", force: bool = False) -> None:
    """把 GPU 利用率读取的来源/原始值写进 logs/gpu_util.log（节流：约每 15s 一条）。

    用于诊断「自动 GPU 永远看到 util=0」：从日志能看出到底是
    (a) 采样器没就绪 → source=sampler_missing，需看 typeperf 回退值；
    (b) GPU 真空闲 → source=sampler，value=0.0（这是正常的）；
    (c) 采样器与 typeperf 都读不到 → value 恒为 0，可能是驱动/PDH 计数器问题。

    ``force=True`` 用于「新代码生效」头部与关键事件，绕过 15s 节流确保一定落盘。
    关键不变量：只要本函数被调用过一次，gpu_util.log 就**绝不可能是空的**——
    因此掌机上若该日志完全空 == 旧 ryzenadj.py 在跑（见 read_gpu_utilization 头部）。
    """
    global _GPU_UTIL_DIAG_TS
    try:
        now = time.time()
        if not force and now - _GPU_UTIL_DIAG_TS < 15.0:
            return
        # 只有非强制写入才推进节流时间戳；HEADER/monitor_started 等 force 行
        # 不该吞掉紧随其后的首条 direct_monitor 诊断。
        if not force:
            _GPU_UTIL_DIAG_TS = now
        os.makedirs(os.path.dirname(_GPU_UTIL_LOG), exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] source={source} value={value:.3f}"
        if extra:
            line += f" | {extra}"
        with open(_GPU_UTIL_LOG, "a", encoding="utf-8", errors="ignore") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def _log_invoke(cmd_args: list[str], silent: bool, ok, code, out: str,
                tag: str = "RYZENADJ") -> None:
    """追加一条 ryzenadj 调用记录（exe 路径 + 命令 + 返回值）。失败静默忽略。"""
    try:
        os.makedirs(os.path.dirname(_INVOKE_LOG), exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        lines = [f"[{ts}] {tag}"]

        # 如果第一个参数是 exe 路径，拆成 exe + args 两行显示，更清楚
        if cmd_args and (cmd_args[0].endswith(".exe") or os.path.isabs(cmd_args[0])):
            exe = cmd_args[0]
            args = cmd_args[1:]
            lines.append(f"    exe: {exe}")
            lines.append(f"    cmd: {' '.join(args) if args else '(none)'}")
        else:
            lines.append(f"    cmd: {' '.join(cmd_args)}")

        lines.append(f"    rc:  {code}")
        lines.append(f"    ok:  {ok}")
        if silent:
            lines.append("    mode: fire-and-forget")

        if out:
            trimmed = out.replace("\r\n", "\n").strip()
            if len(trimmed) > 2000:
                trimmed = trimmed[:2000] + "...[truncated]"
            for ln in trimmed.split("\n"):
                if ln.strip():
                    lines.append(f"    out: {ln.strip()}")
        else:
            lines.append("    out: (none)")
        lines.append("-" * 64)

        with open(_INVOKE_LOG, "a", encoding="utf-8", errors="ignore") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception:  # noqa: BLE001
        pass


# --- 自持 GPU 利用率监控（订阅合并采样宿主）---------------------------------
# 关键修复：自动 GPU 之前依赖 core.metrics.MetricSampler 共享采样器的
# snapshot.gpu_pct；但 MetricSampler 有「GPU reader + 每核 reader」两路，只要每核
# reader 活着，回调就一直在跑（表现为日志里 TICK 不断），可一旦 GPU reader 悄无声息
# 地挂了，gpu_pct 永远 None，read_gpu_utilization 就回退到 typeperf 单帧采样——而
# 单帧采样在空闲/瞬时下几乎恒为 0，于是自动 GPU 永远看到 util=0、一路压到地板。
# 这里让 RyzenAdj 自己持有一个直连的 GPU 监控，彻底绕开「共享采样器某一 reader 挂掉
# 拖垮全部消费者」的脆弱性。
# 2026-08-19：GPU 监控改为订阅 core.perf_probe.SharedMonitor 合并采样宿主（1 个
# PowerShell 同时输出 GPU/每核/温度，G 前缀行），不再自行 spawn。崩溃安全网
# （Job Object / atexit / EOF 自动重启）由宿主统一负责，本类只消费数值。


class _GpuUtilMonitor:
    """后台 GPU 利用率监控（单例）。

    2026-08-19 起订阅 ``core.perf_probe.SharedMonitor`` 合并采样宿主的 gpu
    通道（1 个 PowerShell 同时输出 GPU/每核/温度，G 前缀行），不再自行 spawn。
    崩溃安全网（Job Object / atexit / EOF 自动重启）由宿主统一负责，本类只
    消费数值。对外 API 不变：``ensure_started`` / ``read`` / ``warmed`` /
    ``stop``。
    """

    _instance: "_GpuUtilMonitor | None" = None

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0.0
        self._ok = False
        self._running = False

    @classmethod
    def instance(cls) -> "_GpuUtilMonitor":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def ensure_started(self) -> None:
        if self._running or sys.platform != "win32":
            return
        try:
            SharedMonitor.instance().subscribe("gpu", self._on_gpu)
            self._running = True
            _log_gpu_util("monitor_started", 0.0,
                          "GPU 直连监控已切换至合并采样宿主（SharedMonitor）",
                          force=True)
        except OSError as e:  # 订阅/宿主拉起失败（罕见）→ 记一条，证明监控没跑
            self._running = False
            _log_gpu_util("monitor_spawn_failed", 0.0,
                          f"err={e!r}（GPU 监控订阅失败，util 将恒为 0）",
                          force=True)

    def _on_gpu(self, v: float) -> None:
        """宿主 reader 线程回调：最新 GPU 占用（0-100）。"""
        with self._lock:
            self._value = float(v)
            self._ok = True

    def read(self) -> float:
        self.ensure_started()
        with self._lock:
            return self._value if self._ok else 0.0

    @property
    def warmed(self) -> bool:
        """是否已拿到过至少一次有效读数（预热完成）。"""
        with self._lock:
            return self._ok

    def stop(self) -> None:
        """注销订阅；若这是最后一个订阅者，SharedMonitor 自动终止宿主进程。

        硬崩溃场景由宿主内部 Job Object 兜底（见 core/perf_probe.py），
        本类不再持有进程句柄。
        """
        self._running = False
        try:
            SharedMonitor.instance().unsubscribe("gpu", self._on_gpu)
        except Exception:  # noqa: BLE001
            pass


def _invoke_ok(out: "str | None", code: "int | None") -> bool:
    """统一的「调用是否成功」判定：silent / 非 silent 两条路径共用。

    关键：ryzenadj 在某些机器上「频率已设但退出码非 0」（例如 HX370 上
    ``--gfx-clk`` 设成功却返回非 0 退出码），此时必须靠回执文本
    (``Successfully`` / ``set gfx``) 判定成功，否则**只查退出码**会让 silent
    （GPU 浮动）路径把成功的调用误判为失败 → 连续失败触发熔断 → 浮动彻底不
    下发，硬件频率卡在最后那次成功值（典型：软件降到 400 但硬件还是 900）。
    手动路径原本就带文本判定，故浮动/手动表现不一致。此处让两条路径完全一致。
    """
    out_l = (out or "").lower()
    rejected = (
        ("usage:" in out_l)
        or ("unknown argument" in out_l)
        or ("invalid argument" in out_l)   # P2-6：部分版本/固件用此措辞拒绝
        or ("unable to init" in out_l)
        or ("unable to get os_access" in out_l)
    )
    # P2-6 加固：成功文本白名单补 "gfx clock"（部分版本输出 "gfx clock set to X"
    # 而无 "Successfully"/"set gfx"）。rejected 优先拦截，防止误认失败为成功。
    return (not rejected) and (
        ("successfully" in out_l)
        or ("set gfx" in out_l)
        or ("gfx clock" in out_l)
        or (code in RYZENADJ_OK_EXIT_CODES)
    )


class RyzenAdj(ExternalAdapter):
    def __init__(self, exe_path: str) -> None:
        self.exe_path = exe_path
        # 熔断 / 单实例状态（修复崩溃-重启正反馈）
        self._consecutive_fails = 0
        self._circuit_open = False
        self.on_circuit_breaker = None  # 可选回调：熔断开启时通知 UI
        # 可选回调：silent（GPU 浮动）下发被后台 reaper 判定为成功/失败后触发，
        # 入参为「硬件已确认的 gfx_clk」。power 块用它把 gpu_current_freq（显示 +
        # 算法 current）校正到真实硬件状态，消除「显示/算法以为已设、硬件却没收到」
        # 的发散（这是「GPU 浮动有时控不住、显示与实际频率不符」的根因）。
        self.on_silent_sent = None
        self._resolved_exe_cache = None  # R7：_resolve_exe 整目录复制结果缓存
        # 最近一次 invoke 被 SKIP 的原因（""=无；busy/busy-cross-process/circuit-open）。
        # power 块据此把 busy 告警文案细分（另一实例 vs 同进程繁忙，P2-5，2026-08-20）。
        self._last_skip_reason = ""
        # 最近一次 silent reaper 因超时 kill 子进程的时刻（monotonic；0=从未）。
        # 若 busy 告警时刚发生过 reaper 超时，说明是「自身调用挂起」而非「另一实例占用」。
        self._last_reap_timeout_mono = 0.0
        # 最近一次 ryzenadj 失败的根因诊断（空串=无/未知）。供 UI 给出精确指引，
        # 替代过去一刀切的「请以管理员身份运行」（管理员救不了被 HVCI 拦截的驱动）。
        self._last_diag = ""
        # 统一派发器：期望状态（三路共享同一个 RyzenAdj 实例，故能合并下发）。
        # None = 不管理该项（不 emit 对应 flag）；coall_mv=0 是「显式归零」（游戏外）。
        self._desired = {"gfx_clk": None, "coall_mv": None, "tdp_mw": None}
        self._applied = dict(self._desired)
        self._state_lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self.exe_path) and os.path.exists(self.exe_path)

    @staticmethod
    def build_args(
        config: PowerConfig,
        gpu_freq: int | None = None,
    ) -> list[str]:
        """Build CLI args exactly like the power block's ``Confirm-PowerSettings``.

        TDP (locked):  --stapm-limit <W*1000> --fast-limit <W*1000+1000>
                         --slow-limit <W*1000> --apu-slow-limit <W*1000> --slow-time 1
        GPU  (locked):  --gfx-clk <freq>

        ⚠️ 用户实测：在 HX370 (Strix Point) 上，**``--gfx-clk`` 才是真正能控制 GPU
        频率的 flag**——手动锁频与 GPU 浮动都走它，硬件频率会跟着变。之前误改成
        ``--max-gfxclk`` / ``--min-gfxclk`` 反而让 GPU 频率彻底失控（两条路径都失效，
        手动设了也没反应）。``--apu-sku 0`` 也不要加（本版 ryzenadj 不认，会被
        argparse 拒绝回打 Usage，整条命令失效）。
        """
        args: list[str] = []
        if config.tdp_lock:
            mw = config.tdp * 1000
            args += [
                "--stapm-limit", str(mw),
                "--fast-limit", str(mw + 1000),
                "--slow-limit", str(mw),
                "--apu-slow-limit", str(mw),
                "--slow-time", "1",
            ]
        if config.gpu_lock or config.gpu_float_enabled:
            gfx = gpu_freq if gpu_freq is not None else config.gpu
            # 用户实测：--gfx-clk 在 HX370 上有效（手动锁频 / 浮动都走它）。
            # 之前误用 --max-gfxclk/--min-gfxclk 反而让两条路径都失效。
            args += ["--gfx-clk", str(gfx)]
        return args

    @staticmethod
    def _appdata_copy_complete(dst_exe: str) -> bool:
        """校验 AppData 副本是否完整（ryzenadj.exe + WinRing0x64.sys/.dll 都在）。

        WinRing0x64.sys 是 ryzenadj 的 ring0 驱动，缺失/被杀软 quarantine 后
        ryzenadj 会静默报 os_access 失败。校验不通过就宁可回退到源码目录，
        避免一直用残破副本。
        """
        if not dst_exe or not os.path.exists(dst_exe):
            return False
        d = os.path.dirname(dst_exe)
        return (os.path.exists(os.path.join(d, "WinRing0x64.sys"))
                and os.path.exists(os.path.join(d, "WinRing0x64.dll")))

    def _resolve_exe(self) -> str:
        # R7：整目录复制代价高（含多个 DLL / 驱动），加缓存避免每次 invoke
        # 都重复制到 %LOCALAPPDATA%（脏检查已把调用频率从 1Hz 降到偶尔，但
        # 冷启动 / 多次 Apply 仍会反复触发）。仅在源目录 mtime 变化时才重复制。
        cache = getattr(self, "_resolved_exe_cache", None)
        if cache is not None:
            dst_exe, src_dir, src_mtime = cache
            # 防御：缓存的 AppData 副本可能不完整（驱动被 quarantine / 复制中途
            # 失败）→ 一旦不完整立即作废，回退源码目录，避免静默失效。
            if self._appdata_copy_complete(dst_exe):
                try:
                    if os.path.getmtime(src_dir) == src_mtime:
                        return dst_exe
                except Exception:
                    pass
            else:
                self._resolved_exe_cache = None
        # 当安装路径含非 ASCII 字符时，把整个 ryzenadj 目录复制到
        # %LOCALAPPDATA%，避免中文路径导致驱动加载失败。
        # ⚠️ 必须复制「整个目录」（含 inpoutx64.dll / libryzenadj.dll /
        # WinRing0x64.dll|sys 等依赖）——只复制 ryzenadj.exe 单文件会因
        # 缺 DLL 直接 0xC0000135 (STATUS_DLL_NOT_FOUND) 崩溃，导致所有
        # GPU 频率控制（静态锁频 / 浮动 / 保活）全部静默失效。
        try:
            self.exe_path.encode("ascii")
            self._resolved_exe_cache = None  # ascii 路径无需复制
            return self.exe_path
        except UnicodeEncodeError:
            local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
            src_dir = os.path.dirname(self.exe_path)
            dst_dir = os.path.join(local, "ryzenadj")
            dst_exe = os.path.join(dst_dir, "ryzenadj.exe")
            try:
                os.makedirs(dst_dir, exist_ok=True)
                # 同步整个源目录（保留所有配套 DLL / 驱动），而非仅 exe。
                for name in os.listdir(src_dir):
                    src_file = os.path.join(src_dir, name)
                    if os.path.isfile(src_file):
                        shutil.copy2(src_file, os.path.join(dst_dir, name))
                # 记录源目录 mtime，供下次跳过（源未更新则不再复制）
                try:
                    src_mtime = os.path.getmtime(src_dir)
                except Exception:
                    src_mtime = -1
                self._resolved_exe_cache = (dst_exe, src_dir, src_mtime)
                # 复制后二次校验：不完整则作废，回退源码目录（宁可中文路径报错，
                # 也比用残破副本静默失效更容易诊断）。
                if not self._appdata_copy_complete(dst_exe):
                    self._resolved_exe_cache = None
                    return self.exe_path
                return dst_exe
            except Exception:
                self._resolved_exe_cache = None
                return self.exe_path

    def invoke(
        self,
        args: list[str],
        silent: bool = False,
        timeout: float = 5.0,
        _prev_applied: "dict | None" = None,
        _applied_snapshot: "dict | None" = None,
    ) -> tuple[bool, int | None, str]:
        """Run ryzenadj with ``args``. Returns (success, exit_code, output).

        **安全层（修复崩溃-重启正反馈，2026-07-26）**：
        - 全局单实例（进程内 ``_RYZENADJ_CALL_LOCK`` + ``_RYZENADJ_BUSY``）：WinRing0 是
          全局 ring0 驱动，绝不允许两个 ryzenadj 并发跑（会抢驱动设备句柄→竞态崩溃）。
        - 跨进程系统级命名互斥（``_SYS_MUTEX``，对标 EC 的 EcMutex）：``_RYZENADJ_CALL_LOCK``
          只拦同进程多线程；命名互斥再拦「别的 凝冰结魄 实例 / 别的 ryzenadj 工具」跨进程抢
          WinRing0。争用则 SKIP（不阻塞、不雪崩）；silent 路径互斥持有到子进程退出才释放。
        - ``silent``（GPU 浮动/保活）改为**后台线程回收**：Popen 后立刻返回，
          由 daemon 线程 ``wait()`` 回收退出码，杜绝孤儿进程堆积（原 fire-and-forget
          不 wait，崩溃进程残留 + 每秒再 spawn → 驱动句柄越堆越多 → 系统硬重启）。
        - **熔断（circuit breaker）**：silent 调用连续 ``RYZENADJ_FAIL_LIMIT`` 次
          *真失败*（非 ``0xC0000005``，后者在 Core Isolation 下是「频率已设但退出崩」
          = 成功）即停止自动调用并告警，避免无限重试拖垮系统。
        - 非 silent（apply/手动）走 ``subprocess.run`` 同步等待（用户点击频率低，
          可接受阻塞），同样受单实例锁保护。

        成功判定与旧逻辑完全一致（``successfully``/``set gfx``/OK 退出码），
        且 ``0xC0000005`` 仍算成功 —— 故 Core Isolation 下 GPU 浮动锁频照常生效，
        只是不再把崩溃进程堆成系统崩溃。
        """
        global _RYZENADJ_BUSY
        exe = self._resolve_exe()
        # 入口即记一条（无论 available 与否），含 resolved exe 路径 ——
        # 这样日志空不空 = 新代码有没有真正被加载/调用，一眼判定。
        _log_invoke(
            ["<ENTRY>", f"available={self.available}", f"exe={exe}", *args],
            silent, None, None, "",
        )
        # 每次 invoke 开始时清 SKIP 原因，由本次调用的各 SKIP 分支重新设置
        # （power 块 busy 告警据此细分来源，P2-5）。
        self._last_skip_reason = ""
        if not self.available:
            _log_invoke([exe, *args], silent, False, None,
                        "SKIP: ryzenadj 不可用（exe 路径不存在？）")
            return (False, None, "")

        # 跨进程系统级命名互斥（对齐 EC 的 EcMutex）：先抢系统级锁，再抢进程内锁。
        # 只拦「别的实例正在驱动 WinRing0」这一系统级争用；degraded（本平台不支持）
        # 则放行、不持锁（仅进程内锁仍生效，不致命）。
        mx_state = _SYS_MUTEX.try_acquire()
        if mx_state == "busy":
            self._last_skip_reason = "busy-cross-process"
            _log_invoke([exe, *args], silent, False, None,
                        "SKIP: 跨进程互斥被占用（别的实例/工具正在驱动 WinRing0）")
            return (False, None, "busy-cross-process")
        mx_needs_release = (mx_state == "acquired")
        mx_held_by_reaper = False
        try:
            # 全局单实例（进程内）：抢到锁才能进入；若已有实例在跑则直接跳过。
            with _RYZENADJ_CALL_LOCK:
                if _RYZENADJ_BUSY:
                    self._last_skip_reason = "busy"
                    _log_invoke([exe, *args], silent, False, None,
                                "SKIP: 已有 ryzenadj 实例在运行（防并发抢驱动）")
                    return (False, None, "busy")
                if silent and self._circuit_open:
                    self._last_skip_reason = "circuit-open"
                    _log_invoke([exe, *args], True, False, None,
                                "SKIP: 熔断已开启（连续失败，停止自动调用）")
                    return (False, None, "circuit-open")
                _RYZENADJ_BUSY = True

            if silent:
                try:
                    proc = subprocess.Popen(
                        [exe, *args],  # noqa: S603
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, encoding="utf-8", errors="ignore",
                        creationflags=subprocess.CREATE_NO_WINDOW)
                except Exception:
                    with _RYZENADJ_CALL_LOCK:
                        _RYZENADJ_BUSY = False
                    if mx_needs_release:
                        _SYS_MUTEX.release()
                    _log_invoke([exe, *args], True, False, None, "Popen failed")
                    return (False, None, "")
                # 子进程生命周期内持续持有跨进程互斥；由 reaper 在子进程退出后释放。
                mx_held_by_reaper = True
                threading.Thread(target=self._reap_silent,
                                 args=(proc, args, mx_needs_release, _prev_applied,
                                       _applied_snapshot),
                                 daemon=True).start()
                _log_invoke([exe, *args], True, None, None, "fire-and-forget(回收中)")
                return (True, None, "")

            # 非 silent：同步等待（用户主动调用，频率极低）。
            try:
                proc = subprocess.run(
                    [exe, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="ignore",
                    timeout=timeout, check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                out = (proc.stdout or "") + (proc.stderr or "")
                code = proc.returncode
                # 成功判定与 silent 路径共用同一把尺（_invoke_ok）：既查退出码，
                # 也认 "Successfully"/"set gfx" 真实回执，避免「设成功但退出码非 0」
                # 被误判失败（特别是 HX370 上 --gfx-clk 的情况）。
                ok = _invoke_ok(out, code)
                if ok:
                    self._consecutive_fails = 0
                # 记录失败根因诊断：os_access/init 失败在 HVCI 开启时是驱动被内核
                # 拦截（管理员也无效），必须给出正确指引而非误导「请以管理员运行」。
                self._last_diag = self._build_diag(out)
                _log_invoke([exe, *args], False, ok, code, out)
                return (ok, code, out)
            except subprocess.TimeoutExpired:
                try:
                    subprocess.run(["taskkill", "/IM", "ryzenadj.exe", "/F"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  check=False,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
                except Exception:
                    pass
                _log_invoke([exe, *args], False, False, None, "timeout")
                return (False, None, "timeout")
        finally:
            # 仅 non-silent / 早退路径在此释放跨进程互斥 + 复位 BUSY；
            # silent 成功路径已交给 reaper（mx_held_by_reaper=True）本 finally 跳过。
            if mx_needs_release and not mx_held_by_reaper:
                with _RYZENADJ_CALL_LOCK:
                    _RYZENADJ_BUSY = False
                _SYS_MUTEX.release()

    # --- 熔断 / 回收（修复崩溃-重启正反馈）----------------------------------
    def _reap_silent(self, proc: "subprocess.Popen", args: list[str],
                     mx_needs_release: bool = False,
                     prev_applied: "dict | None" = None,
                     snapshot: "dict | None" = None) -> None:
        """后台回收 silent 调用的退出码与回执，并按统一 OK 语义更新熔断计数。

        ⚠️ 关键：与手动（非 silent）路径共用 ``_invoke_ok`` —— 既查退出码，也认
        "Successfully"/"set gfx" 真实回执。否则「频率已设但退出码非 0」会被误判为
        失败（HX370 上 ``--gfx-clk`` 正是如此），连续 3 次误判即熔断 → GPU 浮动
        彻底不下发，硬件卡在最后那次成功值。Core Isolation 下退出码 ``0xC0000005``
        属 ``RYZENADJ_OK_EXIT_CODES`` → 判成功、清零失败计数（功能正常、不熔断）。
        只有 *真失败*（usage/unknown argument/os_access、等不到=code None、
        DLL 缺失 ``0xC0000135`` 等）才累计，连续 ``RYZENADJ_FAIL_LIMIT`` 次即熔断。
        """
        global _RYZENADJ_BUSY
        try:
            out, err = proc.communicate(timeout=RYZENADJ_SILENT_REAP_TIMEOUT)
        except Exception:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            out = ""
            err = ""
            code = None  # 等不到 = 视为失败
            # P2-5：记录 reaper 超时时刻，供 power 块的 busy 告警区分「自身挂起」
            # 与「另一实例占用」（互斥被自己挂起的子进程占住时，tick 也会 busy）。
            self._last_reap_timeout_mono = time.monotonic()
        else:
            code = proc.returncode
        finally:
            with _RYZENADJ_CALL_LOCK:
                _RYZENADJ_BUSY = False
            # 跨进程互斥持有到子进程退出才释放，覆盖 ryzenadj.exe 整个 WinRing0 生命周期。
            if mx_needs_release:
                _SYS_MUTEX.release()
        # 关键修复（2026-08-20）：合并 stdout+stderr 再判成功，与手动(非 silent)
        # 路径（apply_power 用 ``out = proc.stdout + proc.stderr``）完全一致。
        # 否则某些机器/固件下 ryzenadj 把成功回执打到 **STDERR**（stdout 为空、且
        # 退出码非零、不在 OK 集合），reaper 只看 stdout 会误判「失败」→ 回滚
        # ``_applied`` 到旧天花板 + ``on_silent_sent`` 把显示拉回旧值，表现即
        # 「GPU 浮动卡在最高上限不降」。手动路径正是 stdout+stderr 合并判定，所以
        # 手动能成功、浮动却失效——根因即此路径不对称。统一后两套判定一致：
        # 手动能成功的，浮动 reaper 同样判成功、不再误熔断、显示贴合真实硬件。
        ok = _invoke_ok((out or "") + (err or ""), code)
        if ok:
            self._consecutive_fails = 0
        else:
            self._note_failure(args, code, out)
        # 计算「本次下发最终确认的 gfx」并回调上层（power 块据此把显示/算法 current
        # 校正到真实硬件状态）。⚠️ 在锁内按【本次结果】确定性取数，而不是事后竞态
        # 读 self._applied——若期间已有更新的下发覆盖了 _applied，旧代码会把别人的
        # 值当本次结果回调，显示被错误拉回（2026-08-20 修复）。
        with self._state_lock:
            rolled_back = False
            if not ok:
                # 回滚 applied：本次浮动下发被判定失败（HX370 上 --gfx-clk 常返非 0
                # 退出码，靠 _invoke_ok 文本判定才识破「其实成功了」）。若不清掉乐观
                # 更新的 applied，下一次浮动决策会因 desired==applied 被脏检查拦截 →
                # 硬件永远收不到新频率（软件变了、硬件不变）。回滚后下一 tick 会重新
                # 下发，直到真正成功或熔断。prev_applied 为 None（非浮动来源）则不回滚。
                # 关键竞态修复（2026-08-19）：仅当 applied 仍是「本次 invoke 写下的乐观
                # 快照」时才回滚。若期间有更新的下发推进了 applied（例如本拍 -200 失败、
                # 下一拍 -100 已成功写入），无条件回滚会把旧天花板值覆盖回 applied，再经
                # on_silent_sent 回调把 gpu_current_freq 错误地拉回天花板 → 表现即
                # 「GPU 浮动卡在最高点上限」。snapshot 为 None（旧调用方）保持原行为。
                if prev_applied is not None:
                    if snapshot is None or self._applied == snapshot:
                        self._applied = dict(prev_applied)
                        rolled_back = True
            if ok:
                # 本次下发成功：硬件已到本次目标频率（snapshot=本次 desired 快照）。
                confirmed = (snapshot or {}).get("gfx_clk")
            elif rolled_back:
                # 失败且已回滚：硬件仍是上一拍已生效频率（applied 已回滚到 prev_applied）。
                confirmed = self._applied.get("gfx_clk")
            else:
                # 失败但回滚被跳过：applied 已被更新的下发接管，本次结果不干预显示
                # （由更新下发的 reaper 负责校正），避免把显示错误拉回旧值。
                confirmed = None
        # 通知上层（power 块）GPU 浮动最新「硬件已确认频率」，用于把显示与算法
        # current 校正到真实硬件状态：成功 → 确认值=本次目标；失败（已回滚）→
        # 确认值=上一拍已生效频率。这样「凝冰结魄显示的 GPU 浮动频率」始终贴合
        # 实际硬件，而不会停在乐观目标值（否则会出现显示 600MHz、实际仍 400MHz 的
        # 发散，且算法误以为已生效而停止重试 → 表现成「GPU 浮动有时控不住」）。
        cb = getattr(self, "on_silent_sent", None)
        if callable(cb):
            try:
                cb(confirmed)
            except Exception:  # noqa: BLE001
                pass

    def _note_failure(self, args: list[str], code: "int | None",
                      out: str = "") -> None:
        self._consecutive_fails += 1
        tail = ""
        if out:
            tail = " | out=" + (out or "").strip()[-200:]
        _log_invoke(
            [self._resolve_exe(), *args], True, False, code,
            f"silent 调用失败(code={code})，连续失败 "
            f"{self._consecutive_fails}/{RYZENADJ_FAIL_LIMIT}{tail}",
        )
        if self._consecutive_fails >= RYZENADJ_FAIL_LIMIT and not self._circuit_open:
            self._circuit_open = True
            _log_invoke(
                [self._resolve_exe(), *args], True, False, code,
                f"⚠ 熔断开启：ryzenadj 连续 {RYZENADJ_FAIL_LIMIT} 次失败，"
                f"停止自动调用以防系统崩溃（手动点 Apply 仍可强制下发）",
            )
            cb = getattr(self, "on_circuit_breaker", None)
            if callable(cb):
                try:
                    cb()
                except Exception:  # noqa: BLE001
                    pass
            # 自动恢复：熔断开启后等冷却期自动 reset_circuit，让 GPU 浮动在瞬态驱动
            # 问题（争用/超时）消退后自行恢复，而不是必须用户手动开关才复活（此前
            # 熔断后表现为「有时控不住」的永久哑火）。保守：仅重置熔断计数，不立即
            # 重发；若仍失败会再熔断、再冷却，形成有界重试，不会雪崩。
            self._schedule_breaker_reset()

    @property
    def circuit_open(self) -> bool:
        """是否已因连续失败而熔断（自动调用被暂停）。"""
        return self._circuit_open

    def reset_circuit(self) -> None:
        """手动恢复熔断（如下次启动 / 用户确认环境已正常 / 重新开开关）。"""
        self._circuit_open = False
        self._consecutive_fails = 0

    def _schedule_breaker_reset(self) -> None:
        """熔断开启后，启动一个守护定时器在冷却期后自动 reset_circuit。

        守护线程不阻塞进程退出；reset_circuit 仅重置计数/熔断标志，对已退出进程
        安全。让 GPU 浮动在瞬态问题（驱动争用/超时）自愈，而不是永久失效——这是
        「GPU 浮动有时控不住」在熔断场景下的重要修复。
        """
        try:
            t = threading.Timer(RYZENADJ_BREAKER_COOLDOWN_S, self.reset_circuit)
            t.daemon = True
            t.start()
        except Exception:  # noqa: BLE001
            pass

    # --- 统一派发器（desired-state dispatcher，2026-07-26 治本）-----------
    # 把「GPU 锁频 / CPU 降压 / TDP 锁」三路原本各自独立 invoke ryzenadj 的调用，
    # 收敛成「每 tick 只比较 desired vs applied，仅在【组合状态真正变化】时合并成
    # 一次 ryzenadj 调用」。直接消除：
    #   ① 跨线程并发：后台采样线程的 GPU 浮动 + 主线程的降压 tick 各自 invoke →
    #      两进程并发抢 WinRing0 全局 ring0 驱动 → 内核失稳崩。现在所有写入都经
    #      同一把 _RYZENADJ_CALL_LOCK（invoke 内），天然串行化。
    #   ② 高频 churn：GPU 浮动每 5s 保活重发 + 每 1s 浮动调 → 驱动反复加载/卸载
    #      到内核失稳硬重启。改为仅在值变化时下发，且 GPU 浮动不再做保活重发
    #      （ryzenadj/SMU 自身会保留设定值，无需周期重发）。
    # 设计要点：
    #   - desired 三字段：gfx_clk(int|None) / coall_mv(int|None) / tdp_mw(int|None)。
    #   - maybe_apply：desired==applied → 幂等返回 True（无需操作）；否则合并 emit
    #     一次 invoke；busy（锁占用，瞬态）回滚 applied 让下次重试；circuit-open
    #     不回滚（避免熔断期重试风暴）。所有写入共享 invoke 内的全局锁，跨线程安全。
    def set_desired_gpu_clk(self, freq: "int | None") -> None:
        with self._state_lock:
            self._desired["gfx_clk"] = None if freq is None else int(freq)

    def set_desired_coall_mv(self, mv: "int | None") -> None:
        with self._state_lock:
            self._desired["coall_mv"] = None if mv is None else int(mv)

    def set_desired_tdp_mw(self, mw: "int | None") -> None:
        with self._state_lock:
            self._desired["tdp_mw"] = None if mw is None else int(mw)

    def build_desired_args(self) -> list[str]:
        """根据当前 desired 合并生成 ryzenadj 参数（只 emit 被管理的字段）。

        顺序刻意与旧 ``build_args`` 一致（TDP → gfx-clk → coall），保证单测断言稳定。
        """
        args: list[str] = []
        tdp_mw = self._desired["tdp_mw"]
        if tdp_mw is not None:
            mw = int(tdp_mw)
            args += ["--stapm-limit", str(mw), "--fast-limit", str(mw + 1000),
                     "--slow-limit", str(mw), "--apu-slow-limit", str(mw),
                     "--slow-time", "1"]
        gfx = self._desired["gfx_clk"]
        if gfx is not None:
            # 用户实测：--gfx-clk 在 HX370 上有效。
            args += ["--gfx-clk", str(int(gfx))]
        coall = self._desired["coall_mv"]
        if coall is not None:
            args += ["--set-coall", str(COALL_BASE - abs(int(coall)))]
        return args

    def maybe_apply(self, silent: bool = True, force: bool = False) -> bool:
        """仅在组合 desired 相对 applied 真正变化时，合并下发一次 ryzenadj。

        返回 True=已最新/已应用/无需操作（幂等成功）；False=invoke 实际失败或 busy。
        所有写入共享 invoke 内的全局单实例锁，跨线程（后台采样线程 / 主线程）安全。

        ``force=True``：跳过 ``desired==applied`` 脏检查，强制重新合并下发一次。
        供 GPU 浮动驱动使用——浮动每拍都要把硬件推到「计算出的目标频率」，而
        ``applied`` 是 maybe_apply 在 invoke 真正完成前就乐观写下的快照；若不强制，
        下一拍若目标频率恰好等于这个乐观快照，脏检查会直接 return，导致 ryzenadj
        永远收不到新频率（表现为「浮动失效但手动正常」）。强制下发幂等，硬件已在
        目标值则无副作用。
        """
        with self._state_lock:
            if not force and self._desired == self._applied:
                return True
            args = self.build_desired_args()
            if not args:
                self._applied = dict(self._desired)
                return True
            prev_applied = dict(self._applied)
            snapshot = dict(self._desired)
            self._applied = dict(self._desired)
        ok, _code, out = self.invoke(args, silent=silent, _prev_applied=prev_applied,
                                     _applied_snapshot=snapshot)
        # 失败回滚：保证 applied 始终反映真实硬件状态，避免「乐观快照」把后续
        # dirty-check 误挡（手动设置后无法再改 / 浮动被吞）。
        #   - silent 路径：invoke 立即返回 True（fire-and-forget），真实成败在
        #     _reap_silent 后台线程按 _invoke_ok 判定；失败时它已用 prev_applied
        #     回滚，此处无需处理。但 busy（锁被占用、未 spawn reaper）需在此回滚。
        #   - 非 silent 路径：invoke 同步返回真实成败；busy 瞬态或真实失败都回滚
        #     applied，仅当期间 desired 未再变化时才回滚（让下一 tick 重试）。
        if out in ("busy", "busy-cross-process", "circuit-open") or (not silent and not ok):
            with self._state_lock:
                if self._desired == snapshot:
                    self._applied = prev_applied
        return ok

    def health_check(self, timeout: float = 10.0) -> bool:
        """``Test-RyzenAdjHealth -i`` — self-check privileges/driver.

        受全局单实例锁保护，避免与健康自检并发跑另一个 ryzenadj。
        """
        global _RYZENADJ_BUSY
        if not self.available:
            return False
        with _RYZENADJ_CALL_LOCK:
            if _RYZENADJ_BUSY:
                return False
            _RYZENADJ_BUSY = True
        try:
            proc = subprocess.run(
                [self._resolve_exe(), "-i"], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="ignore", timeout=timeout, check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            ok = proc.returncode in RYZENADJ_OK_EXIT_CODES
            if not ok:
                self._last_diag = self._build_diag(
                    (proc.stdout or "") + (proc.stderr or ""))
            _log_invoke([self._resolve_exe(), "-i"], False, ok,
                        proc.returncode, (proc.stdout or "") + (proc.stderr or ""))
            return ok
        except Exception:
            _log_invoke([self._resolve_exe(), "-i"], False, False, None,
                        "health_check exception")
            return False
        finally:
            with _RYZENADJ_CALL_LOCK:
                _RYZENADJ_BUSY = False

    @staticmethod
    def _is_admin() -> bool:
        """当前进程是否以管理员运行。

        ryzenadj 需要 ring0 驱动（WinRing0 / inpoutx64）访问权限，非管理员
        下会 ``Unable to get os_access Obj`` 失败。非 Windows 平台（headless
        测试 / 非 AMD 机器）返回 True，避免误报。
        """
        if sys.platform != "win32":
            return True
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return True

    def _build_diag(self, out: str) -> str:
        """把 ryzenadj 的 stderr 映射到人类可读的根因 + 修复指引。

        关键区分：
        - HVCI(内存完整性)开启 + 管理员 → 驱动被内核拦截，必须关内存完整性；
        - 非管理员 → 权限问题，需以管理员运行；
        - 其它 → 驱动文件缺失/被杀软拦截。
        过去代码只给「请以管理员身份运行」，在 HVCI 场景下是错误指引。
        """
        low = (out or "").lower()
        if "os_access" in low or "unable to init" in low:
            if hvci_enabled():
                return ("Windows 内存完整性(内核隔离 / Core Isolation)已开启，拦截了 ryzenadj 的 "
                        "WinRing0x64.sys ring0 驱动，导致无法访问硬件。请在「Windows 安全中心 → "
                        "设备安全性 → 内核隔离」中关闭「内存完整性」，重启电脑后再试。")
            if not self._is_admin():
                return "请以管理员身份运行本程序（ryzenadj 需要 ring0 驱动权限）。"
            return ("WinRing0x64.sys 驱动无法加载（可能被杀软/系统隔离策略拦截，或文件缺失）。"
                    "请确认 blocks/ryzenadj-win64/ 下 WinRing0x64.sys 存在。")
        return ""

    def diagnose(self) -> str:
        """返回最近一次 ryzenadj 失败的根因诊断（空串=无/未知）。供 UI 提示。"""
        return getattr(self, "_last_diag", "") or ""

    def read_gpu_utilization(self) -> float:
        """Read GPU engine utilization % (PDH counter). 0.0 on failure/off-Windows.

        **直连监控优先（record_load.py 同款）**：``RyzenAdj`` 自己持有一个后台
        PowerShell 监控（``_GpuUtilMonitor``），直接读 ``\\GPU Engine(*)`` 求和。
        这条路径与用户验证可用的 ``record_load.py`` 完全一致，**不依赖** 共享的
        ``MetricSampler``（它的 GPU reader 一旦悄无声息挂掉，``gpu_pct`` 变 None，
        旧逻辑就会回退到恒为 0 的 typeperf 单帧采样）。

        诊断：读取来源与原始值节流写入 ``logs/gpu_util.log``，便于确认「到底读到
        了没有」以及「GPU 是否为真空闲」。

        This only runs on real Windows hardware; tests use :class:`MockRyzenAdj`.
        """
        if sys.platform != "win32":
            return 0.0
        # 诊断头：新代码生效的不言自明标记（只写一次、绕过节流）。
        # 作用：掌机上只要本文件是新版且被加载，gpu_util.log 启动时就有这一行；
        # 若日志完全空 → 100% 是旧 ryzenadj.py / 陈旧 __pycache__ 在生效，与
        # 浮动开关是否打开无关。
        global _GPU_UTIL_HEADER_WRITTEN
        if not _GPU_UTIL_HEADER_WRITTEN:
            _GPU_UTIL_HEADER_WRITTEN = True
            _log_gpu_util(
                "HEADER", 0.0,
                "新代码生效：GPU 利用率监控已就绪（与 record_load.py 同款直连方法，"
                "绕过共享 MetricSampler）。若此行都没有 → 跑的是旧代码/陈旧 .pyc。",
                force=True,
            )
        # ① 直连监控（与 record_load.py 同源，最可靠）
        try:
            mon = _GpuUtilMonitor.instance()
            v = mon.read()
            _log_gpu_util("direct_monitor", float(v),
                          f"直连 PowerShell GPU 监控（record_load.py 同款）；"
                          f"warmed={mon.warmed}（False=监控还没拿到过读数/进程没起来）")
            return float(v)
        except Exception:
            pass
        # ② 极端兜底：直连监控都没起来 → typeperf 一次性求和
        try:
            raw = subprocess.run(
                ["typeperf", "\\GPU Engine(*)\\Utilization Percentage", "-sc", "1"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="ignore", timeout=3.0, check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            ).stdout or ""
            lines = [ln for ln in raw.splitlines() if ln.strip()]
            if len(lines) >= 2:
                parts = next(csv.reader(io.StringIO(lines[1])))
                vals: list[float] = []
                for cell in parts[1:]:
                    cell = (cell or "").strip().strip('"')
                    if not cell:
                        continue
                    try:
                        vals.append(float(cell))
                    except ValueError:
                        pass
                if vals:
                    total = sum(vals)
                    _log_gpu_util("typeperf_fallback", total,
                                  f"instances={len(vals)} nonzero={sum(1 for v in vals if v > 0.01)}")
                    return total
        except Exception:
            pass
        _log_gpu_util("missing", 0.0, "直连监控与 typeperf 均无读数")
        return 0.0

    def apply(self, config: PowerConfig) -> bool:
        ok, _, _ = self.invoke(self.build_args(config), silent=False)
        return ok


class MockRyzenAdj(RyzenAdj):
    def __init__(self) -> None:
        super().__init__("")
        self.calls: list[PowerConfig] = []
        self.last_args: list[str] = []
        self.last_silent: bool = False
        self.next_util: float = 0.0
        self.health: bool = True
        self._ok: bool = True

    @property
    def available(self) -> bool:
        return True

    def invoke(
        self,
        args: list[str],
        silent: bool = False,
        timeout: float = 5.0,
        _prev_applied: "dict | None" = None,
        **kwargs: object,
    ) -> tuple[bool, int | None, str]:
        self.last_args = list(args)
        self.last_silent = silent
        return (self._ok, 0 if self._ok else 1, "")

    def health_check(self, timeout: float = 10.0) -> bool:
        return self.health

    def read_gpu_utilization(self) -> float:
        return self.next_util

    def apply(self, config: PowerConfig) -> bool:
        self.calls.append(config)
        self.last_args = self.build_args(config)
        return self._ok


# --- 模块加载标记（诊断用）------------------------------------------------
# 进程首次 import 本模块时写一行，证明「带日志的新代码」确实被加载。
# 若日志里连这行都没有 → 掌机跑的是旧版 ryzenadj.py（需重新拷覆盖）。
def _log_module_loaded() -> None:
    try:
        _log_invoke(
            ["<MODULE-LOADED>", f"log_path={_INVOKE_LOG}"],
            False, None, None,
            "ryzenadj 日志模块已加载（新代码生效）",
        )
    except Exception:  # noqa: BLE001
        pass


_log_module_loaded()