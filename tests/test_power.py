"""Power block tests (headless, MockRyzenAdj)."""

from __future__ import annotations

import time

from ningbingjue.blocks.power import PowerBlock
from ningbingjue.core.config import AppConfig, PowerConfig
from ningbingjue.core.metrics import MetricSampler
from ningbingjue.platform.ryzenadj import MockRyzenAdj


def _block(tdp=15, gpu=800, tdp_lock=False, gpu_lock=False, gpu_float=False):
    cfg = AppConfig()
    cfg.power_config = PowerConfig(tdp=tdp, gpu=gpu, tdp_lock=tdp_lock,
                                  gpu_lock=gpu_lock, gpu_float_enabled=gpu_float)
    b = PowerBlock(cfg, MockRyzenAdj())
    # 测试确定性（P1-② 改造后 __init__ 会经 _connect_sampler 启动浮动 worker）：
    # 立即停掉，测试用 _float_worker_once() 手动单步驱动，避免后台线程干扰断言。
    b.dispose()
    return b


class _FakeLabel:
    """捕获 show_toast 写入的文案（PowerBlock.show_toast 只写 UI 控件、不存
    实例字段，故测试注入此假 label 来断言弹窗内容）。"""

    def __init__(self) -> None:
        self.text = ""
        self.fore_color = None


def _with_toast_label(b):
    lbl = _FakeLabel()
    b.ui["toast_label"] = lbl  # type: ignore[index]
    return lbl


def test_gpu_float_worker_drives_tick():
    """GPU 浮动决策由独立 worker 驱动（P1-② 改造，2026-08-20）：

    旧实现把浮动决策挂在 MetricSampler 采样回调里同步执行——SMT 图 / 温度 /
    自动锁帧共用同一条采样链，浮动一发卡（ryzenadj 下发 / 互斥等待）会阻塞整个
    采样宿主。改为独立 worker 线程后：单步（_float_worker_once）应驱动决策，
    且 worker 不受窗口焦点影响（daemon 线程照常运行，等价旧「挂采样器」的
    失焦免疫）。此测试验证单步驱动 + worker 生命周期。
    """
    b = _block(gpu=800, gpu_float=True)
    fired = []
    orig = b.gpu_float_tick

    def _spy(*a, **k):
        fired.append(1)
        return orig(*a, **k)

    b.gpu_float_tick = _spy  # type: ignore[assignment]
    b._float_worker_once()
    assert fired, "浮动 worker 单步应驱动 gpu_float_tick"

    # worker 生命周期：start 后存活，dispose 后停止
    b._start_float_worker()
    assert b._float_worker is not None and b._float_worker.is_alive(), \
        "浮动 worker 应已启动（daemon，失焦照常运行）"
    b.dispose()
    assert b._float_worker is None or not b._float_worker.is_alive(), \
        "dispose 后浮动 worker 应停止"


def test_gpu_float_worker_respects_enabled():
    """浮动关闭时 worker 单步不应驱动决策。"""
    b = _block(gpu=800, gpu_float=False)
    fired = []
    orig = b.gpu_float_tick

    def _spy(*a, **k):
        fired.append(1)
        return orig(*a, **k)

    b.gpu_float_tick = _spy  # type: ignore[assignment]
    b._float_worker_once()
    assert not fired, "gpu_float 关闭时 worker 不应驱动 gpu_float_tick"


def test_clamp_ranges():
    b = _block()
    assert b._clamp_tdp(99) == b.TDP_MAX
    assert b._clamp_tdp(0) == b.TDP_MIN
    assert b._clamp_gpu(99999) == b.GPU_MAX
    assert b._clamp_gpu(0) == b.GPU_MIN


def test_apply_builds_args_and_invokes():
    b = _block(tdp=10, gpu=800, tdp_lock=True, gpu_lock=True)
    ok = b.apply_power()
    assert ok is True
    assert b.applied is True
    assert b.ryzenadj.last_args == [
        "--stapm-limit", "10000", "--fast-limit", "11000", "--slow-limit", "10000",
        "--apu-slow-limit", "10000", "--slow-time", "1", "--gfx-clk", "800",
    ]


def test_apply_with_float_enabled_respects_manual_gpu():
    """回归：GPU 浮动开启时，手动设 GPU 频率（滑块+Apply）必须立即生效，

    不能再被 apply_power 里的 `pass` 跳过（用户报告：开浮动后改不了频率、
    表现成“GPU 浮动没有任何作用”）。修复后手动值作为频率上限+当前频率下发，
    浮动从该值继续自动调节。
    """
    b = _block(tdp=15, gpu=900, tdp_lock=True, gpu_float=True)
    ok = b.apply_power()
    assert ok is True
    # 手动值必须作为当前频率下发：desired gfx == 900
    assert b.ryzenadj._desired["gfx_clk"] == 900
    assert b.gpu_current_freq == 900
    # 实际下发的命令行必须包含 --gfx-clk 900（用户实测在 HX370 上此 flag 生效）
    assert "--gfx-clk" in b.ryzenadj.last_args
    assert "900" in b.ryzenadj.last_args


def test_gpu_float_high_boosts():
    b = _block(gpu=800, gpu_float=True)
    b.gpu_current_freq = 600  # below cap so there is headroom
    b.ryzenadj.next_util = 95.0
    b.gpu_float_tick()  # 第 1 拍：升频防抖计数中，仍 600
    assert b.gpu_current_freq == 600
    b.gpu_float_tick()
    # 高占用(≥92%)饱和抬频：分级步长 95%→+100 → 700
    assert b.gpu_current_freq == 700
    assert b.gpu_current_freq > 600


