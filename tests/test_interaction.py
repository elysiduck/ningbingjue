"""Regression tests for the unified InteractionController.

Verifies that all interactive controls across every block flow through one
navigation/activation model instead of each panel wiring its own handlers.
"""

import os
import tempfile
import types
import pytest
from PyQt6.QtCore import QEvent, Qt, QPoint, QPointF
from PyQt6.QtGui import QKeyEvent, QWheelEvent
from PyQt6.QtWidgets import QApplication, QWidget, QPushButton

from ningbingjue.app import Application
from ningbingjue.ui.interaction import InteractionController, NavItem
from ningbingjue.ui.panels import BlockPanel, RtssPanel, _NoScrollSlider
from ningbingjue.ui.faq_popup import FaqController
from ningbingjue.blocks.freeze import FreezeBlock
from ningbingjue.platform.process_manager import MockProcessManager, ProcessInfo

ROOT = (
    r"D:\下载\localsend\凝冰结魄-覆盖任务栏-修复-完全解耦-备份-20260717172824"
    r"\凝冰结魄-覆盖任务栏-修复-完全解耦-备份"
)


def _rtss_panel(win):
    """Locate the rtss panel by type (panel order depends on the loaded
    layout, so index-based access is fragile)."""
    return next((p for p in win.panels if isinstance(p, RtssPanel)), None)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _key(window, key, modifiers=Qt.KeyboardModifier.NoModifier):
    ev = QKeyEvent(QEvent.Type.KeyPress, key, modifiers)
    window.keyPressEvent(ev)
    QApplication.instance().processEvents()


def test_controller_aggregates_all_panels(qapp):
    app = Application(ROOT, headless=False).boot()
    win = app._window
    ctrl: InteractionController = win._controller
    ctrl.refresh()
    # Every panel exposes at least one nav item.
    total = sum(len(p.nav_items()) for p in win.panels)
    # rtss(2) + power(6) + freeze(candidates+frozen) + procmon + ls + tile
    assert total >= 8, f"expected many nav items, got {total}"
    qapp.processEvents()
    app._window.close()
    qapp.processEvents()


def test_arrow_navigation_moves_focus(qapp):
    app = Application(ROOT, headless=False).boot()
    win = app._window
    ctrl = win._controller
    ctrl.refresh()
    items = ctrl._items
    assert len(items) > 1
    first = ctrl._focus
    ctrl.handle_key(_kev(Qt.Key.Key_Down))
    assert ctrl._focus != first or len(items) == 1
    # The focus rectangle is shown whenever something is focused.
    assert ctrl._rect.isVisible() == (ctrl._focus >= 0)
    app._window.close()
    qapp.processEvents()


def test_enter_activates_focused_item(qapp):
    app = Application(ROOT, headless=False).boot()
    win = app._window
    ctrl = win._controller
    ctrl.refresh()
    # Focus the rtss toggle and confirm it flips lock_enabled.
    for i, it in enumerate(ctrl._items):
        if it.widget is _rtss_panel(win).toggle_btn:
            ctrl.set_focus(i)
            break
    before = app.blocks["rtss"].lock_enabled
    ctrl.activate()
    after = app.blocks["rtss"].lock_enabled
    assert before != after
    app._window.close()
    qapp.processEvents()


def test_tab_steps_through_all_items(qapp):
    app = Application(ROOT, headless=False).boot()
    win = app._window
    ctrl = win._controller
    ctrl.refresh()
    n = len(ctrl._items)
    seen = set()
    for _ in range(n):
        ctrl.handle_key(_kev(Qt.Key.Key_Tab))
        seen.add(ctrl._focus)
    # A full Tab cycle visits every item (no "-1" no-focus state in the set).
    assert len(seen) == n
    app._window.close()
    qapp.processEvents()


def test_page_jump_moves_between_panels(qapp):
    app = Application(ROOT, headless=False).boot()
    win = app._window
    ctrl = win._controller
    ctrl.refresh()
    starts = list(ctrl._panel_starts)
    assert len(starts) >= 2
    ctrl.set_focus(0)
    ctrl.handle_key(_kev(Qt.Key.Key_PageDown))
    # Focus landed on a different panel's first item.
    assert ctrl._focus in starts[1:]
    app._window.close()
    qapp.processEvents()


