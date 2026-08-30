"""Headless tests for the main-HUD pagination pipeline.

These exercise the *data* half of the fix (the part that answers "why do
cards moved to page 2 still show on page 1?"):

* ``Application.render_pages()`` — single source of truth that splits the
  expanded block order (``full_block_order``) at ``config.page_breaks``.
* ``Application.apply_layout(..., page_breaks=...)`` — persists a paginated
  layout (de-dupes ``Cards`` and normalises the break indices).
* ``NavigationController.goto_page`` / ``switch_page`` — track the active
  page and expose ``current_page_cards()``.

The actual panel rendering lives in ``ui/app_window._OverlayWindow`` (PyQt6),
so it is covered by ``test_layout_pagination.py`` in the GUI environment.
This file runs headless (no display / no PyQt6 required).
"""

import json
import os
import tempfile

import pytest

from ningbingjue.app import Application
from ningbingjue.core.navigation import NavigationController


@pytest.fixture()
def app():
    tmp = tempfile.mkdtemp()
    cfg = os.path.join(tmp, "layout.json")
    a = Application(script_root=os.getcwd(), headless=True, config_path=cfg)
    a.boot()
    yield a


def _order(app: Application):
    return app.full_block_order()


def test_default_single_or_uncapped_pages(app):
    pages = app.render_pages()
    assert pages, "at least one page expected"
    # 不封顶：单页可容纳任意数量卡片；但任何页都不能为空（覆盖所有
    # token 恰好一次）。
    assert all(len(p) >= 1 for p in pages)
    # Pages together cover every token exactly once.
    assert sum(len(p) for p in pages) == len(_order(app))


def test_render_pages_respects_editor_move_to_page2(app):
    order = _order(app)
    # Break at expanded index 3: page0 = order[0:3], page1 = order[3:].
    app.apply_layout(order, {t: True for t in order}, "Default",
                     page_breaks=[0, 3])
    pages = app.render_pages()
    assert len(pages) == 2
    assert pages[0] == order[0:3]
    assert pages[1] == order[3:]


def test_apply_layout_dedupes_cards_and_stores_breaks(app):
    order = _order(app)
    app.apply_layout(order, {t: True for t in order}, "Default",
                     page_breaks=[0, 2])
    saved = json.load(open(app.config_manager.path, encoding="utf-8"))
    # Cards are de-duplicated (tile appears once even with extra blocks).
    assert len(saved["Cards"]) == len(set(saved["Cards"]))
    # Breaks round-trip and start at 0.
    assert saved["PageBreaks"][0] == 0


def test_nav_goto_and_switch_track_current_page(app):
    order = _order(app)
    app.apply_layout(order, {t: True for t in order}, "Default",
                     page_breaks=[0, 3])
    nav = NavigationController(app.registry, app.config_manager, app)
    assert nav.total_pages() == 2

    nav.goto_page(1)
    assert nav.current_page == 1
    assert nav.current_page_cards() == order[3:]

    nav.switch_page(-1)
    assert nav.current_page == 0
    assert nav.current_page_cards() == order[0:3]


def test_nav_uses_render_pages_not_contracted_config(app):
    # The navigation controller must paginate on the *expanded* order
    # (render_pages), not the contracted config.cards.
    order = _order(app)
    app.apply_layout(order, {t: True for t in order}, "Default",
                     page_breaks=[0, 4])
    nav = NavigationController(app.registry, app.config_manager, app)
    assert nav._pages() == app.render_pages()
    # Active page starts at 0; jump to page 1 and confirm it tracks render_pages.
    nav.goto_page(1)
    assert nav.current_page == 1
    assert nav.current_page_cards() == order[4:]


def test_single_page_switch_and_goto_are_noop(app):
    # 单页时：掌机肩键 / 翻页键 / 页码跳转都应是纯 no-op——不切页、不发任何
    # 副作用（exit_item_mode / focus 复位 / powerplan 刷新 / 页重建回调）。
    order = _order(app)
    app.apply_layout(order, {t: True for t in order}, "Default", page_breaks=[0])
    nav = NavigationController(app.registry, app.config_manager, app)
    assert nav.total_pages() == 1

    nav.enter_item_mode()
    assert nav.in_item_mode is True

    # 肩键左右翻页（direction ±1）在单页下什么也不做。
    nav.switch_page(1)
    nav.switch_page(-1)
    assert nav.current_page == 0
    assert nav.in_item_mode is True  # 不应被踢出 item mode

    # 页码跳转（goto_page）在单页下同样 no-op。
    nav.goto_page(0)
    assert nav.current_page == 0
    assert nav.in_item_mode is True


