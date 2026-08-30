"""Tile block UI + persistence regression tests.

Covers the four "磁贴" fixes requested against the original ``凝冰结魄`` script:

1. **Persist** — tiles (and the whole ``layout.json``) actually save to disk
   now that ``Application`` defaults ``config_path`` to ``<root>/layout.json``
   (previously ``path`` was ``None`` and ``ConfigManager.save`` early-returned).
2. **2-D matrix** — the main ``TilePanel`` lays tiles out at their ``Col/Row``
   and spans them by ``Width/Height`` (1×1~2×2) inside a 4-column grid.
3. **Icons / thumbnails** — ``TileButton`` shows the tile's custom image (or,
   failing that, the launch target's shell icon) instead of a plain label.
4. **Arrangement editor** — moving a tile in the in-window ``TileEditPanel``
   updates its ``Col/Row`` and the change is written back to disk on 保存.
"""

from __future__ import annotations

import json

import pytest
from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication

from ningbingjue.app import Application
from ningbingjue.core.config import AppConfig, Tile

ROOT = (
    r"D:\下载\localsend\凝冰结魄-覆盖任务栏-修复-完全解耦-备份-20260717172824"
    r"\凝冰结魄-覆盖任务栏-修复-完全解耦-备份"
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _seed(tmp_path, tiles):
    cfg = tmp_path / "layout.json"
    ac = AppConfig()
    ac.tiles = tiles
    cfg.write_text(json.dumps(ac.to_dict(), ensure_ascii=False), encoding="utf-8")
    return cfg


def test_tile_persists_to_disk(tmp_path):
    """Adding a tile and ``save_all`` must round-trip through layout.json."""
    cfg = _seed(tmp_path, [
        Tile(name="T1", launch_path="C:/x.exe", launch_type="EXE", col=0, row=0),
    ])
    app = Application(str(tmp_path), config_path=str(cfg), headless=True).boot()
    app.blocks["tile"].add_tile("T2", "C:/y.exe")
    app.save_all()

    reloaded = Application(str(tmp_path), config_path=str(cfg), headless=True).boot()
    names = [t.name for t in reloaded.blocks["tile"].tiles]
    assert names == ["T1", "T2"]


def test_tile_panel_2d_matrix_and_icon(tmp_path, qapp):
    """Main page tiles occupy their Col/Row slot with Width/Height spans and
    render an icon when one is configured."""
    from PyQt6.QtGui import QPixmap

    img = tmp_path / "ic.png"
    QPixmap(40, 40).save(str(img), "PNG")

    cfg = _seed(tmp_path, [
        Tile(name="Big", launch_path="C:/big", launch_type="FILE",
             col=0, row=0, width=2, height=2, icon_path=str(img)),
        Tile(name="Sm", launch_path="C:/sm", launch_type="FILE",
             col=2, row=0, width=1, height=1),
    ])

    app = Application(str(tmp_path), config_path=str(cfg), headless=False).boot()
    panel = next((p for p in app._window.panels
                 if p.__class__.__name__ == "TilePanel"), None)
    assert panel is not None
    assert len(panel.btns) == 2

    item = panel.grid.itemAtPosition(0, 0)
    assert item is not None
    idx = panel.grid.indexOf(panel.btns[0])
    _row, _col, rspan, cspan = panel.grid.getItemPosition(idx)
    assert rspan == 2 and cspan == 2

    # The custom image is loaded onto the tile button.
    assert not panel.btns[0]._icon_label.pixmap().isNull()
    app._window.close()
    qapp.processEvents()


def test_editor_move_updates_position_and_saves(tmp_path, qapp):
    """Moving a tile in the in-window editor rewrites its Col/Row and persists."""
    cfg = _seed(tmp_path, [
        Tile(name="A", launch_path="C:/a", launch_type="FILE",
             col=0, row=0, width=1, height=1),
        Tile(name="B", launch_path="C:/b", launch_type="FILE",
             col=1, row=0, width=1, height=1),
    ])
    app = Application(str(tmp_path), config_path=str(cfg), headless=False).boot()

    # Open the in-window editor, enter the tile sub-mode.
    app._window._open_editor()
    app._window._edit_overlay.open_tile()
    tile_panel = app._window._edit_overlay._tile_panel
    # Select tile A and move it to row=2, col=2 (no overlap).
    tile_panel.sel = 0
    tile_panel._grid._move_selected_to(0, 2, 2)
    assert tile_panel.work[0].row == 2 and tile_panel.work[0].col == 2
    tile_panel._save()

    data = json.loads(cfg.read_text(encoding="utf-8"))
    by_name = {t["Name"]: t for t in data["Tiles"]}
    assert by_name["A"]["Row"] == 2 and by_name["A"]["Col"] == 2
    app._window.close()
    qapp.processEvents()


def test_tile_block_name_and_color_apply_to_hud(tmp_path, qapp):
    """The block name / theme color edited per-tile actually drive the HUD card
    title text, visibility and neon accent (faithful to tile.ps1 main title)."""
    from ningbingjue.core.color.themes import resolve_block_theme_color
    from ningbingjue.core.color.rgb import Color

    cfg = _seed(tmp_path, [
        Tile(name="A", launch_path="C:/a", launch_type="FILE",
             col=0, row=0, width=1, height=1,
             block_name="我的工具", block_theme_color="深海",
             show_block_title=True),
    ])
    app = Application(str(tmp_path), config_path=str(cfg), headless=False).boot()
    panel = next((p for p in app._window.panels
                  if p.__class__.__name__ == "TilePanel"), None)
    assert panel is not None

    # Header shows the block name and is visible.
    assert panel._hdr.text() == "► 我的工具"
    assert panel._hdr.isVisible() is True
    # Accent (neon border + tile borders) follows the block theme color.
    expected = "#" + resolve_block_theme_color("深海").to_hex().lstrip("#")
    assert panel.accent.lower() == expected.lower()
    app._window.close()
    qapp.processEvents()


def test_tile_block_title_hidden_when_disabled(tmp_path, qapp):
    """show_block_title=False hides the HUD card header."""
    cfg = _seed(tmp_path, [
        Tile(name="A", launch_path="C:/a", launch_type="FILE",
             col=0, row=0, width=1, height=1,
             block_name="隐藏我", show_block_title=False),
    ])
    app = Application(str(tmp_path), config_path=str(cfg), headless=False).boot()
    panel = next((p for p in app._window.panels
                  if p.__class__.__name__ == "TilePanel"), None)
    assert panel._hdr.isVisible() is False
    app._window.close()
    qapp.processEvents()


def test_tile_editor_add_tile_button(tmp_path, qapp):
    """The in-window tile editor exposes an 新增磁贴 button wired to _add_tile,
    so a (new) block can grow individual tiles."""
    cfg = _seed(tmp_path, [
        Tile(name="A", launch_path="C:/a", launch_type="FILE",
             col=0, row=0, width=1, height=1),
    ])
    app = Application(str(tmp_path), config_path=str(cfg), headless=False).boot()
    app._window._open_editor()
    app._window._edit_overlay.open_tile()
    tile_panel = app._window._edit_overlay._tile_panel

    assert hasattr(tile_panel, "_add_tile_btn")
    before = len(tile_panel.work)
    tile_panel._add_tile_btn.click()
    assert len(tile_panel.work) == before + 1
    # The new tile is reflected in the arrangement grid.
    assert len(tile_panel._grid._tiles) == before + 1
    app._window.close()
    qapp.processEvents()

