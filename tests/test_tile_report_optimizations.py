"""Verification for the tile.ps1 analysis-report optimizations (S6.1/S6.2).

Covers:
* ExtraTileBlock end-to-end persistence (report S6.1 high / S6.2-1)
* JSON load failure keeps the raw text for recovery (report S6.2-5 / S5.1)
* ``_edit_items("tile#N")`` opens the additional block editor (fixes the
  previously-dead edit button on extra blocks)
* Editing an ExtraTileBlock writes block name / theme / show-title / tiles back
  to config AND live-refreshes the HUD proxy panel (tile card display sync)
"""

from __future__ import annotations

import json

import pytest
from PyQt6.QtWidgets import QApplication

from ningbingjue.app import Application
from ningbingjue.core.color.palette import TILE_BG, TILE_FOCUS_BORDER
from ningbingjue.core.config import AppConfig, ConfigManager, ExtraTileBlock, Tile
from ningbingjue.ui.panels import layout_tile_grid


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _seed_with_extra(tmp_path):
    cfg = tmp_path / "layout.json"
    ac = AppConfig()
    ac.extra_tile_blocks = [ExtraTileBlock(
        block_name="工具箱", block_theme_color="Teal", show_block_title=True,
        tiles=[Tile(name="Note", launch_path="C:/n.exe", launch_type="EXE",
                    col=0, row=0, block_name="工具箱",
                    block_theme_color="Teal", show_block_title=True)],
    )]
    cfg.write_text(json.dumps(ac.to_dict(), ensure_ascii=False), encoding="utf-8")
    return cfg


def test_extra_tile_block_persists_round_trip(tmp_path):
    """A saved ExtraTileBlock (with its own tiles) survives a reload."""
    cfg = _seed_with_extra(tmp_path)
    reloaded = Application(str(tmp_path), config_path=str(cfg), headless=True).boot()
    blocks = reloaded.config_manager.config.extra_tile_blocks
    assert len(blocks) == 1
    assert blocks[0].block_name == "工具箱"
    assert blocks[0].block_theme_color == "Teal"
    assert blocks[0].show_block_title is True
    assert blocks[0].tiles[0].name == "Note"
    assert blocks[0].tiles[0].launch_type == "EXE"


def test_config_load_keeps_raw_on_error(tmp_path):
    """On a malformed layout.json, the raw text is preserved (not silently lost)."""
    cfg = tmp_path / "layout.json"
    cfg.write_text("{ this is : not valid json ", encoding="utf-8")
    cm = ConfigManager(str(cfg))
    config = cm.load()
    assert cm.last_error_raw is not None
    assert "this is" in cm.last_error_raw
    assert cm.last_error  # error message captured
    # Still returns a usable (empty) config instead of crashing.
    assert isinstance(config, AppConfig)


def test_edit_items_opens_extra_block_editor(tmp_path, qapp):
    """Clicking edit on a tile#N row opens the editor scoped to that block."""
    cfg = _seed_with_extra(tmp_path)
    app = Application(str(tmp_path), config_path=str(cfg), headless=False).boot()
    app._window._open_editor()
    layout_panel = app._window._edit_overlay._layout_panel
    extra = app.config_manager.config.extra_tile_blocks[0]
    layout_panel._edit_items("tile#0")
    tile_panel = app._window._edit_overlay._tile_panel
    assert tile_panel._extra is extra
    app._window.close()
    qapp.processEvents()


def test_edit_extra_block_syncs_hud_and_config(tmp_path, qapp):
    """Editing an additional block updates config + live HUD proxy panel."""
    cfg = _seed_with_extra(tmp_path)
    app = Application(str(tmp_path), config_path=str(cfg), headless=False).boot()
    extra = app.config_manager.config.extra_tile_blocks[0]
    tp = app._window._edit_overlay._tile_panel
    tp.edit_target(extra)

    # Change block name + theme + title visibility.
    tp._block_name.setText("我的工具")
    tp._apply_block_name()
    tp._block_color.setCurrentText("Peach")
    tp._apply_block_color("Peach")
    tp._toggle_show_title()  # flips to hidden
    # Add a second tile to the block.
    tp._add_tile()

    # 1) working copy + config reflect the edits
    assert tp.work[-1].block_name == "我的工具"
    eb = app.config_manager.config.extra_tile_blocks[0]
    assert eb.block_name == "我的工具"
    assert eb.block_theme_color == "Peach"
    assert eb.show_block_title is False
    assert len(eb.tiles) == 2

    # 2) the HUD proxy panel live-refreshed: header + tile name + title hidden
    proxy = next(p for p in app._window.panels
                 if getattr(getattr(p, "instance", None), "_extra", None) is extra)
    assert "我的工具" in proxy._hdr.text()
    assert proxy._hdr.isVisible() is False  # show_block_title -> hidden
    assert proxy.btns[0]._name_label.text() == "Note"

    # 3) persisted to disk
    data = json.loads(cfg.read_text(encoding="utf-8"))
    saved = data["ExtraTileBlocks"][0]
    assert saved["BlockName"] == "我的工具"
    assert saved["BlockThemeColor"] == "Peach"
    assert saved["ShowBlockTitle"] is False
    assert len(saved["Tiles"]) == 2
    app._window.close()
    qapp.processEvents()


def test_delete_extra_block_removes_it(tmp_path, qapp):
    """Deleting an additional block removes it from config + HUD."""
    cfg = _seed_with_extra(tmp_path)
    app = Application(str(tmp_path), config_path=str(cfg), headless=False).boot()
    extra = app.config_manager.config.extra_tile_blocks[0]
    tp = app._window._edit_overlay._tile_panel
    tp.edit_target(extra)
    tp._del_block()

    assert len(app.config_manager.config.extra_tile_blocks) == 0
    # no proxy panel remains for this block
    left = [p for p in app._window.panels
            if getattr(getattr(p, "instance", None), "_extra", None) is extra]
    assert left == []
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data.get("ExtraTileBlocks", []) == []
    app._window.close()
    qapp.processEvents()


def test_tile_colors_centralized():
    """Hard-coded tile surface/focus colours now flow from the palette API."""
    assert TILE_BG.r == 28 and TILE_BG.g == 30 and TILE_BG.b == 38
    assert TILE_FOCUS_BORDER.r == 230 and TILE_FOCUS_BORDER.g == 255 \
        and TILE_FOCUS_BORDER.b == 30


def test_layout_tile_grid_helper():
    """The shared grid layout helper positions tiles at Col/Row with spans."""
    from PyQt6.QtWidgets import QGridLayout, QWidget
    grid = QGridLayout()
    parent = QWidget()
    btns = [QWidget(parent) for _ in range(2)]
    tiles = [
        Tile(name="Big", col=0, row=0, width=2, height=2),
        Tile(name="Sm", col=2, row=0, width=1, height=1),
    ]
    layout_tile_grid(grid, tiles, btns, max_cols=5)
    idx = grid.indexOf(btns[0])
    _r, _c, rspan, cspan = grid.getItemPosition(idx)
    assert rspan == 2 and cspan == 2
    idx1 = grid.indexOf(btns[1])
    r1, c1, _, _ = grid.getItemPosition(idx1)
    assert r1 == 0 and c1 == 2
