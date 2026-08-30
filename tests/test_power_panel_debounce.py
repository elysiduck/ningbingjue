"""PowerPanel 滑块防抖测试。

回归：拖动 TDP/GPU 滑块期间不能每次 valueChanged 都启动一个 ryzenadj 进程
（subprocess + WinRing0 互斥占用，与降压/风扇卡片共享同一把互斥，频繁抢占
会卡 UI）。修复后 _on_tdp/_on_gpu 只启动/重置一个单发 QTimer，最后一次调节
后 POWER_APPLY_DEBOUNCE_MS 才真正调用 apply_power。

offscreen 下 QTimer 自然触发不可靠（memory: 离屏 Qt 测试陷阱），本测试**不
依赖定时器实际到点**——只验证：
  1. 连续 _on_tdp/_on_gpu 期间 apply_power 未被调用，定时器处于活动状态
  2. 手动 _flush_apply() 后 apply_power 恰好被调用 1 次（合并下发）
  3. _confirm_apply 立即 apply_power + 取消定时器
  4. _confirm_tdp_lock/_confirm_gpu_lock/_confirm_gpu_float 取消悬挂的定时器
  5. 关锁状态下滑动不启动定时器
  6. 关锁后 _flush_apply 不应下发（保持原 _on_* 的"关锁不下发"语义）
"""

from __future__ import annotations

import pytest
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication

from ningbingjue.blocks.power import PowerBlock
from ningbingjue.core.config import AppConfig, PowerConfig
from ningbingjue.platform.ryzenadj import MockRyzenAdj
from ningbingjue.ui.panels import PowerPanel


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _make_panel(tdp_lock: bool = False, gpu_lock: bool = False):
    """构造一个真实 PowerPanel + PowerBlock + MockRyzenAdj。

    把 ``panel.instance.apply_power`` 替换成计数 lambda，使测试可以断言
    「真正下发 ryzenadj 的次数」而无需启动 ryzenadj.exe 子进程。
    """
    cfg = AppConfig()
    cfg.power_config = PowerConfig(
        tdp=15, gpu=800, tdp_lock=tdp_lock, gpu_lock=gpu_lock,
    )
    block = PowerBlock(cfg, MockRyzenAdj())
    panel = PowerPanel(block, "性能", QColor("#4be38a"))
    calls: list[int] = []
    # apply_power 返回 True（与真实实现一致：成功时 maybe_apply → True）。
    panel.instance.apply_power = lambda *a, **k: calls.append(1) or True  # type: ignore
    return panel, calls


# -- 1. 拖动期间不下发 --------------------------------------------------
def test_debounce_does_not_fire_during_drag(qapp):
    """连续 5 次 _on_tdp（拖动期间）apply_power 不被调用，定时器处于活动状态。"""
    panel, calls = _make_panel(tdp_lock=True)
    for v in range(16, 21):  # 5 次连续调节
        panel._on_tdp(v)
    assert calls == [], f"拖动期间不应下发 ryzenadj，实际调用 {len(calls)} 次"
    assert panel._apply_timer.isActive(), "防抖定时器应在拖动期间保持活动"


# -- 2. flush 后合并下发一次 -------------------------------------------
def test_flush_apply_fires_once_after_drag(qapp):
    """_flush_apply 后 apply_power 恰好被调用 1 次（5 次拖动合并为 1 次下发）。

    注意：``_flush_apply`` 在生产环境是被 ``QTimer.timeout`` 信号触发的，
    singleShot 模式下到点后 Qt 会自动停止定时器——所以 ``_flush_apply`` 自身
    不需要显式 ``stop()``。本测试模拟「定时器到点」时先停定时器再调
    ``_flush_apply``，复现 Qt 事件循环的真实行为。
    """
    panel, calls = _make_panel(tdp_lock=True)
    for v in range(16, 21):
        panel._on_tdp(v)
    # 模拟 QTimer singleShot 到点的真实行为：Qt 先停定时器、再 emit timeout
    panel._apply_timer.stop()
    panel._flush_apply()
    assert len(calls) == 1, f"5 次拖动后应合并下发 1 次，实际 {len(calls)} 次"
    assert not panel._apply_timer.isActive(), "到点后定时器应已停止"


# -- 3. 「应用」按钮立即下发 + 取消防抖 ---------------------------------
def test_confirm_apply_cancels_debounce_and_fires_immediately(qapp):
    """用户按「应用」按钮：取消防抖、立即下发。"""
    panel, calls = _make_panel(tdp_lock=True)
    panel._on_tdp(18)  # 启动防抖
    assert panel._apply_timer.isActive()
    panel._confirm_apply()
    assert len(calls) == 1, "_confirm_apply 应立即下发一次"
    assert not panel._apply_timer.isActive(), "_confirm_apply 应取消防抖定时器"


