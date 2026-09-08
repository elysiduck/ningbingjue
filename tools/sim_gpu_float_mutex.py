"""GPU 浮动「卡在锁频上限」第二根因复现 + 修复验证（离线模拟，无需掌机/真 ryzenadj）。

日志铁证（gpu_monitor_20260820_145233.log）：freq 恒 907、applied_gfx 在 707↔907
振荡、circuit=False、util 1~4%。逐项排除后指向唯一自洽机制：

  **另一进程长期占用跨进程互斥 ``NingBingJue_RyzenAdj_WinRing0``**
  （最典型：掌机上同时跑了两个凝冰结魄实例 —— run.py 原本没有单实例守卫）。
  此时本实例每次 GPU 浮动下发：
    maybe_apply(force=True) → 乐观写 _applied=707 → invoke
      → _SYS_MUTEX.try_acquire() 阻塞 1.5s（WaitForSingleObject 超时）
      → 返回 busy-cross-process → maybe_apply 回滚 _applied=907
      → _send_gpu_clock busy 分支把 gpu_current_freq 拉回 907
  于是：freq 恒 907；applied 的 707 恰是 1.5s 阻塞窗（采样能抓到）；circuit=False
  （busy 不算失败、不熔断）；手动锁频能成（抢到间隙的锁）而浮动每拍都撞上占用。

本脚本把 ``_SYS_MUTEX.try_acquire`` 打桩为「阻塞 BLOCK_S 后返回 busy」，用真实的
PowerBlock + 真实的 RyzenAdj.maybe_apply/_reap_silent/_send_gpu_clock 复现该现象，
并验证修复（busy 连击退避 + 告警挂起）后不再每拍锤锁、且会提示用户关闭重复实例。

用法：python tools/sim_gpu_float_mutex.py
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
import types

# ── 让真实 ryzenadj.py 能在本机 import/运行（subprocess 打桩）─────────────────
subprocess.CREATE_NO_WINDOW = 0x08000000

# 模拟单次 ryzenadj 调用（无争用对照组用）：占用 HOLD 秒后成功返回（成功文本在 stderr，
# 退出码非零——与掌机 HX370 行为一致，验证 stderr 合并判定仍在工作）。
HOLD = 0.05


class _FakeProc:
    def __init__(self, args, **kw):
        self.args = args
        self.returncode = None
        self.stdout = None
        self.stderr = None

    def communicate(self, timeout=None):
        time.sleep(HOLD)
        self.returncode = 1  # 不在 OK 集合 → 必须靠 stderr 文本判定成功
        self.stdout = ""
        self.stderr = "Vanguard: Successfully set gfx clock to target\n"
        return (self.stdout, self.stderr)

    def kill(self):
        pass


def _fake_popen(args, **kw):
    return _FakeProc(args, **kw)


def _fake_run(args, **kw):
    p = _FakeProc(args, **kw)
    out, err = p.communicate()
    return types.SimpleNamespace(stdout=out, stderr=err, returncode=p.returncode)


subprocess.Popen = _fake_popen
subprocess.run = _fake_run

from ningbingjue.blocks.power import PowerBlock
from ningbingjue.core.config import AppConfig, PowerConfig
from ningbingjue.platform.ryzenadj import RyzenAdj
import ningbingjue.platform.ryzenadj as _rzmod


class _SimRyzenAdj(RyzenAdj):
    """继承真实 RyzenAdj，仅让可用性成立（不真 spawn 硬件）。"""

    def __init__(self):
        super().__init__("C:/fake/ryzenadj.exe")

    @property
    def available(self):  # type: ignore[override]
        return True


class _BlockedMutex:
    """模拟另一实例长期占用跨进程互斥：每次 try_acquire 阻塞 BLOCK_S 后返回 busy。"""

    def __init__(self, block_s: float):
        self._block = block_s
        self.acquire_count = 0

    def try_acquire(self) -> str:
        self.acquire_count += 1
        time.sleep(self._block)  # 模拟 WaitForSingleObject(1500ms) 等到超时
        return "busy"


def _make_block(ceiling: int = 907):
    cfg = AppConfig()
    cfg.power_config = PowerConfig(tdp=15, gpu=ceiling, tdp_lock=True,
                                   gpu_lock=False, gpu_float_enabled=True)
    ra = _SimRyzenAdj()
    b = PowerBlock(cfg, ra)
    # 停掉 __init__ 自动启动的浮动 worker（P1-② 改造后），本脚本用 tick_loop
    # 手动单步驱动 tick，避免 worker 并发双驱动干扰复现结果。
    b.dispose()
    b._connect_sampler = lambda: None  # type: ignore[assignment]
    b.gpu_float_enabled = True
    b.gpu_current_freq = ceiling
    # 真实掌机开启浮动时会先成功下发一次天花板(907)（toggle 路径），此处对齐初始状态
    ra.set_desired_gpu_clk(ceiling)
    with ra._state_lock:
        ra._applied["gfx_clk"] = ceiling
    b._read_gpu_util = lambda snap=None: (5.0, True)  # type: ignore[assignment]
    return b, ra


# 模拟参数：BLOCK_S=0.15（真实 1.5s 按比例缩短以省时，机制相同）
BLOCK_S = 0.15
TICK_S = 0.02
SAMPLE_S = 0.1


def _run(name: str, with_contention: bool, seconds: float) -> None:
    print("=" * 74)
    print(f"{name}: {'互斥被另一实例长期占用(busy)' if with_contention else '无争用(对照组)'}"
          f"  ceiling=907 util≈5%  运行 {seconds:.1f}s")
    print("=" * 74)
    b, ra = _make_block(907)
    orig_mutex = _rzmod._SYS_MUTEX
    held = None
    if with_contention:
        held = _BlockedMutex(BLOCK_S)
        _rzmod._SYS_MUTEX = held  # 全局互斥换成「永远 busy」的桩
    try:
        stop = threading.Event()

        def tick_loop():
            while not stop.is_set():
                try:
                    b.gpu_float_tick()
                except Exception:  # noqa: BLE001
                    pass
                stop.wait(TICK_S)

        th = threading.Thread(target=tick_loop, daemon=True)
        th.start()
        samples: list[tuple[float, int, object, bool]] = []
        start = time.monotonic()
        while time.monotonic() - start < seconds:
            samples.append((time.monotonic() - start, b.gpu_current_freq,
                            ra._applied.get("gfx_clk"), ra.circuit_open))
            stop.wait(SAMPLE_S)
        stop.set()
        th.join(timeout=1.0)
    finally:
        _rzmod._SYS_MUTEX = orig_mutex

    freqs = sorted({s[1] for s in samples})
    applieds = sorted({s[2] for s in samples}, key=lambda v: (v is None, v))
    circ = any(s[3] for s in samples)
    print(f"{'t(s)':>5} | {'freq':>6} | {'applied':>7} | circuit")
    print("-" * 42)
    for i, (t, f, a, c) in enumerate(samples):
        if i % 4 == 0:
            print(f"{t:>5.1f} | {f:>6} | {a!s:>7} | {c}")
    print("-" * 42)
    print(f"freq 取值集合 = {freqs}")
    print(f"applied 取值集合 = {applieds}  circuit = {circ}")
    if with_contention and held is not None:
        print(f"互斥 try_acquire 调用次数 = {held.acquire_count}"
              f"（无修复时≈每 1s 一次持续锤锁；修复后退避期不再调用）")
        ok_pattern = (b.gpu_current_freq == 907 and circ is False and 707 in applieds)
        if ok_pattern:
            print("✅ 复现成功：freq 恒 907 + applied 707↔907 振荡 + circuit=False"
                  " —— 与掌机日志完全一致（互斥被另一实例占用）")
        else:
            print("❌ 未复现预期组合，请检查模拟/代码")
        if getattr(b, "_gpu_busy_warn_pending", False):
            print("✅ 修复生效：已挂起「另一实例占用 ryzenadj」告警（面板将弹 toast），"
                  "且已进入退避（不再每拍锤 1.5s 锁）")
        else:
            print("（未检测到修复标志——模拟的是修复前代码，或连击数未达阈值）")
    else:
        final = b.gpu_current_freq
        print(f"最终 freq = {final}  MHz"
              + ("  ✅ 对照组收敛到地板，浮动正常" if final <= 400 else "  ❌ 对照组未收敛（异常）"))
    print()


if __name__ == "__main__":
    _run("对照组", with_contention=False, seconds=5.0)
    _run("互斥争用", with_contention=True, seconds=6.5)
    print("=" * 74)
    print("结论：freq 恒 907 + applied 707↔907 振荡 + circuit=False 的机制 =")
    print("跨进程互斥被另一实例长期占用（每拍下发 1.5s 超时 busy-SKIP → 回滚天花板）。")
    print("修复：① run.py 单实例守卫（杜绝双实例抢锁）；② busy 连击≥5 退避 5s +")
    print("      挂起「另一实例占用」告警；③ reaper 回调确定性（防竞态拉回显示）。")