def test_gpu_float_tiered_down_deep_idle():
    # 新分级降频阶梯 + 加强防抖：util<10% → 每拍 -200；需连续 3 拍确认才降第一档
    # （FLOAT_DOWN_DEBOUNCE_TICKS=3，防单拍毛刺）；降一档后清零，下一档再连 3 拍确认。
    b = _block(gpu=800, gpu_float=True)
    b.gpu_current_freq = 800
    b.gpu_last_send_ts = 0.0
    b.ryzenadj.next_util = 5.0
    b.gpu_float_tick()  # 第 1 拍：防抖计数中，不降
    assert b.gpu_current_freq == 800
    b.gpu_float_tick()  # 第 2 拍：仍计数中
    assert b.gpu_current_freq == 800
    b.gpu_float_tick()  # 第 3 拍：确认，-200 → 600
    assert b.gpu_current_freq == 600


def test_gpu_float_floor_raise_on_high_load():
    """回归：卡在 400MHz 地板且负载确实高(≥85%)时，必须抬离，而非死等 util≥92–98%。

    旧算法在 400MHz 处：升频步长被 50MHz 对齐吞掉、永远等于 400；导致有负载时
    GPU 永远锁在 0.4GHz。修复后：util_peak>=85%（施密特脱离阈值，与分级降频
    <85% 对齐）即一次性抬到单档预热地板 FLOAT_ACTIVE_FLOOR(600) 并稳住。
    升频防抖：需连续 2 拍高负载确认（FLOAT_UP_DEBOUNCE_TICKS=2，防单拍尖峰误抬）。
    """
    b = _block(gpu=800, gpu_float=True)
    b.gpu_current_freq = 400
    b.ryzenadj.next_util = 90.0  # 高载(≥85%)
    b.gpu_float_tick()           # 第 1 拍：升频防抖计数中，仍 400
    assert b.gpu_current_freq == 400
    b.gpu_float_tick()           # 第 2 拍：确认，抬到预热地板 600
    assert b.gpu_current_freq == 600, (
        f"期望从地板 400 抬到预热地板 600，实际 {b.gpu_current_freq}")
    # 预热地板是常量（非活跃下限），gpu_floor_active 不再被抬升，保持 400
    assert b.gpu_floor_active == 400


def test_gpu_float_floor_raise_threshold():
    """地板抬离门槛=85%（施密特脱离阈值）：≥85% 抬离，<85% 继续 hold 在地板。

    设计约束：脱离阈值(85%)与分级降频上界(<85%)严格互补，稳定负载只落一侧。
    升频防抖需连续 2 拍确认。
    """
    b = _block(gpu=800, gpu_float=True)
    b.gpu_current_freq = 400
    # 90% >= 阈值 → 连续 2 拍抬到 600
    b.ryzenadj.next_util = 90.0
    b.gpu_float_tick()
    assert b.gpu_current_freq == 400  # 第 1 拍防抖计数
    b.gpu_float_tick()
    assert b.gpu_current_freq == 600, f"90% 应抬离到 600，实际 {b.gpu_current_freq}"
    # 重置回地板，80% < 阈值 → 保持 400（<85% 分级降频本就要降到地板）
    b2 = _block(gpu=800, gpu_float=True)
    b2.gpu_current_freq = 400
    b2.ryzenadj.next_util = 80.0
    b2.gpu_float_tick()
    assert b2.gpu_current_freq == 400, f"80% 应继续 hold 400，实际 {b2.gpu_current_freq}"


def test_gpu_float_floor_no_flap():
    """回归（施密特迟滞）：稳定低负载(20%)不得 400↔600 抖动。

    修复后：① 锁在 400 且 util_peak<85% → 不抬离（仍 400，不再误抬）；
    ② 若已在 600，util_peak<85% → 分级降频拉回 400 后停在地板（不回弹）。
    两种情况最终都收敛到 400 且不来回抖。
    """
    # ① 锁在地板 + 持续 20% 轻载：不应误抬，稳定停在 400
    b = _block(gpu=800, gpu_float=True)
    b.gpu_current_freq = 400
    b.ryzenadj.next_util = 20.0
    for _ in range(8):
        b.gpu_float_tick()
    assert b.gpu_current_freq == 400, (
        f"地板+20% 轻载不应误抬，应稳在 400，实际 {b.gpu_current_freq}")

    # ② 先在高位(600)，持续 20% 轻载：分级降频拉回 400 后稳住（不回弹到 600）
    import ningbingjue.blocks.power as _powermod
    b2 = _block(gpu=800, gpu_float=True)
    b2.gpu_current_freq = 600
    b2.gpu_last_send_ts = 0.0
    b2.ryzenadj.next_util = 20.0
    _clock = [3_000_000.0]
    _orig_time = _powermod.time.time
    try:
        _powermod.time.time = lambda: (_clock.__setitem__(0, _clock[0] + 2.0) or _clock[0])
        for _ in range(20):
            b2.gpu_float_tick()
    finally:
        _powermod.time.time = _orig_time
    assert b2.gpu_current_freq == 400, (
        f"600+20% 轻载应分级降频拉回 400 后稳住，实际 {b2.gpu_current_freq}")


def test_gpu_float_min_up_step_no_stall_at_intermediate():
    """回归：升频最小步长(FLOAT_MIN_UP_STEP=50)防止低频步长被 50 对齐吞掉。

    在 current=450、util=93% 时，旧乘法步长≈15MHz，new=465→round→450 卡死；
    现饱和抬频用固定分级加法步长（93%→+50）。升频防抖需连续 2 拍确认。
    """
    b = _block(gpu=800, gpu_float=True)
    b.gpu_current_freq = 450
    b.ryzenadj.next_util = 93.0
    b.gpu_float_tick()  # 第 1 拍：升频防抖计数中，仍 450
    assert b.gpu_current_freq == 450
    b.gpu_float_tick()  # 第 2 拍：确认，+50 → 500
    assert b.gpu_current_freq == 500, (
        f"450@93% 应升到 500（不再卡 450），实际 {b.gpu_current_freq}")



