"""回归：套用按钮预设不得丢失按钮当下的「开/关」状态外貌。

背景（用户反馈「应用了自定义按钮预设后，状态按钮要点两下才会出现状态外貌样式」）：

* ``styles._apply_style_to_ctrl`` 只把预设的**基础外观**（BackColor 那张样式表）
  推给按钮，不关心按钮当下是开还是关 —— 正处在『开』态的按钮被刷成基础色，
  看上去像「关」。
* 面板侧 ``panels._set()`` 用 ``_last`` 做了「状态没变就不重刷」的早退，每秒
  一次的 ``sync_all`` 走到这里直接 return，永远不会把状态外貌补回来。
* 结果：第 1 下点成『关』（仍无状态外貌），第 2 下点回『开』才经 ``set_style``
  走到 ``_apply_preset_variant`` —— 即「点两下才出现状态外貌样式」。

修复：``_apply_style_to_ctrl`` 套完预设后回调 ``HintButton.refresh_state_style()``，
按当前开关态补一次 Active*/Inactive* 变体（仅限确认是开关按钮的控件）。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from ningbingjue.core.color.styles import (  # noqa: E402
    ButtonStyle,
    _apply_style_to_ctrl,
)

# 一个「自定义圆角预设」：基础 + 激活(绿) + 未激活(灰) 三套配色
PRESET = ButtonStyle(
    cut_size=0, corner_radius=10, border_thickness=2,
    hover_only_border=False, hover_gradient=False, link_theme=False,
    colors={
        "BackColor": "FF1E2430", "HoverColor": "FF2A3242",
        "BorderColor": "FF5FAFEB", "ForeColor": "FFE6FF1E",
        "HoverForeColor": "FFFFFFFF",
        "ActiveBackColor": "FF00C853", "ActiveBorderColor": "FF00E676",
        "InactiveBackColor": "FF3A3F4B", "InactiveBorderColor": "FF5A6070",
    },
)

BASE_BG = "rgb(30,36,48)"
ACTIVE_BG = "rgb(0,200,83)"
INACTIVE_BG = "rgb(58,63,75)"

# 离屏 GUI 测试里窗口必须被强引用：局部变量被 GC 后 C++ 侧按钮随之销毁，
# 后续调用会抛 RuntimeError("wrapped C/C++ object ... has been deleted")。
_KEEP: list = []


def _mk_button():
    from PyQt6.QtWidgets import QVBoxLayout, QWidget

    from ningbingjue.ui.gamepad_hint import HintButton

    win = QWidget()
    lay = QVBoxLayout(win)
    btn = HintButton("锁帧: 关")
    lay.addWidget(btn)
    win.show()
    _KEEP.append(win)
    return btn


def test_preset_apply_keeps_active_state():
    """套预设时按钮正处于『开』态 → 立刻显示激活色（不是基础色）。"""
    btn = _mk_button()
    btn.set_style("#7da2c8", True)
    _apply_style_to_ctrl(btn, PRESET)
    assert ACTIVE_BG in btn.styleSheet()
    assert BASE_BG not in btn.styleSheet()


def test_preset_apply_keeps_inactive_state_for_toggle():
    """开关按钮处于『关』态 → 显示未激活色（与切换一次后的外貌一致）。"""
    btn = _mk_button()
    btn.set_style("#7da2c8", True)     # 先进入过『开』态 → 判定为开关按钮
    btn.set_style("#7da2c8", False)    # 再切回『关』
    _apply_style_to_ctrl(btn, PRESET)
    assert INACTIVE_BG in btn.styleSheet()


def test_plain_button_keeps_base_look_after_preset():
    """普通按钮（从未进入『开』态）→ 仍是基础外观，与编辑器『常规』预览一致。"""
    btn = _mk_button()
    btn.set_style("#7da2c8", False)
    _apply_style_to_ctrl(btn, PRESET)
    ss = btn.styleSheet()
    assert BASE_BG in ss
    assert INACTIVE_BG not in ss
    assert ACTIVE_BG not in ss


def test_toggle_still_switches_after_preset():
    """套预设后点击切换仍然生效（修的是「初始外貌」，不能破坏切换本身）。"""
    btn = _mk_button()
    btn.set_style("#7da2c8", False)
    _apply_style_to_ctrl(btn, PRESET)

    btn.set_style("#7da2c8", True)     # 第 1 下 -> 开
    assert ACTIVE_BG in btn.styleSheet()

    btn.set_style("#7da2c8", False)    # 第 2 下 -> 关
    assert INACTIVE_BG in btn.styleSheet()


def test_state_style_survives_repeated_preset_apply():
    """反复套预设（绑定/覆盖/重建都会触发）不应让状态外貌漂移。"""
    btn = _mk_button()
    btn.set_style("#7da2c8", True)
    for _ in range(3):
        _apply_style_to_ctrl(btn, PRESET)
        assert ACTIVE_BG in btn.styleSheet()
