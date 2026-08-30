"""Regression tests for the frosted-skin / headless-safety work.

These guard the three behaviours that were historically fragile:

1. The overlay window must build + paint under the **offscreen** (headless)
   test surface without raising ``AttributeError`` on the skin attributes.
   The custom frosted ``paintEvent`` is gated behind ``_offscreen`` and the
   skin attributes are always initialised, so a headless ``processEvents``
   must never crash (it used to exit Python with code 127, no traceback).
2. The battery probe returns either ``None`` or a sane 3-tuple.
3. The LS panel wires the original's gamepad-hint letters (Load=A / Save=Y /
   Delete=X) onto its nav items, and ``NavItem`` defaults to an empty hint.
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication

from ningbingjue.app import Application
from ningbingjue.platform.battery import get_battery_status
from ningbingjue.ui.interaction import NavItem
from ningbingjue.ui.panels import BlockPanel, LSPanel, RtssPanel, PowerPanel

ROOT = (
    r"D:\下载\localsend\凝冰结魄-覆盖任务栏-修复-完全解耦-备份-20260717172824"
    r"\凝冰结魄-覆盖任务栏-修复-完全解耦-备份"
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_overlay_offscreen_builds_and_paints(qapp):
    """Building + painting the overlay on the offscreen surface must not
    raise (the old crash was an AttributeError on uninitialised skin attrs,
    surfacing as a hard Python exit 127 with no traceback)."""
    app = Application(ROOT, headless=False).boot()
    win = app._window
    # The test host owns no display, so Qt selects the offscreen platform.
    assert win._offscreen is True
    panels = win.findChildren(BlockPanel)
    assert len(panels) >= 6  # extra tile blocks add more panels

    # Exercise the paint path explicitly — must not throw.
    win.repaint()
    qapp.processEvents()
    win.update()
    qapp.processEvents()
    app._window.close()
    qapp.processEvents()


def test_battery_probe_sane():
    status = get_battery_status()
    if status is None:
        return
    assert isinstance(status, tuple) and len(status) == 3
    pct, charging, available = status
    assert isinstance(pct, int) and 0 <= pct <= 100
    assert isinstance(charging, bool)
    assert isinstance(available, bool)


def test_navitem_default_hint_empty():
    item = NavItem(object(), "action")
    assert item.hint == ""


def test_ls_nav_items_carry_gamepad_hints(qapp):
    app = Application(ROOT, headless=False).boot()
    ls_panel = next((p for p in app._window.panels if isinstance(p, LSPanel)), None)
    assert ls_panel is not None
    items = ls_panel.nav_items()
    by_widget = {it.widget: it.hint for it in items}
    assert by_widget.get(ls_panel.load_btn) == "A"
    assert by_widget.get(ls_panel.save_btn) == "Y"
    assert by_widget.get(ls_panel.del_btn) == "X"
    app._window.close()
    qapp.processEvents()


def test_main_action_hints(qapp):
    """The other blocks' primary-action buttons carry the gamepad hint
    following the convention A=主操作 / Y=保存 / X=删除 / B=取消.

    rtss 锁帧 (主操作) -> A ; power 应用 (主操作) -> A.
    """
    app = Application(ROOT, headless=False).boot()
    rtss = next((p for p in app._window.panels if isinstance(p, RtssPanel)), None)
    power = next((p for p in app._window.panels if isinstance(p, PowerPanel)), None)
    assert rtss is not None and power is not None
    by_rtss = {it.widget: it.hint for it in rtss.nav_items()}
    by_power = {it.widget: it.hint for it in power.nav_items()}
    assert by_rtss.get(rtss.toggle_btn) == "A"
    assert by_power.get(power.apply) == "A"
    app._window.close()
    qapp.processEvents()