def test_gpu_float_low_load_comes_down():
    """回归（根因①）：低/中负载(<85%) 经分级降频降到 400，而非死守高位。

    旧逻辑把 10–50% 整段丢进死区 hold，导致游戏菜单/2D/挂机等低中负载永远锁在
    600+。修复后：util_peak<85% 即按阶梯降频（30%→-100/tick）降到 400。
    分级降频走非 force 下发受 1s 冷却节流，单元测试需推进假时钟。
    """
    import ningbingjue.blocks.power as _powermod
    b = _block(gpu=800, gpu_float=True)
    b.gpu_current_freq = 800
    b.gpu_last_send_ts = 0.0
    b.ryzenadj.next_util = 30.0  # 低载(30%<60%→-100) → 应降下来
    _clock = [6_000_000.0]
    _orig_time = _powermod.time.time
    try:
        _powermod.time.time = lambda: (_clock.__setitem__(0, _clock[0] + 2.0) or _clock[0])
        for _ in range(20):
            b.gpu_float_tick()
    finally:
        _powermod.time.time = _orig_time
    assert b.gpu_current_freq == 400, (
        f"30% 低载应经分级降频降到 400，实际 {b.gpu_current_freq}")


def test_gpu_float_sawtooth_still_drops():
    """回归（根因②）：5%↔15% 锯齿不阻断分级降频。

    旧逻辑用瞬时利用率判空闲：锯齿中任一帧 ≥10% 就把 idle_count 清零、降不下来。
    现用 3 帧峰值 util_peak 判档位，锯齿帧(15%) 的峰值仍 <30% → 走 -100 档，稳定
    降到 400，不再被单帧毛刺卡死。
    """
    import ningbingjue.blocks.power as _powermod
    b = _block(gpu=800, gpu_float=True)
    b.gpu_current_freq = 600
    b.gpu_last_send_ts = 0.0
    _clock = [7_000_000.0]
    _orig_time = _powermod.time.time
    try:
        _powermod.time.time = lambda: (_clock.__setitem__(0, _clock[0] + 2.0) or _clock[0])
        for i in range(20):
            # 5%↔15% 锯齿：3 帧峰值取到 15%，仍 <30% → -100 档，稳定下降
            b.ryzenadj.next_util = 15.0 if (i % 2 == 0) else 5.0
            b.gpu_float_tick()
    finally:
        _powermod.time.time = _orig_time
    # 锯齿下的峰值(<30%) 仍触发分级降频，最终降到 400
    assert b.gpu_current_freq == 400, (
        f"5%/15% 锯齿应稳定降到 400，实际 {b.gpu_current_freq}")


def test_gpu_float_deadband_holds_no_jitter():
    """回归：缓冲带 [85%, 92%) 稳住（不升不降）；<85% 分级降频；低负载落到 400。

    88% 落在缓冲带 → 稳住；70% 落在 <85% 段 → 降 25。注意分级降频走非 force
    下发，受 1s 冷却节流；单元测试瞬间跑完需推进假时钟。
    """
    import ningbingjue.blocks.power as _powermod

    b = _block(gpu=800, gpu_float=True)
    b.gpu_current_freq = 600
    b.gpu_last_send_ts = 0.0
    # 88%：落在 [85%,92%) 缓冲带 → 稳住 600（不升压不降频）
    b.ryzenadj.next_util = 88.0
    b.gpu_float_tick()
    assert b.gpu_current_freq == 600, f"88% 缓冲带应稳住 600，实际 {b.gpu_current_freq}"

    # 70%：<85% → 分级降频 -25（需连续 3 拍确认，加强防抖后逐档重新确认）
    b.gpu_util_buf = []
    b.gpu_util_ema = -1.0
    b.gpu_current_freq = 600
    b.gpu_last_send_ts = 0.0
    b.gpu_float_down_hold = 0
    b.gpu_float_up_hold = 0
    b.ryzenadj.next_util = 70.0
    b.gpu_float_tick()
    assert b.gpu_current_freq == 600  # 第 1 拍防抖计数中
    b.gpu_float_tick()
    assert b.gpu_current_freq == 600  # 第 2 拍仍计数中
    b.gpu_float_tick()
    assert b.gpu_current_freq == 575, f"70% 第 3 拍应降 25 到 575，实际 {b.gpu_current_freq}"

    # 轻载 20%：分级降频把 600 拉回 400（低载降得下来）
    b.gpu_util_buf = []
    b.gpu_util_ema = -1.0
    b.gpu_current_freq = 600
    b.gpu_last_send_ts = 0.0
    b.ryzenadj.next_util = 20.0
    _clock = [4_000_000.0]
    _orig_time = _powermod.time.time
    try:
        _powermod.time.time = lambda: (_clock.__setitem__(0, _clock[0] + 2.0) or _clock[0])
        for _ in range(20):
            b.gpu_float_tick()
    finally:
        _powermod.time.time = _orig_time
    assert b.gpu_current_freq == 400, f"20% 轻载应经分级降频落到 400，实际 {b.gpu_current_freq}"

    # 低载 30%：同样降到 400
    b.gpu_util_buf = []
    b.gpu_util_ema = -1.0
    b.gpu_current_freq = 600
    b.gpu_last_send_ts = 0.0
    b.ryzenadj.next_util = 30.0
    _clock = [5_000_000.0]
    try:
        _powermod.time.time = lambda: (_clock.__setitem__(0, _clock[0] + 2.0) or _clock[0])
        for _ in range(20):
            b.gpu_float_tick()
    finally:
        _powermod.time.time = _orig_time
    assert b.gpu_current_freq == 400, f"30% 低载应经分级降频落到 400，实际 {b.gpu_current_freq}"


