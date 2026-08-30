"""Navigation tests — 2D matrix, 1D default, and the page/zone/item-mode controller."""

from __future__ import annotations

from ningbingjue.core.color.rgb import Color
from ningbingjue.core.navigation import (
    FocusHolder,
    NavigationController,
    clear_block_focus,
    default_block_navigate,
    navigate_matrix,
)
from ningbingjue.core.registry import Block, BlockRegistry
from ningbingjue.core.config import AppConfig, ConfigManager
from conftest import FakeBtn


def _grid():
    return [
        FakeBtn(col=0, row=0),
        FakeBtn(col=1, row=0),
        FakeBtn(col=0, row=1),
        FakeBtn(col=1, row=1),
    ]


def test_matrix_right_and_wrap():
    controls = _grid()
    focus = FocusHolder(0)
    navigate_matrix(controls, 10, focus)  # right
    assert focus.value == 1
    navigate_matrix(controls, 10, focus)  # right wraps to col 0
    assert focus.value == 0


def test_matrix_down():
    controls = _grid()
    focus = FocusHolder(1)  # c10
    navigate_matrix(controls, 1, focus)  # down
    assert focus.value == 3  # c11


def test_default_navigate_cyclic():
    items = [FakeBtn(), FakeBtn(), FakeBtn()]
    focus = FocusHolder(0)
    default_block_navigate(items, 1, focus)
    assert focus.value == 1
    default_block_navigate(items, 1, focus)
    assert focus.value == 2
    default_block_navigate(items, 1, focus)
    assert focus.value == 0  # wrap


def test_clear_block_focus():
    items = [FakeBtn(), FakeBtn()]
    items[0].set_focus_visual(True)
    clear_block_focus(items)
    assert items[0].focused is False


def test_navigation_controller_item_mode():
    registry = BlockRegistry()
    calls: dict[str, int] = {}

    def on_activate():
        calls["activate"] = calls.get("activate", 0) + 1

    def on_deactivate():
        calls["deactivate"] = calls.get("deactivate", 0) + 1

    def on_nav(direction):
        calls["nav"] = calls.get("nav", 0) + 1

    card = object()
    registry.register(Block(type="rtss", name="rtss", color=Color(), card=card,
                            hooks={"OnActivateItem": on_activate,
                                   "OnDeactivateItem": on_deactivate,
                                   "OnNavigate": on_nav}))
    cfg = AppConfig.from_dict({"Cards": ["rtss"], "PageBreaks": [0]})
    cm = ConfigManager()
    cm.config = cfg
    nav = NavigationController(registry, cm)

    nav.switch_zone(1)  # focus card 0
    nav.enter_item_mode()
    assert nav.in_item_mode is True
    assert calls["activate"] == 1

    nav.move_item_focus(10)
    assert calls["nav"] == 1

    nav.exit_item_mode()
    assert nav.in_item_mode is False
    assert calls["deactivate"] == 1


def test_navigation_controller_page_wrap():
    registry = BlockRegistry()
    refreshed: list[str] = []
    card_a, card_b = object(), object()
    registry.register(Block(type="rtss", name="rtss", color=Color(), card=card_a,
                            hooks={"OnRefresh": lambda: refreshed.append("rtss")}))
    registry.register(Block(type="ls", name="ls", color=Color(), card=card_b,
                            hooks={"OnRefresh": lambda: refreshed.append("ls")}))
    cfg = AppConfig.from_dict({"Cards": ["rtss", "ls"], "PageBreaks": [0, 1]})
    cm = ConfigManager()
    cm.config = cfg
    nav = NavigationController(registry, cm)
    assert nav.total_pages() == 2
    nav.switch_page(1)  # wrap to page 1
    assert nav.current_page == 1
    assert refreshed == ["ls"]
