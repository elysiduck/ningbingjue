"""rtss 自动锁帧 tests — 低负载游戏场景顶到 AUTO_TARGET / 回落逻辑。

新逻辑判定信号（取代旧的「GPU 占用率 < 25%」2D 启发式）：
  * 前台是游戏进程 (core.game_detect.find_foreground_game)
  * 游戏进程 CPU 占用 < 40%（CPU 余量护栏：实测游戏进程 CPU 在菜单/战斗都仅 ~14%，
    max 28%，故 40 远超出正常占用、绝不误挡；仅在游戏 CPU 异常飙高时抑制升压；
    优先用共享指标器 MetricSampler.prog_cpu 前台进程级，不可用时回退 max_core）
  * GPU 自动锁频已到地板 (经 gpu_freq_provider 注入的布尔)  OR  系统 GPU 负载 < 阈值(默认50%，UI 可配)
    （前者地板 = power.GPU_MIN；后者来自 MetricSampler.gpu_pct）
  * 数据新鲜度守卫：共享指标器采样停摆（ts 过期）时 tick 保守 no-op

测试不真正启动 MetricSampler 的后台 PowerShell，而是直接把单例的 ``_running``
置 True 并写入 ``_state`` 快照（每次更新刷新 ts），从而在不依赖硬件的情况下驱动
真实代码路径。前台游戏判定与 gpu_freq_provider 通过覆写模拟。
"""

from __future__ import annotations

import json
import time

from ningbingjue.blocks.rtss import RtssBlock
from ningbingjue.core.float_params import (
    EMA_ALPHA,
    METRIC_STALE_SECS,
    METRIC_STALE_UI_SECS,
)
from ningbingjue.core.metrics import MetricSampler
from ningbingjue.platform.rtss_cli import MockRtssCli

_N_CORES = 8


def _start_fake_sampler() -> None:
    sampler = MetricSampler.instance()
    sampler._running = True  # 不真正起 PowerShell，仅令 is_running 为真
    sampler._state["ok"] = True
    sampler._state["ts"] = time.monotonic()  # 新鲜


def _auto_block(tmp_path, make_rtss_ui, initial_max_core=50.0, initial_gpu=50.0,
                initial_prog=None):
    cli = MockRtssCli()
    block = RtssBlock(cli, str(tmp_path), ui=make_rtss_ui())
    _start_fake_sampler()
    sampler = MetricSampler.instance()

    def set_metrics(max_core=50.0, gpu=50.0, prog=None) -> None:
        # max_core 写入各核相同值，snapshot.max_core = max(cores) = max_core
        sampler._state["cores"] = {i: float(max_core) for i in range(_N_CORES)}
        sampler._state["gpu"] = float(gpu)
        sampler._state["prog_cpu"] = prog
        sampler._state["ts"] = time.monotonic()  # 每次更新刷新新鲜度

    set_metrics(initial_max_core, initial_gpu, initial_prog)
    block.set_metrics = set_metrics  # type: ignore[attr-defined]

    # GPU 自动锁频是否到地板（布尔），默认 False（不在地板）
    block._at_floor = False  # type: ignore[attr-defined]
    block.gpu_freq_provider = lambda: block._at_floor  # type: ignore[attr-defined]
    block.set_at_floor = lambda v: setattr(block, "_at_floor", bool(v))  # type: ignore[attr-defined]

    block._foreground_is_game = lambda: True  # 模拟“前台是游戏”
    return block, cli


def test_enable_sets_baseline_and_lock(tmp_path, make_rtss_ui):
    block, cli = _auto_block(tmp_path, make_rtss_ui, 50.0, 50.0)
    assert block.auto_lock_enabled is False
    block.toggle_auto_lock()
    assert block.auto_lock_enabled is True
    assert block.lock_enabled is True
    # 尊重用户默认档位 30（掌机目标帧率）
    assert block.auto_baseline == 30
    assert block.fps == 30
    assert ("limiter", True) in cli.calls
    assert ("limit", 30) in cli.calls


