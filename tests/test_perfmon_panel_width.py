"""Regression test: PerfMonPanel's QGridLayout must respect the panel width.

之前的实现给标签/数值各列 setFixedWidth(56/130) + setColumnMinimumWidth，
把网格硬定成 384px 一旦主窗口内容区 < 384px，整张 perfmon 卡片就会撑出右
边界，**连带**把同一页面里所有其他卡片（rtss / power / freeze / …）一并
裁掉（外层 QVBoxLayout 取最大子宽度），触发"自动锁帧"、"TDP 锁"、"GPU 锁"
按钮被右边界切掉这一类连锁 bug。

修复：把 setFixedWidth 改为 setMinimumWidth + setColumnStretch，让列宽跟随
窗口可用空间变化（最小 50/30）；值单元格用 _NoWrapElideLabel —— 单行显示、
绝不换行，超宽时尾部省略，因此网格占满整张卡片宽度（无左右留白），也绝不会
把卡片撑出窗口。
"""

from __future__ import annotations

import os

import pytest
from PyQt6.QtWidgets import QApplication, QGridLayout, QLabel, QSizePolicy, QWidget

from ningbingjue.ui.panels import PerfMonPanel, _NoWrapElideLabel


# ---------------------------------------------------------------------------
# test fixture: a single Application is fine; qapp is shared per test module.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class _Stub:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
    def __getattr__(self, k):
        return None


def _make_perfmon():
    """Build a PerfMonPanel with stub data; safe to call repeatedly."""
    inst = _Stub(
        ui={},
        snapshot={
            "window": "python.exe",
            "wtype": "其它程序",
            "is_game": False,
            "cpu_avg": 35, "cpu_max": 64, "gpu_avg": 9,
            "load_label": "重载",
            "can_drop": True,
            "fps_state": "已锁帧 30 FPS",
            "gpu_lock_mhz": 855,
            "undervolt_state": "CO=-50 (-50未验证)",
            "undervolt_heavy_validated": False,
            "gpu_lock_status": "已锁",
        },
    )
    panel = PerfMonPanel(inst, title="perfmon", color="#e6ff1e",
                          block_key="perfmon", cfg=None)
    try:
        panel.sync()
    except Exception:
        pass
    return panel


# ---------------------------------------------------------------------------
# the regression tests
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("host_width", [300, 360, 420, 500])
def test_perfmon_grid_minimum_stays_below_host_width(qapp, host_width):
    """At any reasonable HUD width the perfmon panel must not force a minimum
    wider than what fits in the window."""
    panel = _make_perfmon()
    assert panel.minimumSizeHint().width() <= host_width, (
        f"PerfMonPanel minimum width {panel.minimumSizeHint().width()} "
        f"exceeds host width {host_width}"
    )


def test_perfmon_grid_uses_setFixedWidth_only_for_labels(qapp):
    """The grid must NOT use setFixedWidth(130) on the *value* cells — that's
    what forces the 384px minimum that overflows the window.  Label cells can
    stay Fixed (their text is fixed-width) but value cells must be
    Preferred/Expanding so they shrink to their minimum and wrap long text."""
    panel = _make_perfmon()
    grid = panel.findChild(QGridLayout)
    assert grid is not None
    offenders: list[str] = []
    for row in range(grid.rowCount()):
        for col in (1, 3):  # value columns only
            item = grid.itemAtPosition(row, col)
            if item is None:
                continue
            w = item.widget()
            if w is None:
                continue
            sp = w.sizePolicy()
            if sp.horizontalPolicy() == QSizePolicy.Policy.Fixed:
                offenders.append(
                    f"grid[{row},{col}]={type(w).__name__}({w.text()!r}) "
                    f"hPolicy=Fixed"
                )
    assert not offenders, (
        "Value cells must not be Fixed-width; offenders:\n" + "\n".join(offenders)
    )


def test_perfmon_grid_uses_qgridlayout_with_stretchable_value_cols(qapp):
    """The 4-column grid must declare setColumnStretch(1, 1) and (3, 1) so
    that value columns absorb excess width on wide screens and shrink to their
    minimum on narrow screens."""
    panel = _make_perfmon()
    grid = panel.findChild(QGridLayout)
    assert grid is not None, "expected a QGridLayout somewhere under panel"
    # Value columns are indices 1 and 3.
    assert grid.columnStretch(1) >= 1, "value col 1 must have stretch ≥ 1"
    assert grid.columnStretch(3) >= 1, "value col 3 must have stretch ≥ 1"
    # Label columns are indices 0 and 2 — must have stretch 0 (don't compete).
    assert grid.columnStretch(0) == 0
    assert grid.columnStretch(2) == 0
    # And minimums must be modest (50 for label, 30 for value) — not the old
    # 56 / 130 which would still force a 384px grid.
    assert grid.columnMinimumWidth(0) == 50
    assert grid.columnMinimumWidth(1) == 30
    assert grid.columnMinimumWidth(2) == 50
    assert grid.columnMinimumWidth(3) == 30


def test_perfmon_value_labels_single_line(qapp):
    """Every value cell must be a _NoWrapElideLabel that stays on ONE line
    (wordWrap=False). Long status strings ('已锁帧 30 FPS' / 'CO=-50 (-50未验证)')
    must NOT wrap — they elide with '…' instead, so the card never grows taller
    and never wraps content onto a second line."""
    panel = _make_perfmon()
    val_labels = [
        c for c in panel.findChildren(_NoWrapElideLabel)
    ]
    # Five left + five right = 10 value labels in the grid; all of them must
    # be single-line (no wrapping).
    assert len(val_labels) >= 10, (
        f"expected ≥10 single-line value labels, found {len(val_labels)}"
    )
    for lab in val_labels:
        assert not lab.wordWrap(), (
            f"value label {lab._full!r} must not word-wrap"
        )


def test_perfmon_grid_fills_card_width(qapp):
    """The grid must OCCUPY the full card content width — no leftover stretch on
    either side (the old hcenter wrapper centered the grid and left big left/right
    gaps).  When the card is given a wide width, the grid should span the content
    area (panel width minus the 10px root margins on each side)."""
    panel = _make_perfmon()
    host_width = 460
    panel.setFixedWidth(host_width)
    panel.show()
    qapp.processEvents()
    grid = panel.findChild(QGridLayout)
    assert grid is not None
    # content width = panel width - left/right root margin (10 + 10)
    expected = host_width - 20
    got = grid.geometry().width()
    assert abs(got - expected) <= 2, (
        f"grid should fill card width ({expected}), but grid width = {got}"
    )
