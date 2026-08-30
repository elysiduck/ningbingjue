"""滑块应用方式测试：鼠标 vs 手柄/键盘方向键。

需求：调整滑块值后**不**自动应用；
  - 手柄/键盘方向键微调 → 等「再摁一次 A / Enter」确认才下发；
  - 鼠标拖动 → 防抖（最后调节后延迟）自动下发。

验证点：
  1. 鼠标路径（_on_* 且非 nudge）在「会下发」的条件下启动防抖定时器，到点 flush 恰好下发 1 次；
  2. 手柄/键盘微调路径（_nudge_*）**不**启动定时器、**不**下发；
  3. 微调后再调用确认回调（_on_slider_confirm / _confirm_apply）恰好下发 1 次；
  4. 风扇「未 applied」或「auto/curve 模式」下鼠标拖动也不自动下发（保持原语义）。

offscreen 下 QTimer 自然触发不可靠，测试不依赖定时器实际到点——手动停定时器后调
_flush_apply 模拟「到点」。
"""

from __future__ import annotations

import pytest
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication

import ningbingjue.platform.ec_io as ec_io_mod
from ningbingjue.ui.panels import FanPanel, RtssPanel


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _patch_ec_diag(monkeypatch):
    # FanPanel._setup 末尾调 _refresh_diag → ec_io.diag_info()；headless 下用安全值替代，
    # 避免真实探测 WinRing0 / 管理员状态。
    monkeypatch.setattr(
        ec_io_mod, "diag_info",
        lambda: {"is_admin": True, "getdllstatus": 0, "method": "ols",
                 "dll_path": "x", "last_error": None, "last_error_code": 0},
    )


class _FakeRtss:
    FPS_MIN = 20
    FPS_MAX = 60

    def __init__(self) -> None:
        self.lock_enabled = False
        self.lock_applied = False
        self.fps = 30
        self.auto_active = False
        self.auto_lock_enabled = False
        self.auto_target = 60
        self.auto_gpu_load_pct = 50
        self.apply_calls = 0

    def save_config(self) -> None:
        pass

    def update_slider_ui(self, v: int) -> None:
        self.fps = v

    def apply_lock(self, silent: bool = False) -> None:
        self.apply_calls += 1

    def auto_lock_available(self) -> bool:
        return True

    def rtss_status(self):
        return ("missing", "状态未知")


class _FakeFan:
    PWM_MIN = 0
    PWM_MAX = 255

    def __init__(self) -> None:
        self.mode = "manual"
        self.manual_pct = 128
        self.silent_pct = 0
        self.applied = True
        self.apply_calls = 0

    def set_manual_pct(self, v: int) -> None:
        self.manual_pct = v

    def set_silent_pct(self, v: int) -> None:
        self.silent_pct = v

    def apply(self, silent: bool = False) -> bool:
        self.apply_calls += 1
        return True

    def fan_rpm_display(self) -> str:
        return "0 RPM"

    last_temp_c = None


def _make_rtss(qapp, lock_enabled: bool) -> tuple[RtssPanel, _FakeRtss]:
    block = _FakeRtss()
    block.lock_enabled = lock_enabled
    block.lock_applied = lock_enabled
    panel = RtssPanel(block, "锁帧", QColor("#4be38a"))
    return panel, block


def _make_fan(qapp, mode: str = "manual", applied: bool = True) -> tuple[FanPanel, _FakeFan]:
    block = _FakeFan()
    block.mode = mode
    block.applied = applied
    panel = FanPanel(block, "风扇", QColor("#4be38a"))
    return panel, block


def _simulate_timer_fire(panel) -> None:
    """模拟单发 QTimer 到点的真实行为：Qt 先停定时器、再 emit timeout。"""
    panel._apply_timer.stop()
    panel._flush_apply()


# -- RTSS ----------------------------------------------------------------
def test_rtss_mouse_drag_arms_timer_and_flushes_once(qapp):
    """鼠标拖动（非 nudge）且锁开启：启动防抖，到点恰好下发 1 次。"""
    panel, block = _make_rtss(qapp, lock_enabled=True)
    panel._on_slide(45)  # 鼠标路径：_nudging 默认 False
    assert panel._apply_timer.isActive(), "鼠标拖动应启动防抖定时器"
    _simulate_timer_fire(panel)
    assert block.apply_calls == 1, "防抖到点应下发 1 次 RTSS 锁帧命令"


def test_rtss_keyboard_nudge_does_not_arm_timer(qapp):
    """手柄/键盘方向键微调：绝不启动定时器、绝不下发。"""
    panel, block = _make_rtss(qapp, lock_enabled=True)
    moved = panel._nudge_fps(1)
    assert moved
    assert not panel._apply_timer.isActive(), "键盘微调不应启动防抖定时器"
    assert block.apply_calls == 0, "键盘微调不应下发"


def test_rtss_keyboard_nudge_then_confirm_applies(qapp):
    """手柄/键盘微调后「再摁一次 A / Enter」确认 → 下发 1 次。"""
    panel, block = _make_rtss(qapp, lock_enabled=True)
    panel._nudge_fps(1)
    assert not panel._apply_timer.isActive()
    panel._on_slider_confirm()  # 用户「再摁一次 A / Enter」
    assert block.apply_calls == 1


# -- FAN -----------------------------------------------------------------
def test_fan_mouse_drag_debounce_applies(qapp):
    """鼠标拖动（manual 且已 applied）：启动防抖，到点下发 1 次。"""
    panel, block = _make_fan(qapp, mode="manual", applied=True)
    panel._on_pwm(200)  # 鼠标路径
    assert panel._apply_timer.isActive(), "鼠标拖动应启动防抖定时器"
    _simulate_timer_fire(panel)
    assert block.apply_calls == 1, "防抖到点应写 EC 1 次"


def test_fan_keyboard_nudge_does_not_arm_timer(qapp):
    """手柄/键盘方向键微调：绝不启动定时器、绝不写 EC。"""
    panel, block = _make_fan(qapp, mode="manual", applied=True)
    moved = panel._nudge_pwm(1)
    assert moved
    assert not panel._apply_timer.isActive(), "键盘微调不应启动防抖定时器"
    assert block.apply_calls == 0, "键盘微调不应写 EC"


def test_fan_keyboard_nudge_then_confirm_applies(qapp):
    """手柄/键盘微调后「再摁一次 A / Enter」确认 → 写 EC 1 次。"""
    panel, block = _make_fan(qapp, mode="manual", applied=True)
    panel._nudge_pwm(1)
    assert not panel._apply_timer.isActive()
    panel._confirm_apply()  # 用户「再摁一次 A / Enter」
    assert block.apply_calls == 1


def test_fan_mouse_drag_no_apply_when_not_applied(qapp):
    """未 applied 时鼠标拖动也不自动下发（要按「应用」按钮）。"""
    panel, block = _make_fan(qapp, mode="manual", applied=False)
    panel._on_pwm(200)
    assert not panel._apply_timer.isActive(), "未 applied 不应启动防抖"
    assert block.apply_calls == 0


def test_fan_mouse_drag_no_apply_in_auto_mode(qapp):
    """auto 模式下滑块不生效，鼠标拖动也不自动下发。"""
    panel, block = _make_fan(qapp, mode="auto", applied=True)
    panel._on_pwm(200)
    assert not panel._apply_timer.isActive(), "auto 模式不应启动防抖"
    assert block.apply_calls == 0