def test_boost_to_target_after_rise_ticks(tmp_path, make_rtss_ui):
    # 低负载：进程 CPU 10 + GPU 负载 10（<30）→ 条件成立
    block, cli = _auto_block(tmp_path, make_rtss_ui, 10.0, 10.0, initial_prog=10.0)
    block.toggle_auto_lock()
    block.auto_lock_tick()
    assert block.auto_active is False
    block.auto_lock_tick()
    assert block.auto_active is False
    block.auto_lock_tick()
    assert block.auto_active is True
    assert block.fps == block.AUTO_TARGET
    assert ("limit", block.AUTO_TARGET) in cli.calls


def test_fallback_to_baseline_after_fall_ticks(tmp_path, make_rtss_ui):
    block, cli = _auto_block(tmp_path, make_rtss_ui, 10.0, 10.0, initial_prog=10.0)
    block.toggle_auto_lock()
    for _ in range(3):
        block.auto_lock_tick()  # 顶到上限
    assert block.auto_active is True
    # 离开低负载：GPU 负载拉到 95（>50）且不在地板 → 但 EMA(α=0.4) 需几拍才越过阈值
    block.set_metrics(10.0, 95.0, 10.0)
    block.auto_lock_tick()
    assert block.auto_active is True  # 1 fall tick：EMA 仅 ~44% <50%，尚不回落
    block.auto_lock_tick()
    assert block.auto_active is True  # 2 fall tick：EMA ~64% >50%，差一拍
    block.auto_lock_tick()
    assert block.auto_active is False  # 3 fall tick：稳定回落到基线
    assert block.fps == block.auto_baseline == 30
    assert ("limit", 30) in cli.calls


def test_foreground_lost_blocks_boost(tmp_path, make_rtss_ui):
    block, cli = _auto_block(tmp_path, make_rtss_ui, 10.0, 10.0, initial_prog=10.0)
    block.toggle_auto_lock()
    block._foreground_is_game = lambda: False  # 桌面 / HUD 自身
    for _ in range(5):
        block.auto_lock_tick()
    assert block.auto_active is False  # 非游戏前台永不升压


def test_gpu_at_floor_boosts_even_with_high_load(tmp_path, make_rtss_ui):
    # GPU 自动锁频落到地板时，即使 GPU 负载高也应升压（只要 cpu 低 + 游戏）
    block, cli = _auto_block(tmp_path, make_rtss_ui, 10.0, 90.0, initial_prog=10.0)
    block.set_at_floor(True)
    block.toggle_auto_lock()
    for _ in range(3):
        block.auto_lock_tick()
    assert block.auto_active is True
    assert block.fps == block.AUTO_TARGET


def test_high_cpu_blocks_boost(tmp_path, make_rtss_ui):
    # 游戏进程 CPU > 40%（护栏阈值）→ 不满足条件（即便 GPU 负载低 + 频在地板）
    block, cli = _auto_block(tmp_path, make_rtss_ui, 75.0, 5.0, initial_prog=75.0)
    block.set_at_floor(True)
    block.toggle_auto_lock()
    for _ in range(3):
        block.auto_lock_tick()
    assert block.auto_active is False


# -- 实测数据驱动的护栏语义（原神/绝区零 16 份录制）--------------------------
def test_realistic_menu_cpu_passes_guard(tmp_path, make_rtss_ui):
    # 菜单实测中位 ~14%（max 28%），远低于 40 护栏 → 不误挡（GPU 到地板时正常升压）
    block, cli = _auto_block(tmp_path, make_rtss_ui, 50.0, 10.0, initial_prog=14.0)
    block.set_at_floor(True)
    block.toggle_auto_lock()
    for _ in range(3):
        block.auto_lock_tick()
    assert block.auto_active is True


def test_cpu_spike_blocks_boost(tmp_path, make_rtss_ui):
    # 游戏 CPU 异常飙到 45%（超过 40 护栏，如着色器编译/物理尖峰）→ 抑制升压
    block, cli = _auto_block(tmp_path, make_rtss_ui, 50.0, 5.0, initial_prog=45.0)
    block.set_at_floor(True)
    block.toggle_auto_lock()
    for _ in range(3):
        block.auto_lock_tick()
    assert block.auto_active is False