def test_gpu_float_scenario_matches_user_spec():
    """端到端锁合约：对照用户新指定阶梯（<85% 分级降频 / ≥85% 抬离 / ≥92% 抬频）。

    场景映射（ceiling=800，施密特：脱离≥85% / 分级降频<85% / 饱和抬频≥92%）：
      - 70% 锁在 400  → <85% 保持地板 400（分级降频本就要降到地板）
      - 95% 锁在 400  → 先抬到 600，再逐拍 +100 缓慢爬（不跳到 800）
      - 轻载 20% 稳在 600 → -100/tick 快速拉回 400
      - 完全空闲(<10%)   → -200/tick 更快拉回 400
    """
    import ningbingjue.blocks.power as _powermod
    b = _block(gpu=800, gpu_float=True)

    # 1) 70% 锁在 400 → <85% 保持地板 400（分级降频本就要降到地板）
    b.gpu_current_freq = 400
    b.ryzenadj.next_util = 70.0
    b.gpu_float_tick()
    assert b.gpu_current_freq == 400, f"70%@400 应保持地板 400，实际 {b.gpu_current_freq}"

    # 2) 95% 锁在 400 → 先 600（升频防抖 2 拍确认），再缓慢爬（每档升频也需 2 拍，
    #    不一次性跳到 800）—— 加强防抖后升频同样逐档确认。
    b.gpu_current_freq = 400
    b.gpu_util_buf = []
    b.gpu_util_ema = -1.0
    b.gpu_float_down_hold = 0
    b.gpu_float_up_hold = 0
    b.ryzenadj.next_util = 95.0
    b.gpu_float_tick()
    assert b.gpu_current_freq == 400, f"95%@400 首拍升频防抖计数中，应仍 400，实际 {b.gpu_current_freq}"
    b.gpu_float_tick()
    assert b.gpu_current_freq == 600, f"95%@400 第二拍应抬到 600，实际 {b.gpu_current_freq}"
    b.gpu_float_tick()
    assert b.gpu_current_freq == 600, f"95%@600 第三拍饱和升频防抖计数中，应仍 600，实际 {b.gpu_current_freq}"
    b.gpu_float_tick()
    assert b.gpu_current_freq == 700, f"95%@600 第四拍应 +100 到 700，实际 {b.gpu_current_freq}"

    # 3) 轻载 20% 稳在 600：-100/tick 快速拉回 400（先清掉上一场景遗留的 3 帧峰值缓存）
    # 分级降频走非 force 下发，受 1s 冷却节流；单元测试推进假时钟。
    b.gpu_current_freq = 600
    b.gpu_util_buf = []
    b.gpu_util_ema = -1.0
    b.gpu_last_send_ts = 0.0
    b.ryzenadj.next_util = 20.0
    _clock = [2_000_000.0]
    _orig_time = _powermod.time.time
    try:
        _powermod.time.time = lambda: (_clock.__setitem__(0, _clock[0] + 2.0) or _clock[0])
        for _ in range(20):
            b.gpu_float_tick()
    finally:
        _powermod.time.time = _orig_time
    assert b.gpu_current_freq == 400, f"20% 轻载应经分级降频降到 400，实际 {b.gpu_current_freq}"

    # 4) 完全空闲(<10%) → -200/tick 更快拉回 400
    # 注：分级降频走非 force 路径，受 1s 冷却节流；单元测试瞬间跑完需推进假时钟
    # （生产环境采样器每 ~1.5s 触发一次，冷却从不拦截，故无此问题）。
    b.gpu_current_freq = 600
    b.gpu_util_buf = []
    b.gpu_util_ema = -1.0
    b.gpu_last_send_ts = 0.0
    b.ryzenadj.next_util = 5.0
    _clock = [3_000_000.0]
    try:
        _powermod.time.time = lambda: (_clock.__setitem__(0, _clock[0] + 2.0) or _clock[0])
        for _ in range(20):
            b.gpu_float_tick()
    finally:
        _powermod.time.time = _orig_time
    assert b.gpu_current_freq == 400, f"空闲应降回 400，实际 {b.gpu_current_freq}"


def test_gpu_float_prefers_metric_sampler_over_direct_monitor():
    """回归：GPU 浮动必须优先采用共享 MetricSampler 的 gpu_pct，而不是独立的
    ``_GpuUtilMonitor``（后者在 ``\\GPU Engine(*)`` 计数器损坏时会恒返 0，导致
    浮动失效——即用户报告的「GPU 浮动没有任何效果」）。

    这里让直连监控（read_gpu_utilization）报告 5%（会被误判为空闲→降频），但
    共享采样器报告 95%（应升频），修复后必须采用共享采样器的值升频到 650。
    """
    b = _block(gpu=800, gpu_float=True)
    b.gpu_current_freq = 600  # below cap so there is headroom
    sampler = MetricSampler.instance()
    b.ryzenadj.next_util = 5.0  # 直连监控的（坏）读数：空闲
    try:
        sampler._state["gpu"] = 95.0  # 共享采样器的（真）读数：高负载
        sampler._state["ok"] = True
        sampler._state["ts"] = time.monotonic()
        b.gpu_float_tick()  # 第 1 拍：升频防抖计数中，仍 600
        assert b.gpu_current_freq == 600
        b.gpu_float_tick()  # 第 2 拍：确认，采用共享采样器 95% 升频到 700
        assert b.gpu_current_freq == 700, (
            f"期望采用共享采样器 95% 升频到 700，实际 {b.gpu_current_freq}"
        )
    finally:
        sampler._state["gpu"] = None
        sampler._state["ok"] = False


def test_toggle_locks():
    b = _block()
    assert not b.tdp_lock
    b.toggle_tdp_lock()
    assert b.tdp_lock
    b.toggle_gpu_lock()
    assert b.gpu_lock


def test_navigation_matrix():
    b = _block()
    b.on_activate_item()
    assert b.focus_idx.value == 0  # tdp slider
    b.on_navigate(1)  # down within col 0
    assert b.focus_idx.value == 2  # gpu slider
    b.on_navigate(1)  # down
    assert b.focus_idx.value == 4  # gpu float btn
    b.on_navigate(1)  # wraps
    assert b.focus_idx.value == 0


