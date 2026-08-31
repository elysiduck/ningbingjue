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


def test_off_button_shows_inactive_after_preset():
    """（Q1=B，2026-09-01）从未切过的按钮套预设后直接显示未激活色。

    只要预设定义了 ``InactiveBackColor``，套完预设就一律按当前开关态取色，
    不再要求按钮「曾切过一次」。用户已接受普通按钮与编辑器『常规』预览
    在此场景下的轻微不一致。
    """
    btn = _mk_button()
    btn.set_style("#7da2c8", False)
    _apply_style_to_ctrl(btn, PRESET)
    ss = btn.styleSheet()
    assert INACTIVE_BG in ss
    assert BASE_BG not in ss


def test_sparse_preset_plain_button_keeps_base_look():
    """稀疏预设（未定义 Inactive*/Active*）→ 普通按钮仍保持基础外观。

    兜底分支：``refresh_state_style`` 只有在预设定义了状态变体色时才按态
    取色；稀疏预设不写这些字段，按钮回落基础色（7-30 行为不变）。
    """
    sparse = ButtonStyle(
        cut_size=0, corner_radius=10, border_thickness=2,
        hover_only_border=False, hover_gradient=False, link_theme=False,
        colors={
            "BackColor": "FF1E2430", "HoverColor": "FF2A3242",
            "BorderColor": "FF5FAFEB", "ForeColor": "FFE6FF1E",
            "HoverForeColor": "FFFFFFFF",
        },
    )
    btn = _mk_button()
    btn.set_style("#7da2c8", False)
    _apply_style_to_ctrl(btn, sparse)
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


# ----------------------------------------------------------------------
# Q2=B（2026-09-01）：切角预设的「未激活背景/边框」不再是死字段。
# 此前 ``_paint_chamfer`` 的关态分支一律回落基础色，从不读
# ``_bs_inactive_back``/``_bs_inactive_border``；现与圆角预设行为对齐。
# ----------------------------------------------------------------------

CHAMFER_PRESET = ButtonStyle(
    cut_size=8, corner_radius=0, border_thickness=2,
    hover_only_border=False, hover_gradient=False, link_theme=False,
    colors={
        "BackColor": "FF1E2430", "HoverColor": "FF2A3242",
        "BorderColor": "FF5FAFEB", "ForeColor": "FFE6FF1E",
        "HoverForeColor": "FFFFFFFF",
        "ActiveBackColor": "FF00C853", "ActiveBorderColor": "FF00E676",
        "InactiveBackColor": "FF3A3F4B", "InactiveBorderColor": "FF5A6070",
    },
)
CHAMFER_INACTIVE = (58, 63, 75)   # FF3A3F4B
CHAMFER_ACTIVE = (0, 200, 83)     # FF00C853
CHAMFER_BASE = (30, 36, 48)       # FF1E2430


def _sample_center_right(btn) -> tuple[int, int, int]:
    """渲染按钮并取样右上区域内部一点（避开居中文字、切角与边框线）。"""
    from PyQt6.QtCore import QPoint

    pm = btn.grab()                      # grab() 强制走一次完整 paintEvent
    img = pm.toImage()
    d = max(1, int(pm.devicePixelRatio()))
    # 取样点：控件坐标 (w-8, 6) —— 在切角路径内部、避开 2px 边框与文字
    x = (btn.width() - 8) * d
    y = 6 * d
    c = img.pixelColor(QPoint(min(x, img.width() - 1), min(y, img.height() - 1)))
    return (c.red(), c.green(), c.blue())


def _close(actual, expect, tol=6) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(actual, expect))


def test_chamfer_off_state_uses_inactive_colors():
    """切角预设关态 → 自绘用 InactiveBackColor（此前是死字段、回落基础色）。"""
    btn = _mk_button()
    btn.set_style("#7da2c8", False)
    _apply_style_to_ctrl(btn, CHAMFER_PRESET)
    assert btn._bs_inactive_back is not None
    assert _close(_sample_center_right(btn), CHAMFER_INACTIVE), \
        f"off-state should use inactive back, got {_sample_center_right(btn)}"


def test_chamfer_on_state_still_uses_active_colors():
    """切角预设开态 → 激活色不回归（原本就正常，Q2 改动不得破坏）。"""
    btn = _mk_button()
    btn.set_style("#7da2c8", True)
    _apply_style_to_ctrl(btn, CHAMFER_PRESET)
    assert _close(_sample_center_right(btn), CHAMFER_ACTIVE), \
        f"on-state should use active back, got {_sample_center_right(btn)}"


def test_chamfer_sparse_off_state_falls_back_to_base():
    """稀疏切角预设（未定义 Inactive*）关态 → 仍回落基础色（向后兼容）。"""
    sparse = ButtonStyle(
        cut_size=8, corner_radius=0, border_thickness=2,
        hover_only_border=False, hover_gradient=False, link_theme=False,
        colors={
            "BackColor": "FF1E2430", "HoverColor": "FF2A3242",
            "BorderColor": "FF5FAFEB", "ForeColor": "FFE6FF1E",
            "HoverForeColor": "FFFFFFFF",
        },
    )
    btn = _mk_button()
    btn.set_style("#7da2c8", False)
    _apply_style_to_ctrl(btn, sparse)
    assert btn._bs_inactive_back is None
    assert _close(_sample_center_right(btn), CHAMFER_BASE), \
        f"sparse chamfer off-state should fall back to base, got {_sample_center_right(btn)}"