# -- prog_cpu 优先于系统级 max_core（缺陷 3 修复验证）----------------------
def test_prog_cpu_preferred_over_max_core(tmp_path, make_rtss_ui):
    # 系统单核峰值很高（后台占核 90），但游戏进程本身很轻（prog=10）→ 应升压
    block, cli = _auto_block(tmp_path, make_rtss_ui, 90.0, 10.0, initial_prog=10.0)
    block.toggle_auto_lock()
    for _ in range(3):
        block.auto_lock_tick()
    assert block.auto_active is True


def test_prog_cpu_dominant_ignores_low_max_core(tmp_path, make_rtss_ui):
    # 系统单核峰值很低（空闲 10），但游戏进程很重（prog=90）→ 不升压
    block, cli = _auto_block(tmp_path, make_rtss_ui, 10.0, 10.0, initial_prog=90.0)
    block.toggle_auto_lock()
    for _ in range(3):
        block.auto_lock_tick()
    assert block.auto_active is False


def test_max_core_fallback_when_prog_unavailable(tmp_path, make_rtss_ui):
    # prog_cpu 缺失 → 回退系统级 max_core：低则升压
    block, cli = _auto_block(tmp_path, make_rtss_ui, 10.0, 10.0, initial_prog=None)
    block.toggle_auto_lock()
    for _ in range(3):
        block.auto_lock_tick()
    assert block.auto_active is True


def test_max_core_fallback_blocks_when_high(tmp_path, make_rtss_ui):
    # prog_cpu 缺失 → 回退 max_core：高则不升压
    block, cli = _auto_block(tmp_path, make_rtss_ui, 90.0, 10.0, initial_prog=None)
    block.toggle_auto_lock()
    for _ in range(3):
        block.auto_lock_tick()
    assert block.auto_active is False


# -- 数据新鲜度守卫（缺陷 2 修复验证）--------------------------------------
def test_stale_snapshot_is_noop(tmp_path, make_rtss_ui):
    block, cli = _auto_block(tmp_path, make_rtss_ui, 10.0, 10.0, initial_prog=10.0)
    block.toggle_auto_lock()
    # 把快照时间戳拨到很久以前，模拟采样停摆
    MetricSampler.instance()._state["ts"] = time.monotonic() - 100.0
    for _ in range(5):
        block.auto_lock_tick()
    assert block.auto_active is False  # 数据过期，保守不升压


# -- 升压目标 / GPU 阈值 UI 可配置（持久化 + 状态机用配置值）----------------
def test_auto_target_and_threshold_configurable(tmp_path, make_rtss_ui):
    block, _ = _auto_block(tmp_path, make_rtss_ui, 10.0, 5.0, initial_prog=10.0)
    # UI 可调：升压上限 45、GPU 阈值 10%
    block.set_auto_target(45)
    block.set_auto_gpu_load_pct(10)
    assert block.auto_target == 45
    assert block.auto_gpu_load_pct == 10
    # 持久化后同 config_dir 重建应还原（模拟重启/重载配置）
    block2, cli2 = _auto_block(tmp_path, make_rtss_ui, 10.0, 5.0, initial_prog=10.0)
    block2.load_config()
    assert block2.auto_target == 45
    assert block2.auto_gpu_load_pct == 10
    # 状态机升压应顶到配置的 45 而非默认 60
    block2.toggle_auto_lock()
    for _ in range(3):
        block2.auto_lock_tick()
    assert block2.auto_active is True
    assert block2.fps == 45
    assert ("limit", 45) in cli2.calls


def test_stale_snapshot_marks_unavailable(tmp_path, make_rtss_ui):
    block, _ = _auto_block(tmp_path, make_rtss_ui, 10.0, 10.0, initial_prog=10.0)
    MetricSampler.instance()._state["ts"] = time.monotonic() - 100.0
    assert block.auto_lock_available() is False
    assert "不可用" in block.status()