def test_gpu_float_display_tracks_confirmed_not_optimistic():
    """GPU 浮动显示/算法 current 必须贴合硬件已确认频率，而非停在乐观目标值。

    旧实现：_send_gpu_clock 无条件把 gpu_current_freq 设为目标，silent 是
    fire-and-forget，下发被 SKIP 或 reaper 判失败后硬件没收到，显示却已跳变 →
    发散，且算法误以为已生效而停止重试（「GPU 浮动有时控不住」根因）。
    """
    b = _block(gpu=800, gpu_float=True)
    b.ryzenadj.on_silent_sent = b._on_gpu_sent
    # 已确认频率 = 600
    b.ryzenadj._applied = {"gfx_clk": 600, "coall_mv": None, "tdp_mw": None}
    b.gpu_current_freq = 600

    # 场景1：下发被受理（正常 fire-and-forget）→ 乐观推进到目标
    def _accept(force=False):
        return True
    b.ryzenadj.maybe_apply = _accept
    b.ryzenadj.set_desired_gpu_clk(650)
    b._send_gpu_clock(650, reason="t1")
    assert b.gpu_current_freq == 650  # 受理成功先乐观推进

    # 场景2：下发被 SKIP（busy，瞬态）→ 必须回退到硬件已确认频率（600），
    # 不能乐观推进到目标（700）。这正是用户报的「显示降到 400、实际没变」根因：
    # 旧代码此处乐观推进，硬件没收到、又无 reaper 回调校正 → 显示与硬件永久发散、
    # 算法误以为到位而停止重试。现回退到 confirmed，desired 仍=700，下一拍
    # desired!=applied 自动重试，直到真成功（reaper 经 on_silent_sent 校正显示）。
    def _skip_busy(force=False):
        b.ryzenadj._applied = {"gfx_clk": 600, "coall_mv": None, "tdp_mw": None}
        return False
    b.ryzenadj.maybe_apply = _skip_busy
    b.ryzenadj._circuit_open = False
    b.ryzenadj.set_desired_gpu_clk(700)
    b._send_gpu_clock(700, force=True, reason="t2")  # force 跳过 1s 冷却，确定性触发逻辑
    assert b.gpu_current_freq == 600, (
        f"busy SKIP 后应回退到确认值 600（贴真），实际 {b.gpu_current_freq}")

    # 场景2b：连续多次 busy，显示始终贴 confirmed，绝不漂移（算法持续重试）。
    for _ in range(5):
        b.ryzenadj.set_desired_gpu_clk(700)
        b._send_gpu_clock(700, force=True, reason="t2-loop")
        assert b.gpu_current_freq == 600, "连续 busy 也必须贴 confirmed=600"

    # 场景2b：熔断（circuit open）→ 必须回退到已确认值（贴真），不能停在乐观值
    def _skip_circuit(force=False):
        b.ryzenadj._applied = {"gfx_clk": 600, "coall_mv": None, "tdp_mw": None}
        return False
    b.ryzenadj.maybe_apply = _skip_circuit
    b.ryzenadj._circuit_open = True
    b.ryzenadj.set_desired_gpu_clk(700)
    b._send_gpu_clock(700, force=True, reason="t3")
    assert b.gpu_current_freq == 600, (
        f"熔断 SKIP 后应回退到确认值 600，实际 {b.gpu_current_freq}")

    # 场景3：reaper 判定失败 → on_silent_sent 把 gpu_current_freq 从乐观目标回滚到确认值
    b.ryzenadj._applied = {"gfx_clk": 600, "coall_mv": None, "tdp_mw": None}
    b.gpu_current_freq = 700  # 此前乐观推进到了 700
    b._on_gpu_sent(600)       # reaper 失败回执：确认值=600
    assert b.gpu_current_freq == 600


def test_save_and_load_roundtrip():
    cfg = AppConfig()
    b = PowerBlock(cfg, MockRyzenAdj())
    b.tdp = 12
    b.gpu = 900
    b.tdp_lock = True
    b.save_config()
    assert cfg.power_config.tdp == 12
    assert cfg.power_config.tdp_lock is True
    b2 = PowerBlock(AppConfig(), MockRyzenAdj())
    b2.load_config()  # reads nothing yet
    # load from populated config
    cfg2 = AppConfig()
    cfg2.power_config = cfg.power_config
    b3 = PowerBlock(cfg2, MockRyzenAdj())
    b3.load_config()
    assert b3.tdp == 12
    assert b3.tdp_lock is True


def test_power_aliases_shared_params():
    """GPU 浮动的 EMA 系数与新鲜度窗口必须与 rtss 自动锁频共用同一份
    （core.float_params），保证两块调参只改一处。"""
    from ningbingjue.core.float_params import EMA_ALPHA, METRIC_STALE_SECS
    assert PowerBlock.FLOAT_EMA_ALPHA == EMA_ALPHA
    assert PowerBlock.GPU_UTIL_STALE_SECS == METRIC_STALE_SECS


