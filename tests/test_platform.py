"""Platform adapter tests — mocks prove the logic without native tools."""

from __future__ import annotations

import io
import subprocess
import sys
import threading
import time

from ningbingjue.core.config import PowerConfig
from ningbingjue.platform.lossless_scaling import MockLosslessScaling
from ningbingjue.platform.process_manager import MockProcessManager
from ningbingjue.platform.ryzenadj import (
    MockRyzenAdj, RyzenAdj, _invoke_ok, _NamedSemaphore, _RYZENADJ_SYS_MUTEX_NAME,
)
from ningbingjue.platform.rtss_cli import MockRtssCli


def test_mock_process_manager_suspend_resume():
    pm = MockProcessManager()
    assert pm.available
    assert pm.suspend(1234) is True
    assert pm.is_suspended(1234)
    assert pm.resume(1234) is True
    assert not pm.is_suspended(1234)


def test_mock_rtss_cli_records_calls():
    cli = MockRtssCli()
    cli.set_limiter(True)
    cli.set_limit(60)
    assert ("limiter", True) in cli.calls
    assert ("limit", 60) in cli.calls


def test_mock_ryzenadj_build_args():
    adj = MockRyzenAdj()
    cfg = PowerConfig(tdp=15, tdp_lock=True, gpu=800, gpu_lock=True, gpu_float_enabled=True)
    adj.apply(cfg)
    # TDP locked: W*1000 mW across stapm/fast/slow/apu-slow + slow-time 1
    # GPU locked + float: --gfx-clk <freq>（用户实测在 HX370 上此 flag 生效；
    # 之前误用 --max-gfxclk/--min-gfxclk 反而让两条路径都失效）。
    # --apu-sku 0 已删除（该参数不被本版 ryzenadj 支持，会导致整条命令被打回 Usage）。
    assert adj.last_args == [
        "--stapm-limit", "15000", "--fast-limit", "16000", "--slow-limit", "15000",
        "--apu-slow-limit", "15000", "--slow-time", "1",
        "--gfx-clk", "800",
    ]
    assert "--apu-sku" not in adj.last_args


def test_float_without_lock_still_sends_gfx_clk():
    # GPU auto (float) must emit --gfx-clk even when the static GPU lock is OFF,
    # matching the original power.ps1 Send-GpuClock behavior (float is independent
    # of the static lock). Regression test for the gpu_lock-or-gpu_float_enabled gate.
    adj = MockRyzenAdj()
    cfg = PowerConfig(tdp=15, tdp_lock=True, gpu=800, gpu_lock=False, gpu_float_enabled=True)
    adj.apply(cfg)
    assert "--gfx-clk" in adj.last_args
    # --apu-sku 已删除：本版 ryzenadj 不识别该参数，会导致命令失效。
    assert "--apu-sku" not in adj.last_args


def test_invoke_ok_text_markers_accept_nonzero_exit():
    """统一成功判定：即使退出码非 0，只要回执含 'Successfully'/'set gfx' 仍判成功。

    HX370 上 ``--gfx-clk`` 设成功却返回非 0 退出码；若 silent 路径只查退出码会把
    成功的调用误判为失败 → 熔断 → 浮动不下发。这里锁定文本判定行为。
    """
    assert _invoke_ok("Successfully set gfx to 800", 1) is True
    assert _invoke_ok("set gfx clock to 800", 0) is True
    assert _invoke_ok("unable to get os_access Obj", 1) is False
    assert _invoke_ok("Usage: ryzenadj [options]", 1) is False
    assert _invoke_ok("", 0) is True  # 退出码 0 即成功
    assert _invoke_ok("", 0xC0000005) is True  # Core Isolation 崩溃但频率已设


def test_silent_gfxclk_success_does_not_trip_breaker(monkeypatch):
    """回归（用户根因）：GPU 浮动走 silent 下发。修复前 reaper 只查退出码，
    HX370 上 ``--gfx-clk`` 设成功却退出码非 0 → 连续失败 → 熔断 → 浮动彻底不下发
    （软件降到 400 但硬件仍 900）。修复后 silent 也认文本回执 → 不熔断、持续下发。
    """
    captured: list[list[str]] = []

    class _FakeProc:
        def __init__(self, out: str, code: int):
            self.returncode = code
            self._out = out

        def communicate(self, timeout=None):
            return (self._out, "")

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            pass

    def _fake_popen(cmd, *a, **k):
        idx = cmd.index("--gfx-clk")
        out = "Successfully set gfx to %s\n" % cmd[idx + 1]
        captured.append(list(cmd))
        return _FakeProc(out, 1)  # 非 0 退出码，但文本表示成功

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    adj = RyzenAdj(sys.executable)  # available=True（路径存在）
    # 模拟浮动每 tick 把目标频率往下降，全部经 silent 下发
    for f in (800, 750, 700, 650, 600):
        adj.set_desired_gpu_clk(f)
        adj.maybe_apply(silent=True)
        time.sleep(0.15)  # 等 reaper 回收并释放互斥，避免 busy 跳过
    # 等所有 reaper 线程完成
    deadline = time.time() + 5.0
    while time.time() < deadline:
        time.sleep(0.05)
        if not adj.circuit_open and len(captured) >= 4:
            break
    assert not adj.circuit_open, "熔断不应开启：--gfx-clk 设成功（文本回执）"
    assert len(captured) >= 4, f"应有至少 4 次实际下发，实际 {len(captured)}"
    for c in captured:
        assert "--gfx-clk" in c