def test_slider_arrow_nudges_value(qapp):
    app = Application(ROOT, headless=False).boot()
    win = app._window
    ctrl = win._controller
    ctrl.refresh()
    rtss = app.blocks["rtss"]
    before = rtss.fps
    for i, it in enumerate(ctrl._items):
        if it.widget is _rtss_panel(win).slider:
            ctrl.set_focus(i)
            break
    # ArrowUp should decrease FPS (slider consumes the arrow).
    ctrl.handle_key(_kev(Qt.Key.Key_Up))
    after = app.blocks["rtss"].fps
    assert after == max(rtss.FPS_MIN, before - 1)
    app._window.close()
    qapp.processEvents()


def test_mouse_click_routes_through_controller(qapp):
    app = Application(ROOT, headless=False).boot()
    win = app._window
    ctrl = win._controller
    ctrl.refresh()
    before = app.blocks["rtss"].lock_enabled
    # Simulate a click on the rtss toggle button.
    _rtss_panel(win).toggle_btn.click()
    QApplication.instance().processEvents()
    # The same unified path flips lock state.
    assert app.blocks["rtss"].lock_enabled != before
    app._window.close()
    qapp.processEvents()


def _kev(key):
    return QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)


def test_refresh_does_not_fire_on_focus(qapp):
    # Regression: the sync bridge calls controller.refresh() every second.
    # It must NOT invoke on_focus(False) during a plain content rebuild, or it
    # would clear the freeze block's pending two-press highlight_key and make
    # freezing impossible (first press sets highlight, 1s later refresh wipes
    # it, second press never matches).
    win = QWidget()
    host = QWidget(win)
    ctrl = InteractionController(win, host)
    calls = {"focus_false": 0, "focus_true": 0}

    def make_widget():
        return QPushButton("x", host)

    class MockPanel:
        def __init__(self, n):
            self.widgets = [make_widget() for _ in range(n)]

        def nav_items(self):
            items = []
            for w in self.widgets:
                items.append(NavItem(
                    w, "action", confirm=lambda: None,
                    on_focus=lambda f: (
                        calls.__setitem__("focus_true", calls["focus_true"] + 1)
                        if f else
                        calls.__setitem__("focus_false", calls["focus_false"] + 1)
                    ),
                ))
            return items

    ctrl.register_panel(MockPanel(2))
    ctrl.refresh()
    ctrl.set_focus(0)
    assert calls["focus_true"] == 1
    calls["focus_false"] = 0
    calls["focus_true"] = 0
    # The per-second content refresh must not disturb focus / on_focus.
    ctrl.refresh()
    assert calls["focus_false"] == 0
    assert calls["focus_true"] == 0
    assert ctrl._focus == 0  # focus preserved by widget identity


def test_freeze_two_press_executes(qapp):
    # Freeze uses a two-press flow (first press highlights, second executes).
    pm = MockProcessManager(
        processes=[ProcessInfo(pid=1234, name="game.exe", memory_mb=500.0)]
    )
    # Fresh config dir so a stale frozen_pids.txt can't exclude our candidate.
    cfg = tempfile.mkdtemp()
    blk = FreezeBlock(cfg, pm)
    blk.refresh_panel()
    assert len(blk.candidates) == 1
    blk.handle_hotkey(0)
    assert blk.highlight_key == 0  # first press = highlight only
    assert len(blk.frozen) == 0
    blk.handle_hotkey(0)
    assert 1234 in pm.suspended  # second press = actually frozen
    assert len(blk.frozen) == 1
    # And it survives a refresh_panel tick (the original per-second path).
    blk.refresh_panel()
    assert len(blk.frozen) == 1


def test_slider_ignores_wheel(qapp):
    slider = _NoScrollSlider(Qt.Orientation.Horizontal)
    slider.setRange(0, 100)
    slider.setValue(50)
    ev = QWheelEvent(
        QPointF(0, 0), QPointF(0, 0), QPoint(0, 0), QPoint(0, 0),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate, False,
    )
    slider.wheelEvent(ev)
    assert ev.isAccepted() is False  # wheel not consumed by the slider
    assert slider.value() == 50  # and the value did not change


def test_esc_clears_focus(qapp):
    app = Application(ROOT, headless=False).boot()
    win = app._window
    ctrl = win._controller
    ctrl.refresh()
    if ctrl._items:
        ctrl.set_focus(0)
        assert ctrl._rect.isVisible()
        ctrl.handle_key(_kev(Qt.Key.Key_Escape))
        assert ctrl._focus == -1
        assert not ctrl._rect.isVisible()
    app._window.close()
    qapp.processEvents()