def test_gpu_float_warns_on_counter_corruption():
    """回归：当 GPU PDH 计数器确认损坏（采样器运行 + 核心健康 + gpu_pct 永远
    读不到），GPU 浮动应提示用户跑 ``lodctr /r`` 重建计数器。

    验证三件事：
      1. 损坏签名持续 <20s 时只记录、不弹（躲开开机预热）；
      2. 用户主动开开关（force=True）立即弹一次；
      3. 同一会话 10min 内冷却，重复触发不再弹。
    """
    b = _block(gpu=800, gpu_float=True)
    b.gpu_current_freq = 600
    lbl = _with_toast_label(b)
    # 让 _read_gpu_util 返回「信号不可用」，从而进入 unknown 分支去跑损坏检测。
    b._read_gpu_util = lambda snap=None: (0.0, False)  # type: ignore[assignment]
    sampler = MetricSampler.instance()
    try:
        sampler._running = True
        sampler._state["ok"] = True
        sampler._state["ts"] = time.monotonic()
        sampler._state["gpu"] = None  # GPU 永远读不到
        sampler._state["cores"] = {f"cpu{i}": 5.0 for i in range(4)}  # 核心健康

        # ① 损坏刚出现（<20s），即便 tick 多次也不应弹 toast
        b.gpu_float_tick()
        assert "lodctr" not in lbl.text.lower(), "损坏未满 20s 不应弹窗"

        # ② 用户主动点开开关 => force 立即弹一次
        b._maybe_warn_gpu_counter_corrupted(force=True)
        assert "lodctr" in lbl.text.lower(), "force 路径必须立即弹 lodctr 引导"
        assert b._gpu_corrupt_warned_at > 0.0

        # ③ 冷却：10min 内重复触发（即便已持续损坏）不应再弹
        prev = lbl.text
        b._gpu_corrupt_since = time.monotonic() - 25.0  # 模拟已持续损坏
        b._maybe_warn_gpu_counter_corrupted(force=True)
        assert lbl.text == prev, "10min 冷却内不应重复弹窗"

        # ④ 非 force 的「持续 ≥20s」路径也应能弹（重置冷却后）
        b._gpu_corrupt_warned_at = 0.0
        b._gpu_corrupt_since = time.monotonic() - 25.0
        b.gpu_float_tick()
        assert "lodctr" in lbl.text.lower(), "持续损坏 ≥20s 必须经 tick 路径弹窗"
    finally:
        sampler._running = False
        sampler._state["gpu"] = None
        sampler._state["cores"] = None
        sampler._state["ok"] = False
        sampler._state["ts"] = 0.0


def test_gpu_float_no_warn_when_sampler_idle():
    """采样器整体未运行（如非掌机 / 未启动）时，不应把“没信号”误判为计数器
    损坏去弹 lodctr——那只会在确认 GPU 计数器单独损坏时才提示。"""
    b = _block(gpu=800, gpu_float=True)
    b.gpu_current_freq = 600
    lbl = _with_toast_label(b)
    b._read_gpu_util = lambda snap=None: (0.0, False)  # type: ignore[assignment]
    sampler = MetricSampler.instance()
    try:
        sampler._running = False  # 整体未运行
        sampler._state["ok"] = False
        sampler._state["gpu"] = None
        sampler._state["cores"] = None
        b._maybe_warn_gpu_counter_corrupted(force=True)
        assert "lodctr" not in lbl.text.lower(), "采样器未运行不应弹 lodctr"
    finally:
        sampler._running = False
        sampler._state["ok"] = False
        sampler._state["gpu"] = None
        sampler._state["cores"] = None


def test_gpu_float_reaper_success_from_stderr():
    """回归（2026-08-20 根因）：GPU 浮动走 silent fire-and-forget，成功与否由后台
    reaper 按 ``_invoke_ok`` 判定。reaper 之前只读 STDOUT、丢弃 STDERR，而掌机
    ryzenadj 把成功回执打到 STDERR（stdout 空 + 退出码非零）→ reaper 误判「失败」
    → 回滚 ``_applied`` 到天花板 + ``on_silent_sent`` 把显示拉回旧值 → 表现
    「GPU 浮动卡在最高上限不降」。手动路径(stdout+stderr 合并判定)却正常，故路径
    不对称即根因。修复：reaper 也合并 stdout+stderr 判定。本测试验证修复后浮动能
    正常降到 400、且不会因误判失败而累计触发熔断。
    """
    import ningbingjue.blocks.power as _powermod
    from ningbingjue.platform.ryzenadj import RyzenAdj

    class _FailModeRyzenAdj(RyzenAdj):
        @property
        def available(self):  # type: ignore[override]
            return True

        def invoke(self, args, silent=False, timeout=5.0, _prev_applied=None,
                   _applied_snapshot=None, **kw):
            if silent:
                # 模拟掌机 ryzenadj：stdout 空、成功回执在 STDERR、退出码非零
                # （不在 OK 集合）。修复前 reaper 只看 stdout → 误判失败。
                class _FP:
                    def communicate(self, timeout=None):
                        return ("", "Vanguard: Successfully set gfx clock to target\n")
                    def kill(self):
                        pass
                fp = _FP()
                fp.returncode = 1
                # 同步跑 reaper（与真实后台线程等价，便于确定性断言）
                self._reap_silent(fp, args, False, _prev_applied, _applied_snapshot)
                return (True, None, "")
            return (True, 0, "Successfully set gfx")

    cfg = AppConfig()
    cfg.power_config = PowerConfig(tdp=15, gpu=907, tdp_lock=True,
                                   gpu_lock=False, gpu_float_enabled=True)
    ra = _FailModeRyzenAdj("C:/fake/ryzenadj.exe")
    b = PowerBlock(cfg, ra)
    b._connect_sampler = lambda: None  # type: ignore[assignment]
    b.gpu_float_enabled = True
    b.gpu_current_freq = 907
    b._read_gpu_util = lambda snap=None: (5.0, True)  # type: ignore[assignment]

    # 分级降频走非 force 下发，受 1s 冷却节流；推进假时钟绕过。
    _clock = [10_000_000.0]
    _orig_time = _powermod.time.time
    try:
        _powermod.time.time = lambda: (_clock.__setitem__(0, _clock[0] + 2.0) or _clock[0])
        for _ in range(25):
            b.gpu_float_tick()
    finally:
        _powermod.time.time = _orig_time

    assert b.ryzenadj.circuit_open is False, (
        f"reaper 误判失败会累计触发熔断，实际 circuit_open={b.ryzenadj.circuit_open}")
    assert b.gpu_current_freq == 400, (
        f"reaper 从 stderr 判成功后，浮动应降到 400，实际 {b.gpu_current_freq}")
    assert b.ryzenadj._applied.get("gfx_clk") == 400, (
        f"硬件确认频率应降到 400，实际 {b.ryzenadj._applied.get('gfx_clk')}")


