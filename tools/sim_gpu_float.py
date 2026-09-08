"""GPU 浮动「卡在 907 不降」根因复现 + 修复验证（离线模拟，无需掌机/Windows）。

做法：用真实的 PowerBlock + 真实的 RyzenAdj.maybe_apply / _reap_silent / on_silent_sent
回调逻辑，但把底层 subprocess 打桩——模拟 ryzenadj 在 WinRing0 互斥锁上的占用与耗时
（核心争用点）。这样能在普通机器上忠实复现掌机上的「busy → 回滚 _applied → 回调把显示拉回 907」
竞态，而无需管理员/真硬件。

两个场景：
  A) 仅 GPU 浮动（无其它调用方抢 ryzenadj）→ 预期能正常降到 400。
  B) GPU 浮动 + 负载门控降压控制器（enabled）频繁改 CO 也调 maybe_apply 抢同一把锁
     → 复现「applied_gfx 在 707/907 间振荡、显示 freq 卡死 907」的生产现象。

用法：python tools/sim_gpu_float.py
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time

# ── 让真实 ryzenadj.py 能在非 Windows 上 import/运行 ──────────────────────────
# 真实代码用 subprocess.CREATE_NO_WINDOW（仅 Windows 有），这里补一个无害值，
# 并把 Popen/run 打桩成「占用互斥锁 HOLD 秒后成功返回」。
subprocess.CREATE_NO_WINDOW = 0x08000000

import types

# ── 模拟 ryzenadj 进程：占用互斥锁 HOLD 秒，返回成功文本 ──────────────────────
HOLD = 0.12  # 模拟单次 ryzenadj 调用在 WinRing0 驱动上的耗时


class _FakeProc:
    def __init__(self, args, **kw):
        self.args = args
        self._ok = True
        self.returncode = None
        self.stdout = None
        self.stderr = None

    def communicate(self, timeout=None):
        time.sleep(HOLD)  # 模拟驱动占用互斥锁的时长
        # ⚠️ 关键复现：真实 ryzenadj 在 HX370 上 --gfx-clk 通常是「退出码非零
        # （且不在 OK 集合 {0,0xC0000005,-1073741819} 内）+ 成功回执打到 STDERR」。
        # 手动路径把 stdout+stderr 合并判定 → 看到 "Successfully" 判成功（所以手动能降到 400）；
        # GPU 浮动的 silent reaper 只读了 STDOUT（丢弃 STDERR）→ 误判失败 → 回滚到 907 → 卡死。
        self.returncode = 1        # 不在 OK 集合 → reaper 仅看 stdout 时必判失败
        self.stdout = ""          # stdout 空
        self.stderr = "Vanguard: Successfully set gfx clock to target\n"
        return (self.stdout, self.stderr)

    def kill(self):
        pass


def _fake_popen(args, **kw):
    return _FakeProc(args, **kw)


def _fake_run(args, **kw):
    p = _FakeProc(args, **kw)
    out, err = p.communicate()
    r = types.SimpleNamespace()
    r.stdout = out
    r.stderr = err
    r.returncode = p.returncode
    return r


subprocess.Popen = _fake_popen
subprocess.run = _fake_run

# 现在再 import 业务代码（CREATE_NO_WINDOW 已存在，subprocess 已打桩）
from ningbingjue.blocks.power import PowerBlock
from ningbingjue.core.config import AppConfig, PowerConfig
from ningbingjue.platform.ryzenadj import RyzenAdj
from ningbingjue.platform.undervolt import UndervoltController, UndervoltConfig, RyzenAdjBackend


class _SimRyzenAdj(RyzenAdj):
    """继承真实 RyzenAdj，仅让可用性与 exe 路径成立（不真 spawn 硬件）。"""
    def __init__(self):
        super().__init__("C:/fake/ryzenadj.exe")

    @property
    def available(self):  # type: ignore[override]
        return True


def _make_block(ceiling=907):
    cfg = AppConfig()
    cfg.power_config = PowerConfig(tdp=15, gpu=ceiling, tdp_lock=True,
                                   gpu_lock=False, gpu_float_enabled=True)
    ra = _SimRyzenAdj()
    b = PowerBlock(cfg, ra)
    # 停掉 __init__ 自动启动的浮动 worker（P1-② 改造后），本脚本用 float_loop
    # 手动单步驱动 tick，避免 worker 并发双驱动干扰复现结果。
    b.dispose()
    # 关掉采样器挂载/直连监控等副作用，手动驱动 tick
    b._connect_sampler = lambda: None  # type: ignore[assignment]
    b.gpu_float_enabled = True
    b.gpu_current_freq = ceiling
    # 模拟低负载（GPU 占用 5%）→ 浮动应当一路降到 400
    b._read_gpu_util = lambda snap=None: (5.0, True)  # type: ignore[assignment]
    return b, ra


def _run_scenario(name, with_undervolt, seconds=6.0):
    print("=" * 72)
    print(f"场景 {name}: GPU 浮动 {'+ 降压控制器(enabled)' if with_undervolt else '(单独)'}"
          f"  ceiling=907  util≈5%  运行 {seconds}s")
    print("=" * 72)
    b, ra = _make_block(ceiling=907)

    samples = []
    stop = threading.Event()

    def float_loop():
        # 模拟 MetricSampler 后台线程驱动浮动决策（~每 0.13s 一拍）
        while not stop.is_set():
            try:
                b.gpu_float_tick()
            except Exception as e:  # noqa
                pass
            stop.wait(0.13)

    threads = [threading.Thread(target=float_loop, daemon=True)]

    uv = None
    if with_undervolt:
        # 真·降压控制器，复用同一把 RyzenAdj；通过后台线程频繁切换 CO 触发 maybe_apply
        # （真实场景里 CO 状态在 idle/light/heavy 间跳变时会调用 backend.apply_coall）
        ucfg = UndervoltConfig(enabled=True, heavy_enabled=False, dry_run=False,
                               t_light_s=0.2, hmax=50.0)
        backend = RyzenAdjBackend(ra, dry_run=False)
        # 用一个最小 stub sampler（控制器只在状态变化时下发，与争用机制无关紧要）
        class _StubSampler:
            snapshot = types.SimpleNamespace(max_core=5.0, mean_core=2.0,
                                             gpu_pct=5.0, prog_cpu=10.0, ok=True,
                                             ts=time.monotonic())
        uv = UndervoltController(backend, _StubSampler(), ucfg)
        # 让 find_foreground_game 永远命中（视为在游戏内）
        import ningbingjue.platform.undervolt as _uvmod
        _uvmod.find_foreground_game = lambda wl=None: (1234, "game.exe")

        def uv_loop():
            toggle = {"v": -20}
            while not stop.is_set():
                # 频繁切换 CO 偏移，强制控制器经 backend.apply_coall 调 maybe_apply
                # 抢同一把 ryzenadj 互斥锁（这正是生产环境里的争用来源）
                toggle["v"] = 0 if toggle["v"] == -20 else -20
                try:
                    uv.backend.apply_coall(toggle["v"])
                except Exception:  # noqa
                    pass
                stop.wait(0.10)
        threads.append(threading.Thread(target=uv_loop, daemon=True))

    for t in threads:
        t.start()

    # 采样显示/算法 current 与硬件确认值
    start = time.monotonic()
    seen = set()
    while time.monotonic() - start < seconds:
        t = time.monotonic() - start
        freq = b.gpu_current_freq
        applied = ra._applied.get("gfx_clk")
        samples.append((round(t, 2), freq, applied, ra.circuit_open))
        seen.add(freq)
        stop.wait(0.10)

    stop.set()
    for t in threads:
        t.join(timeout=1.0)

    # 输出时间线（每 ~0.5s 抽一个点，避免刷屏）
    print(f"{'t(s)':>6} | {'显示freq':>7} | {'applied':>7} | circuit")
    print("-" * 40)
    for i, (t, freq, applied, circ) in enumerate(samples):
        if i % 5 == 0:
            print(f"{t:>6} | {freq:>7} | {applied!s:>7} | {circ}")
    final = samples[-1][1]
    print("-" * 40)
    print(f"最终显示 freq = {final}  MHz")
    print(f"期间显示 freq 取值集合 = {sorted(seen)}")
    if final <= 400:
        print("✅ 收敛到地板 400，浮动正常。")
    else:
        print(f"❌ 卡在 {final} MHz 不降 —— 复现生产 bug（applied 抖动、显示贴旧值）。")
    print()
    return final


if __name__ == "__main__":
    fa = _run_scenario("A", with_undervolt=False, seconds=6.0)
    fb = _run_scenario("B", with_undervolt=True, seconds=6.0)
    print("=" * 72)
    print("结论：")
    print(f"  场景 A（仅浮动）最终显示 = {fa}  -> {'正常' if fa <= 400 else '异常'}")
    print(f"  场景 B（浮动+降压）最终显示 = {fb}  -> {'正常' if fb <= 400 else '异常(复现卡907)'}")