class _SelectingPanel:
    """Lightweight BlockPanel stand-in: records set_card_selected() so the test
    can assert which block's focus border is lit."""

    def __init__(self, n, host):
        self.buttons = [QPushButton(f"b{i}", host) for i in range(n)]
        self.selected = False

    def nav_items(self):
        return [NavItem(w, "action", confirm=lambda: None) for w in self.buttons]

    def set_card_selected(self, sel):
        self.selected = bool(sel)


def test_block_border_follows_selected_element(qapp):
    # The block focus (thickening border) must follow whichever interactive
    # element is selected — across mouse clicks and cross-panel jumps — so the
    # highlighted block always matches the block owning the focused control.
    win = QWidget()
    host = QWidget(win)
    ctrl = InteractionController(win, host)
    p0 = _SelectingPanel(2, host)
    p1 = _SelectingPanel(2, host)
    ctrl.register_panel(p0)
    ctrl.register_panel(p1)
    ctrl.refresh()
    # Cold start: nothing highlighted yet (gate closed).
    assert p0.selected is False and p1.selected is False

    # First mouse click on a button inside panel 0 must light panel 0 only.
    # (Regression: the cold-start gate used to swallow this very first click.)
    ctrl.activate_widget(p0.buttons[0])
    assert p0.selected is True, "panel 0 must be selected after first click"
    assert p1.selected is False, "panel 1 must stay unselected"

    # Jump the focus to panel 1's first button (PageUp/PageDown in item mode).
    # The border must move with the cursor.
    ctrl._jump_panel(1)
    assert p1.selected is True, "panel 1 must be selected after cross-panel jump"
    assert p0.selected is False, "panel 0 must release after cross-panel jump"

    # Clicking a button in panel 0 again must switch the border back.
    ctrl.activate_widget(p0.buttons[1])
    assert p0.selected is True, "panel 0 must re-select on click"
    assert p1.selected is False, "panel 1 must release on click"
    qapp.processEvents()


def test_title_click_moves_focus_to_card(qapp):
    # Clicking a card's title opens that card's FAQ and must ALSO move the
    # operation focus (block thickening border + keyboard/gamepad nav) to the
    # same card — not leave it stranded on the previously-active card.  The HUD
    # does this via controller.pick_widget(panel); this reproduces that path.
    win = QWidget()
    host = QWidget(win)
    ctrl = InteractionController(win, host)
    p0 = _SelectingPanel(2, host)
    p1 = _SelectingPanel(2, host)
    ctrl.register_panel(p0)
    ctrl.register_panel(p1)
    ctrl.refresh()

    # Pretend the user had been on card 0 (border + nav focus there).
    ctrl._select_card(0)
    assert p0.selected is True
    assert ctrl._active_card == 0

    # Click card 1's title -> focus must jump to card 1.
    ctrl.pick_widget(p1)
    assert ctrl._active_card == 1, "operation focus must move to the clicked card"
    assert p1.selected is True, "card 1 must gain the focus border"
    assert p0.selected is False, "card 0 must release the focus border"
    qapp.processEvents()


def test_faq_guard_keeps_open_when_focus_matches(qapp):
    # _on_card_focus_changed must NOT close the FAQ when the newly focused card
    # is exactly the one whose FAQ is open (title-click opens the FAQ and moves
    # focus there).  It must still close when focus moves to a *different* card.
    # Bind the REAL window method onto a lightweight fake window so we exercise
    # the actual code path without constructing the heavy _OverlayWindow.
    from ningbingjue.ui.app_window import _OverlayWindow

    faq = FaqController(QWidget())
    faq.open_for("b", "test")  # FAQ now shows card "b"
    assert faq.is_open() and faq.current_block() == "b"

    fake = types.SimpleNamespace(
        _faq=faq,
        panels=[types.SimpleNamespace(block_type="b"),
                types.SimpleNamespace(block_type="a")],
    )
    method = types.MethodType(_OverlayWindow._on_card_focus_changed, fake)

    # Focus moves FROM card 1 (a) TO card 0 (b): FAQ already shows "b" -> keep.
    method(1, 0)
    assert faq.is_open() is True, "FAQ must stay open when focus matches its card"

    # Focus moves FROM card 0 (b) TO card 1 (a): FAQ shows "b" != "a" -> close.
    method(0, 1)
    assert faq.is_open() is False, "FAQ must close when focus moves to another card"
    qapp.processEvents()