def test_float_force_bypasses_optimistic_applied_dirty_check(monkeypatch):
    """回归（用户根因 Round 4）：GPU 浮动每拍把目标频率往同值推时，maybe_apply
    的乐观 applied 快照会令 ``desired == applied`` 脏检查直接 return，ryzenadj
    收不到新频率（软件在变、硬件卡死）。``force=True`` 必须绕过该检查强制重新下发。

    日志铁证：ryzenadj_invoke.log 在 12:42:14 最后一发 ``--gfx-clk`` 后，到
    14:29 之间再无任何 ENTRY；而 gpu_auto.log 在 13:45:50 有 ``SEND freq=650``
    却无对应 ENTRY —— 即该 SEND 被脏检查吞掉，硬件从未收到 650。
    """
    adj = RyzenAdj(sys.executable)
    calls: list[list[str]] = []

    def _fake_invoke(args, silent=True, timeout=5.0, _prev_applied=None,
                     _applied_snapshot=None):
        calls.append(list(args))
        return (True, 0, "")  # 同步成功，规避 reaper 异步干扰

    monkeypatch.setattr(adj, "invoke", _fake_invoke)

    # 第一拍：desired=650（applied 初始 None）→ 应下发
    adj.set_desired_gpu_clk(650)
    assert adj.maybe_apply(silent=True) is True
    assert len(calls) == 1, "首拍应下发一次"

    # 第二拍：浮动又算出 650（applied 已被乐观写成 650）→ 不带 force 被脏检查吞掉
    adj.set_desired_gpu_clk(650)
    assert adj.maybe_apply(silent=True) is True
    assert len(calls) == 1, "非 force 的重复目标被脏检查吞掉（即原 bug）"

    # 第三拍：带 force=True → 必须重新下发（浮动持续驱动硬件）
    adj.set_desired_gpu_clk(650)
    assert adj.maybe_apply(silent=True, force=True) is True
    assert len(calls) == 2, "force=True 应绕过脏检查重新下发"
    assert "--gfx-clk" in calls[-1]


def test_non_silent_failure_rolls_back_applied(monkeypatch):
    """非 silent（手动 Apply）真实失败时，applied 不应残留在乐观值——否则后续
    手动重设被 ``desired==applied`` 脏检查吞掉（修前 bug：手动设频率后无法再改）。
    """
    adj = RyzenAdj(sys.executable)

    def _fake_invoke(args, silent=True, timeout=5.0, _prev_applied=None,
                     _applied_snapshot=None):
        return (False, 1, "os_access failed")  # 真实失败

    monkeypatch.setattr(adj, "invoke", _fake_invoke)
    adj.set_desired_gpu_clk(700)
    assert adj.maybe_apply(silent=False) is False
    # applied 应回滚到 prev（None），而非残留乐观值 700
    assert adj._applied["gfx_clk"] is None


def test_mock_lossless_scaling_presets():
    ls = MockLosslessScaling()
    ls.current = {"scale": 2}
    assert ls.save_preset("高速") is True
    assert ls.list_presets() == ["高速"]
    ls.current = None
    assert ls.load_preset("高速") is True
    assert ls.current == {"scale": 2}
    assert ls.delete_preset("高速") is True
    assert ls.list_presets() == []


def test_named_semaphore_cross_thread_release_unlocks():
    """跨线程获取/释放（silent 路径的真实模式）：线程 A 获取、线程 B 释放后，
    A 必须能再次获取。旧 Mutex 实现此处 ReleaseMutex 跨线程必失败（ERROR_NOT_OWNER）
    → 互斥永久占用 → 全部调用 SKIP（GPU 浮动卡上限真凶）；信号量无此限制。"""
    if sys.platform != "win32":
        return
    sem = _NamedSemaphore(f"NBJ_Test_Sem_{time.time_ns()}", timeout_ms=500)
    got = []

    def worker():
        got.append(sem.try_acquire())

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert got == ["acquired"], "主线程获取失败"
    # 跨线程释放（reaper 线程的等价场景）
    sem.release()
    # 主线程再次获取 → 必须成功（旧实现会锁死 → busy）
    assert sem.try_acquire() == "acquired", "跨线程释放后互斥仍被占用（旧 Mutex 锁死 bug）"
    sem.release()


def test_named_semaphore_busy_when_held():
    """持有期间另一线程 try_acquire 应超时返回 busy（跨进程互斥语义保持）。"""
    if sys.platform != "win32":
        return
    sem = _NamedSemaphore(f"NBJ_Test_Sem_{time.time_ns()}", timeout_ms=200)
    assert sem.try_acquire() == "acquired"
    results = []

    def contender():
        results.append(sem.try_acquire())

    t = threading.Thread(target=contender)
    t.start()
    t.join()
    assert results == ["busy"], f"争用应 busy，实际 {results}"
    sem.release()
    assert sem.try_acquire() == "acquired"
    sem.release()


def test_named_semaphore_uses_same_kind_as_production():
    """生产单例类型即信号量（防有人把 _SYS_MUTEX 改回 Mutex 复现锁死 bug）。"""
    from ningbingjue.platform import ryzenadj as rz
    assert isinstance(rz._SYS_MUTEX, _NamedSemaphore)
    assert rz._RYZENADJ_SYS_MUTEX_NAME