# -- gpu_freq_provider 异常保护（缺陷 1 修复验证）--------------------------
def test_provider_exception_does_not_crash(tmp_path, make_rtss_ui):
    # provider 抛异常时 gpu_at_floor 视为 False，但不应崩；gpu 负载低仍走该路升压
    block, cli = _auto_block(tmp_path, make_rtss_ui, 10.0, 10.0, initial_prog=10.0)
    def _boom() -> bool:
        raise RuntimeError("power gone")
    block.gpu_freq_provider = _boom
    block.toggle_auto_lock()
    for _ in range(3):
        block.auto_lock_tick()
    assert block.auto_active is True


def test_provider_exception_loses_floor_signal(tmp_path, make_rtss_ui):
    # 仅能靠 floor 信号（gpu 负载高），provider 异常 → floor 丢失 → 不升压
    block, cli = _auto_block(tmp_path, make_rtss_ui, 10.0, 90.0, initial_prog=10.0)
    def _boom() -> bool:
        raise RuntimeError("power gone")
    block.gpu_freq_provider = _boom
    block.toggle_auto_lock()
    for _ in range(3):
        block.auto_lock_tick()
    assert block.auto_active is False


# -- 手动改 FPS 接管（缺陷 9 修复验证）-------------------------------------
def test_manual_fps_takeover_clears_active(tmp_path, make_rtss_ui):
    block, cli = _auto_block(tmp_path, make_rtss_ui, 10.0, 10.0, initial_prog=10.0)
    block.toggle_auto_lock()
    for _ in range(3):
        block.auto_lock_tick()
    assert block.auto_active and block.fps == block.AUTO_TARGET
    block.update_slider_ui(45)  # 手动改帧率
    assert block.auto_active is False
    assert block.auto_baseline == 45
    # 之后即便条件变 false，回落也不会覆盖用户的 45
    block.set_metrics(90.0, 90.0, 90.0)
    block.auto_lock_tick()
    block.auto_lock_tick()
    assert block.fps == 45


def test_disable_restores_baseline(tmp_path, make_rtss_ui):
    block, cli = _auto_block(tmp_path, make_rtss_ui, 10.0, 10.0, initial_prog=10.0)
    block.toggle_auto_lock()
    for _ in range(3):
        block.auto_lock_tick()  # 升压
    assert block.auto_active is True
    block.toggle_auto_lock()  # 关闭
    assert block.auto_lock_enabled is False
    assert block.auto_active is False
    assert block.fps == 30  # 基线恢复


def test_unavailable_provider_reports_disabled(tmp_path, make_rtss_ui):
    # 指标器未运行（headless）→ 自动锁帧不可用，tick 为 no-op
    MetricSampler.instance()._running = False
    MetricSampler.instance()._state["cores"] = None
    MetricSampler.instance()._state["gpu"] = None
    cli = MockRtssCli()
    block = RtssBlock(cli, str(tmp_path), ui=make_rtss_ui())  # 无 gpu_freq_provider
    assert block.auto_lock_available() is False
    status = block.status()
    assert "不可用" in status
    block.auto_lock_enabled = True
    block.auto_lock_tick()
    assert block.auto_active is False


def test_config_persists_auto_lock(tmp_path, make_rtss_ui):
    block, cli = _auto_block(tmp_path, make_rtss_ui, 50.0, 50.0)
    block.fps = 40  # 用户已显式设 40 上限
    block.toggle_auto_lock()
    assert block.auto_baseline == 40  # 沿用现有上限
    data = json.loads((tmp_path / "rtss_config.json").read_text(encoding="utf-8"))
    assert data["AutoLockEnabled"] is True
    assert data["AutoBaseline"] == 40
    # 重新加载
    block2, _ = _auto_block(tmp_path, make_rtss_ui, 50.0, 50.0)
    block2.load_config()
    assert block2.auto_lock_enabled is True
    assert block2.auto_baseline == 40
    assert block2.lock_enabled is True