def test_gpu_float_busy_streak_backoff_warn():
    """回归（2026-08-20 第二根因）：跨进程互斥被另一实例长期占用（busy 连击）时，
    GPU 浮动每拍下发都 1.5s 超时 SKIP → 回滚天花板 → 频率卡死上限（日志铁证 freq
    恒 907 + applied 707↔907 振荡 + circuit 恒 False）。修复：busy 连击 ≥ 阈值触发
    退避（停止每拍锤锁）+ 挂起「另一实例占用」告警（面板弹 toast），且 busy 不算
    失败、不误熔断；退避到期恢复重试。
    """
    import ningbingjue.blocks.power as _powermod
    import ningbingjue.platform.ryzenadj as _rzmod
    from ningbingjue.platform.ryzenadj import RyzenAdj

    class _AlwaysBusyMutex:
        def try_acquire(self) -> str:  # noqa: D102
            return "busy"

    class _BlockRyzenAdj(RyzenAdj):
        @property
        def available(self):  # type: ignore[override]
            return True

    orig_mutex = _rzmod._SYS_MUTEX
    _rzmod._SYS_MUTEX = _AlwaysBusyMutex()
    _clock = [10_000_000.0]
    _orig_time = _powermod.time.time
    try:
        def _fake_time() -> float:
            _clock[0] += 0.4
            return _clock[0]

        _powermod.time.time = _fake_time

        cfg = AppConfig()
        cfg.power_config = PowerConfig(tdp=15, gpu=907, tdp_lock=True,
                                       gpu_lock=False, gpu_float_enabled=True)
        ra = _BlockRyzenAdj("C:/fake/ryzenadj.exe")
        b = PowerBlock(cfg, ra)
        b._connect_sampler = lambda: None  # type: ignore[assignment]
        b.gpu_float_enabled = True
        b.gpu_current_freq = 907

        # 连续 force 下发：互斥每拍 busy → 回滚天花板，freq 贴真不动
        warn_at = _powermod.PowerBlock.GPU_BUSY_STREAK_WARN
        for _ in range(warn_at + 2):
            b._send_gpu_clock(707, force=True, reason="tiered_down")
        assert b.gpu_current_freq == 907, (
            f"互斥被占时频率应贴真实值 907 不动，实际 {b.gpu_current_freq}")
        assert ra.circuit_open is False, "busy 不算失败 → 不应熔断"
        assert b.gpu_busy_streak >= warn_at, (
            f"busy 连击应达阈值，实际 {b.gpu_busy_streak}")
        assert b._gpu_busy_backoff_until > _clock[0], "应已进入退避"
        assert b._gpu_busy_warn_pending, "应已挂起「另一实例占用」告警"

        # 退避期内再次下发 → 直接 SEND_BACKOFF 返回，不再 set_desired/尝试 maybe_apply
        before = ra._desired.get("gfx_clk")
        b._send_gpu_clock(507, force=True, reason="tiered_down")
        assert ra._desired.get("gfx_clk") == before, (
            "退避期内不应再下发（停止锤锁）")

        # 退避到期 → 恢复重试（desired 更新为 507）
        _clock[0] = b._gpu_busy_backoff_until + 0.1
        b._send_gpu_clock(507, force=True, reason="tiered_down")
        assert ra._desired.get("gfx_clk") == 507, "退避到期应恢复重试"
    finally:
        _powermod.time.time = _orig_time
        _rzmod._SYS_MUTEX = orig_mutex


def test_gpu_float_reaper_cb_deterministic():
    """回归（2026-08-20）：reaper 的 on_silent_sent 回调值必须按【本次下发结果】
    确定性计算，而不是竞态读当前 _applied——若期间被更新的下发覆盖，旧代码会把
    别人的值当本次结果回调，显示被错误拉回。验证：
    ① 成功（stderr 回执）→ 回调本次目标 707；
    ② 失败（usage 文本）且快照未被接管 → 回滚到上一拍 907、回调 907；
    ③ 失败但快照已被更新下发接管 → 回滚跳过、不干预（None）。
    """
    from ningbingjue.platform.ryzenadj import RyzenAdj

    class _ReaperRyzenAdj(RyzenAdj):
        @property
        def available(self):  # type: ignore[override]
            return True

    ra = _ReaperRyzenAdj("C:/fake/ryzenadj.exe")
    cb_calls: list = []
    ra.on_silent_sent = lambda gfx: cb_calls.append(gfx)
    snapshot = {"gfx_clk": 707, "coall_mv": None, "tdp_mw": None}
    prev_applied = {"gfx_clk": 907, "coall_mv": None, "tdp_mw": None}

    class _FP:
        def __init__(self, out, err, code):
            self.out, self.err, self.returncode = out, err, code

        def communicate(self, timeout=None):
            return (self.out, self.err)

        def kill(self):
            pass

    # ① 成功 → 回调本次目标 707；_applied 保持乐观值（不回滚）
    with ra._state_lock:
        ra._applied = dict(snapshot)
    ra._reap_silent(_FP("", "Successfully set gfx clock to target\n", 1),
                    ["--gfx-clk", "707"], False, prev_applied, snapshot)
    assert cb_calls[-1] == 707, f"成功应回调本次目标 707，实际 {cb_calls[-1]}"
    assert ra._applied.get("gfx_clk") == 707

    # ② 失败（usage）且快照未被接管 → 回滚到上一拍 907、回调 907
    with ra._state_lock:
        ra._applied = dict(snapshot)
    ra._reap_silent(_FP("usage: ryzenadj --help\n", "", 1),
                    ["--gfx-clk", "707"], False, prev_applied, snapshot)
    assert cb_calls[-1] == 907, f"失败回滚应回调上一拍 907，实际 {cb_calls[-1]}"
    assert ra._applied.get("gfx_clk") == 907

    # ③ 失败但快照已被接管（applied 被更新的下发写成 507）→ 回滚跳过、不干预
    with ra._state_lock:
        ra._applied = {"gfx_clk": 507, "coall_mv": None, "tdp_mw": None}
    ra._reap_silent(_FP("usage: ryzenadj --help\n", "", 1),
                    ["--gfx-clk", "707"], False, prev_applied, snapshot)
    assert cb_calls[-1] is None, f"快照被接管时不应干预，实际 {cb_calls[-1]}"
    assert ra._applied.get("gfx_clk") == 507, "回滚应被跳过（由新下发的 reaper 负责）"