# -- 4. 锁开关取消悬挂的延迟下发 ---------------------------------------
def test_confirm_tdp_lock_cancels_pending_debounce(qapp):
    """关 TDP 锁后悬挂的延迟下发应被取消；之后 _flush_apply 不再触发 apply_power。"""
    panel, calls = _make_panel(tdp_lock=True)
    panel._on_tdp(18)  # 启动防抖
    assert panel._apply_timer.isActive()
    panel._confirm_tdp_lock()  # 关锁 → 取消定时器
    assert not panel._apply_timer.isActive()
    # 关锁后 _flush_apply 不应下发（保持原 _on_* 的"关锁不下发"语义）
    panel._flush_apply()
    assert calls == [], "关锁后 _flush_apply 不应下发 ryzenadj"


def test_confirm_gpu_lock_cancels_pending_debounce(qapp):
    panel, _ = _make_panel(gpu_lock=True)
    panel._on_gpu(850)  # 启动防抖
    assert panel._apply_timer.isActive()
    panel._confirm_gpu_lock()  # 关锁 → 取消定时器
    assert not panel._apply_timer.isActive()


def test_confirm_gpu_float_cancels_pending_debounce(qapp):
    """开/关 GPU 浮动是一次性明确操作：应取消任何悬挂的延迟下发。"""
    panel, _ = _make_panel(tdp_lock=True)
    panel._on_tdp(18)
    assert panel._apply_timer.isActive()
    panel._confirm_gpu_float()
    assert not panel._apply_timer.isActive()


# -- 5. 关锁状态下滑动不启动定时器 -------------------------------------
def test_slider_drag_with_no_lock_does_not_arm_timer(qapp):
    """关锁状态下滑动只更新 UI，不启动防抖定时器、不下发。"""
    panel, calls = _make_panel(tdp_lock=False, gpu_lock=False)
    panel._on_tdp(20)
    assert not panel._apply_timer.isActive(), "关锁状态不应启动防抖"
    assert calls == [], "关锁状态不应下发"


def test_gpu_slider_drag_arms_timer_when_gpu_lock(qapp):
    """GPU 锁开启时拖 GPU 滑块同样应启动防抖（与 TDP 同款 UX）。"""
    panel, calls = _make_panel(gpu_lock=True)
    panel._on_gpu(850)
    assert panel._apply_timer.isActive()
    panel._flush_apply()
    assert len(calls) == 1


# -- 6. tdp 与 gpu 共用同一把防抖定时器（合并下发）----------------------
def test_tdp_and_gpu_share_single_debounce_timer(qapp):
    """TDP 滑块与 GPU 滑块共用一个 _apply_timer：拖 TDP 后拖 GPU 不会起两个
    定时器，最后一次调节后一次 flush 即可合并下发。"""
    panel, calls = _make_panel(tdp_lock=True, gpu_lock=True)
    panel._on_tdp(18)
    panel._on_gpu(850)
    panel._on_tdp(19)  # 再调一次 TDP
    assert panel._apply_timer.isActive()
    panel._flush_apply()
    assert len(calls) == 1, "TDP+GPU 交叉调节应合并为 1 次下发"


# -- 7. 手柄/键盘方向键微调（nudge）：不自动下发，等确认 ------------------
def test_keyboard_nudge_does_not_arm_timer(qapp):
    """手柄/键盘方向键微调 TDP：绝不自动下发、也不启动防抖定时器。"""
    panel, calls = _make_panel(tdp_lock=True)
    moved = panel._nudge_tdp(1)  # 方向键 +1 步
    assert moved, "nudge 应成功改变值"
    assert not panel._apply_timer.isActive(), "键盘微调不应启动防抖定时器"
    assert calls == [], "键盘微调不应下发 ryzenadj"


def test_keyboard_nudge_then_confirm_applies(qapp):
    """手柄/键盘微调后「再摁一次 Enter/A」确认 → 立即下发一次。"""
    panel, calls = _make_panel(tdp_lock=True)
    panel._nudge_tdp(1)  # 方向键微调（_nudging=True）
    assert not panel._apply_timer.isActive()
    panel._confirm_apply()  # 用户「再摁一次 Enter/A」确认
    assert len(calls) == 1, "确认后应下发 1 次 ryzenadj"


def test_keyboard_nudge_gpu_does_not_arm_timer(qapp):
    """手柄/键盘方向键微调 GPU：同样不自动下发、不启动定时器。"""
    panel, calls = _make_panel(gpu_lock=True)
    panel._nudge_gpu(1)
    assert not panel._apply_timer.isActive()
    assert calls == []


def test_keyboard_nudge_gpu_then_confirm_applies(qapp):
    """手柄/键盘微调 GPU 后确认 → 立即下发一次。"""
    panel, calls = _make_panel(gpu_lock=True)
    panel._nudge_gpu(1)
    panel._confirm_apply()
    assert len(calls) == 1