# -- 挂载到全局性能检查脚本 MetricSampler（失焦也运行，同 GPU 浮动）-----------
def test_auto_lock_mounted_on_metric_sampler(tmp_path, make_rtss_ui):
    block, cli = _auto_block(tmp_path, make_rtss_ui, 10.0, 10.0, initial_prog=10.0)
    sampler = MetricSampler.instance()
    # 构造时已把状态机注册到全局 MetricSampler 的采样回调
    assert block._on_sampler_sample in sampler._callbacks
    # 开启后，模拟「每次采样后回调」(后台线程调用形态) 应驱动升压
    block.toggle_auto_lock()
    for _ in range(3):
        block._on_sampler_sample(sampler.snapshot)
    assert block.auto_active is True
    assert ("limit", block.AUTO_TARGET) in cli.calls
    # 清理，避免污染单例（正常 app 退出由 sampler.stop 统一清）
    sampler.unregister_callback(block._on_sampler_sample)


def test_sampler_callback_does_nothing_when_disabled(tmp_path, make_rtss_ui):
    block, cli = _auto_block(tmp_path, make_rtss_ui, 10.0, 10.0, initial_prog=10.0)
    sampler = MetricSampler.instance()
    assert block.auto_lock_enabled is False
    # 未开启时，即便采样回调频繁触发也不升压、不调用 rtss
    for _ in range(5):
        block._on_sampler_sample(sampler.snapshot)
    assert block.auto_active is False
    assert ("limit", block.AUTO_TARGET) not in cli.calls
    sampler.unregister_callback(block._on_sampler_sample)


def test_rtss_aliases_shared_params():
    """自动锁帧的新鲜度窗口必须与 power GPU 浮动共用同一份（core.float_params）。"""
    assert RtssBlock.AUTO_STALE_SECS == METRIC_STALE_SECS
    assert RtssBlock.AUTO_STALE_UI_SECS == METRIC_STALE_UI_SECS


def test_auto_lock_uses_shared_ema_alpha(tmp_path, make_rtss_ui):
    """自动锁帧对 GPU 负载的平滑必须采用共享的 EMA_ALPHA（与 power 同一系数），
    而非各写死的 0.4。

    注入一串 GPU 读数，逐拍校验 gpu_load_ema 的递推值与「用共享 α 重算」一致，
    证明两块确实共用同一平滑系数。
    """
    import math

    block, cli = _auto_block(tmp_path, make_rtss_ui, 10.0, 10.0, initial_prog=10.0)
    sampler = MetricSampler.instance()
    block.toggle_auto_lock()
    block.gpu_load_ema = -1.0  # 强制重置，从干净状态递推

    ema = -1.0
    for g in (10.0, 90.0, 10.0, 90.0):
        block.set_metrics(10.0, g, 10.0)
        block.auto_lock_tick()
        if ema < 0.0:
            ema = g
        else:
            ema = EMA_ALPHA * g + (1.0 - EMA_ALPHA) * ema
        assert math.isclose(block.gpu_load_ema, ema, rel_tol=1e-9), (
            f"GPU 负载 EMA 偏离共享 α：block={block.gpu_load_ema} expected={ema}"
        )
    sampler.unregister_callback(block._on_sampler_sample)


def test_auto_lock_gpu_missing_is_not_low_load(tmp_path, make_rtss_ui):
    """当 gpu_pct 为 None（计数器损坏/暂缺）时，这一拍必须把 GPU 视为「非低负载」，
    绝不沿用上一拍的 EMA 旧值去误升压。"""
    block, cli = _auto_block(tmp_path, make_rtss_ui, 10.0, 90.0, initial_prog=10.0)
    sampler = MetricSampler.instance()
    block.toggle_auto_lock()
    # 先喂几次低负载，把 EMA 拉到 <30（<=> 视为低负载）
    for _ in range(3):
        block.set_metrics(10.0, 10.0, 10.0)
        block.auto_lock_tick()
    assert block.auto_active is True  # 低负载确实顶到了上限
    # 现在 GPU 读数消失（损坏），但 cpu/前台仍满足 => 不应因陈旧 EMA 继续升压
    block.auto_active = False
    block.auto_rise_count = 0
    sampler._state["gpu"] = None
    sampler._state["ts"] = time.monotonic()
    for _ in range(3):
        block.auto_lock_tick()
    assert block.auto_active is False, "gpu_pct 缺失时不得沿用陈旧 EMA 误升压"
    sampler.unregister_callback(block._on_sampler_sample)

