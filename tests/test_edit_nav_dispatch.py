"""Regression: every edit mode must expose non-empty gamepad/keyboard nav_items.

The ``EditOverlay.nav_items()`` dispatch once omitted fan / smt / rtss /
xinput_gate, leaving those edit pages dead to gamepad + keyboard.  This locks
full mode coverage so a future edit page is not silently un-navigable.
"""

from __future__ import annotations

import json

import pytest
from PyQt6.QtWidgets import QApplication

from ningbingjue.app import Application
from ningbingjue.core.config import AppConfig, Tile


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def overlay(tmp_path, qapp):
    cfg = tmp_path / "layout.json"
    ac = AppConfig()
    ac.tiles = [Tile(name="A", launch_path="C:/a", launch_type="FILE")]
    cfg.write_text(json.dumps(ac.to_dict(), ensure_ascii=False), encoding="utf-8")
    app = Application(str(tmp_path), config_path=str(cfg), headless=False).boot()
    ov = app._window._edit_overlay
    yield ov
    app._window.close()
    qapp.processEvents()


_EXPECT = {
    "layout": "_layout_panel",
    "tile": "_tile_panel",
    "procmon": "_procmon_panel",
    "button": "_button_panel",
    "breathing": "_breathing_panel",
    "freeze": "_freeze_panel",
    "power": "_power_panel",
    "undervolt": "_undervolt_panel",
    "perfmon": "_perfmon_panel",
    "smt": "_smt_panel",
    "fan": "_fan_panel",
    "rtss": "_rtss_panel",
    "xinput_gate": "_xinput_gate_panel",
    "cardcolor": "_card_color_panel",
}


@pytest.mark.parametrize("mode,attr", sorted(_EXPECT.items()))
def test_every_edit_mode_dispatches_nav_items(overlay, mode, attr):
    overlay._mode = mode
    items = overlay.nav_items()
    panel = getattr(overlay, attr)
    # NavItem 每次调用都新建实例，不能直接 == 比较；按底层 widget 身份核对，
    # 确保分发确实路由到对应面板（而非返回了别处的列表）。
    assert [it.widget for it in items] == [it.widget for it in panel.nav_items()]
    assert items, f"{mode} mode must expose navigable items"


def test_xinput_gate_items_carry_confirm(overlay):
    overlay._mode = "xinput_gate"
    items = overlay.nav_items()
    assert items
    # 总开关 / 添加 / 移除 / 关闭 都必须在 A 键（confirm）下可触发。
    for it in items:
        assert it.confirm is not None, f"{it.kind} item must have a confirm callback"
