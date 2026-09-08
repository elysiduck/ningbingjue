"""「应用」按钮强制重下发 + 三态反馈 回归测试。

**与 test_power_panel_debounce.py 的区别**：那个文件把 ``apply_power`` 整个换成
计数 lambda，只验证「面板有没有调用 apply_power」；本文件**保留真实的
``PowerBlock.apply_power`` 与 ``RyzenAdj.maybe_apply`` 脏检查**，只把最底层的
``invoke`` 换成计数器，因此能断言「ryzenadj 到底被真正调用了多少次」。

回归的 bug（2026-09-08）：``maybe_apply`` 的
``if not force and self._desired == self._applied: return True`` 幂等短路，配合
``_applied`` 是 invoke 之前写下的**乐观快照**，导致：
  * 拖完滑块（防抖 flush 已下发）再点「应用」→ 0 次下发却返回 True；
  * 休眠恢复重发 ``on_power_resume`` → 0 次下发却返回 True；
  * 两锁都关时 desired 全 None → 参数为空照样返回 True，UI 弹「✓ 已应用」假成功。
修复：``apply_power(force=...)`` + 空参数返回 False + 三态 toast。
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


def _make(tdp_lock: bool = False, gpu_lock: bool = False,
          tdp: int = 15, gpu: int = 800, gpu_float: bool = False):
    """真实 PowerBlock + PowerPanel，只把底层 invoke 换成计数器。"""
    cfg = AppConfig()
    cfg.power_config = PowerConfig(tdp=tdp, gpu=gpu, tdp_lock=tdp_lock,
                                   gpu_lock=gpu_lock,
                                   gpu_float_enabled=gpu_float)
    rj = MockRyzenAdj()
    blk = PowerBlock(cfg, rj)
    calls: list[list[str]] = []
    orig = rj.invoke

    def _spy(args, silent=False, timeout=5.0, **kw):
        calls.append(list(args))
        return orig(args, silent=silent, timeout=timeout, **kw)

    rj.invoke = _spy  # type: ignore[method-assign]
    toasts: list[tuple[str, bool]] = []
    blk.show_toast = lambda m, success=True: toasts.append((m, success))  # type: ignore[method-assign]
    panel = PowerPanel(blk, "性能", QColor("#4be38a"))
    return blk, rj, panel, calls, toasts


# -- 1. 核心回归：拖完滑块再点「应用」，必须真下发 -----------------------
def test_apply_button_forces_real_invoke_after_slider_flush(qapp):
    """拖滑块 → 防抖 flush 已下发 → 再点「应用」：必须再真发一次（不是 0 次）。

    修复前：desired 已被 flush 写进 _applied（乐观快照），脏检查短路 → 0 次，
    却返回 True 弹「✓ 已应用」——用户看到的「应用按钮无效」。
    """
    blk, _rj, panel, calls, toasts = _make(tdp_lock=True, gpu_lock=True)
    panel._on_tdp(25)                 # 鼠标拖动
    panel._apply_timer.stop()
    panel._flush_apply()              # 模拟 700ms 到点
    assert len(calls) == 1, "滑块防抖应下发 1 次"

    n = len(calls)
    toasts.clear()
    panel._confirm_apply()            # 用户按「应用」
    assert len(calls) == n + 1, "应用按钮必须真的再下发一次（force=True）"
    assert blk.applied is True
    assert toasts and toasts[-1][0].startswith("✓ 已下发"), toasts


# -- 2. 连点两次都要真的发 ----------------------------------------------
def test_apply_button_fires_every_press(qapp):
    """连点两次「应用」→ 两次真下发（强制语义，不做幂等短路）。"""
    _blk, _rj, panel, calls, _t = _make(tdp_lock=True, gpu_lock=True)
    panel._confirm_apply()
    assert len(calls) == 1
    panel._confirm_apply()
    assert len(calls) == 2, "每次点「应用」都应真下发一次"


# -- 3. 两锁都关：不下发，且不能报成功 -----------------------------------
def test_apply_with_no_lock_sends_nothing_and_reports_failure(qapp):
    """两锁都关 → 参数为空 → 0 次下发，返回 False，applied 不置 True。

    修复前：走 maybe_apply 拿到 True，弹「✓ 已应用  cmd: （无参数）」假成功。
    """
    blk, _rj, panel, calls, toasts = _make(tdp_lock=False, gpu_lock=False)
    panel._on_tdp(25)                 # 关锁滑动只改 UI
    assert calls == [], "关锁滑动不应下发"

    ok = blk.apply_power()
    assert calls == [], "两锁都关时不应下发任何 ryzenadj"
    assert ok is False, "空参数必须返回 False，绝不能假装成功"
    assert blk.applied is False
    assert toasts and "两锁均未开启" in toasts[-1][0] and toasts[-1][1] is False


def test_apply_button_with_no_lock_shows_unlocked_corner(qapp):
    """关锁点「应用」：角标回到「未锁定」，而不是「✓ 已应用」。"""
    _blk, _rj, panel, _calls, _t = _make(tdp_lock=False, gpu_lock=False)
    panel._confirm_apply()
    panel.sync()
    assert panel._corner_lbl.text() == "未锁定", panel._corner_lbl.text()


# -- 4. 休眠恢复重发必须强制 --------------------------------------------
def test_on_power_resume_forces_reapply(qapp):
    """休眠后 SMU 已重置，但 _applied 快照仍说"已应用" → 必须 force 才发得出去。

    修复前：``on_power_resume`` 返回 True 但 invoke 次数 0 —— 功能完全失效。
    """
    _blk, _rj, panel, calls, _t = _make(tdp_lock=True, gpu_lock=True)
    panel._on_tdp(20)
    panel._apply_timer.stop()
    panel._flush_apply()
    assert len(calls) == 1

    n = len(calls)
    assert _blk.applied is True
    assert _blk.on_power_resume() is True
    assert len(calls) == n + 1, "休眠恢复必须强制重发一次"


def test_on_power_resume_noop_when_never_applied(qapp):
    """从未应用过 → no-op，绝不擅自下发用户未启用的配置。"""
    blk, _rj, _panel, calls, _t = _make(tdp_lock=True, gpu_lock=True)
    assert blk.on_power_resume() is None
    assert calls == []


# -- 5. 滑块防抖路径保持幂等（不能被 force 污染）------------------------
def test_slider_flush_stays_idempotent(qapp):
    """连续两次 flush 且期间无新改动 → 第 2 次不应重复起 ryzenadj 进程。

    保证「force 只加在应用按钮 / 休眠恢复，不污染滑块防抖」。
    """
    _blk, _rj, panel, calls, _t = _make(tdp_lock=True, gpu_lock=True)
    panel._on_tdp(25)
    panel._flush_apply()
    assert len(calls) == 1
    panel._flush_apply()              # 无新改动
    assert len(calls) == 1, "滑块防抖路径必须保持幂等"


# -- 6. 失败也要说真话 --------------------------------------------------
def test_apply_failure_clears_applied_flag(qapp):
    """invoke 真实失败 → 返回 False，applied 置回 False（角标不再停在「✓ 已应用」）。"""
    blk, rj, panel, _calls, toasts = _make(tdp_lock=True, gpu_lock=True)
    rj._ok = False                    # 让 invoke 返回失败
    ok = blk.apply_power()
    assert ok is False
    assert blk.applied is False
    assert toasts and toasts[-1][0].startswith("✗") and toasts[-1][1] is False


# -- 7. 真实点击链路（apply.clicked → controller → confirm → invoke）----
def test_real_button_click_reaches_ryzenadj(qapp):
    """端到端：真实 QPushButton.click() 必须走到 ryzenadj.invoke。

    证明「无效」不是信号/导航问题（那条链路一直是通的），而是下发层被短路。
    """

    class _StubController:
        def activate_widget(self, widget, confirm=True):
            if not confirm:
                return
            for it in panel.nav_items():
                if it.widget is widget:
                    it.confirm()
                    return

    _blk, _rj, panel, calls, _t = _make(tdp_lock=True, gpu_lock=True)
    panel.controller = _StubController()
    panel._on_tdp(25)
    panel._apply_timer.stop()
    panel._flush_apply()
    n = len(calls)
    panel.apply.click()               # 真实点击
    assert len(calls) == n + 1, "点击「应用」按钮必须真的下发 ryzenadj"