def test_gpu_float_toggle_off_writes_back_ceiling():
    """回归（2026-08-20 P1 审查）：浮动关闭后硬件仍停留在最后的浮动值（SMU 保留
    --gfx-clk 直到下次写入），UI 却显示 ceiling → 显示/硬件发散且无「恢复自动频率」
    出口。修复：关闭浮动 = 显式写回用户设定上限，显示/硬件/算法三者一致。
    """
    b = _block(gpu=907, gpu_float=True)
    # 模拟浮动已运行到低频率 400（display 与硬件侧 applied 都在 400）
    b.gpu_current_freq = 400
    b.ryzenadj.set_desired_gpu_clk(400)
    with b.ryzenadj._state_lock:
        b.ryzenadj._applied["gfx_clk"] = 400

    b.toggle_gpu_float()  # 关闭浮动

    assert b.gpu_float_enabled is False
    assert b.gpu_current_freq == 907, (
        f"关闭后显示应恢复 ceiling 907，实际 {b.gpu_current_freq}")
    assert b.ryzenadj._desired.get("gfx_clk") == 907, (
        "关闭后 desired 应写回 ceiling（否则硬件永远收不到新频率）")
    assert b.ryzenadj._applied.get("gfx_clk") == 907, (
        f"关闭后硬件侧 applied 应回到 ceiling 907，实际 {b.ryzenadj._applied.get('gfx_clk')}")


def test_gpu_float_unknown_hold_timeout_disables():
    """回归（2026-08-20 P2-7）：GPU 利用率信号持续不可用超阈值 → 自动关闭浮动
    并写回上限（不再无限 hold 在当前频率静默停摆）。短时不可用（采样器重启等）
    不触发。
    """
    import ningbingjue.blocks.power as _powermod

    b = _block(gpu=907, gpu_float=True)
    b._read_gpu_util = lambda snap=None: (0.0, False)  # 信号持续不可用
    _clock = [10_000_000.0]
    _orig_time = _powermod.time.time
    try:
        _powermod.time.time = lambda: (_clock.__setitem__(0, _clock[0] + 5.0) or _clock[0])
        # 短时不可用（2 拍 = 10s < 60s 阈值）→ 不应触发禁用
        for _ in range(2):
            b.gpu_float_tick()
        assert b.gpu_float_enabled is True, "短时信号不可用不应禁用浮动"
        # 累计超过 60s 阈值 → 后续拍触发自动禁用
        for _ in range(11):
            b.gpu_float_tick()
    finally:
        _powermod.time.time = _orig_time
    assert b.gpu_float_enabled is False, "信号持续不可用超时应自动关闭浮动"
    assert b.gpu_current_freq == 907, (
        f"自动禁用后应写回 ceiling 907，实际 {b.gpu_current_freq}")
    assert b.ryzenadj._applied.get("gfx_clk") == 907, (
        f"自动禁用后硬件侧 applied 应回到 907，实际 {b.ryzenadj._applied.get('gfx_clk')}")


def test_gpu_float_busy_warn_records_skip_reason():
    """回归（2026-08-20 P2-5）：busy 连击告警应记录本次 SKIP 原因，供日志/文案
    细分「另一实例占用」（busy-cross-process）vs 同进程繁忙（busy）——避免把
    自身挂起/争用误报成「另一实例」误导排查。
    """
    import ningbingjue.blocks.power as _powermod
    import ningbingjue.platform.ryzenadj as _rzmod
    from ningbingjue.platform.ryzenadj import RyzenAdj

    class _BusyMut:
        def try_acquire(self) -> str:  # noqa: D102
            return "busy"

    class _BlockRyzenAdj(RyzenAdj):
        @property
        def available(self):  # type: ignore[override]
            return True

    orig_mutex = _rzmod._SYS_MUTEX
    _rzmod._SYS_MUTEX = _BusyMut()
    _clock = [10_000_000.0]
    _orig_time = _powermod.time.time
    try:
        _powermod.time.time = lambda: (_clock.__setitem__(0, _clock[0] + 0.4) or _clock[0])
        cfg = AppConfig()
        cfg.power_config = PowerConfig(tdp=15, gpu=907, tdp_lock=True,
                                       gpu_lock=False, gpu_float_enabled=True)
        ra = _BlockRyzenAdj("C:/fake/ryzenadj.exe")
        b = PowerBlock(cfg, ra)
        b.dispose()  # 停掉 __init__ 自动启动的 worker（保持测试确定性）
        b.gpu_float_enabled = True
        b.gpu_current_freq = 907
        # 互斥桩返回 busy → invoke 记 busy-cross-process；连击达阈值触发告警
        for _ in range(_powermod.PowerBlock.GPU_BUSY_STREAK_WARN + 1):
            b._send_gpu_clock(707, force=True, reason="tiered_down")
        assert b.gpu_busy_streak >= _powermod.PowerBlock.GPU_BUSY_STREAK_WARN
        assert b._gpu_busy_warn_pending, "连击达阈值应挂起告警"
        assert ra._last_skip_reason == "busy-cross-process", (
            f"SKIP 原因应记录为 busy-cross-process，实际 {ra._last_skip_reason!r}")
    finally:
        _powermod.time.time = _orig_time
        _rzmod._SYS_MUTEX = orig_mutex

