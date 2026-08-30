"""Tile block tests (headless, launcher recorder)."""

from __future__ import annotations

from ningbingjue.blocks.tile import TileBlock
from ningbingjue.core.config import AppConfig, Tile


def _launcher():
    calls = []

    def _fn(path, typ):
        calls.append((path, typ))
        return True
    return _fn, calls


def _tiles():
    return [
        Tile(name="A", launch_path="C:/a.exe", launch_type="EXE", col=0, row=0),
        Tile(name="B", launch_path="http://x", launch_type="URL", col=1, row=0),
    ]


def test_autofill_empty_slot():
    cfg = AppConfig()
    cfg.tiles = [Tile(name="Z", launch_path="C:/z", launch_type="EXE", col=-1, row=-1)]
    block = TileBlock(cfg)
    assert block.tiles[0].col >= 0
    assert block.tiles[0].row >= 0


def test_launch_records_calls():
    cfg = AppConfig()
    cfg.tiles = _tiles()
    launcher, calls = _launcher()
    block = TileBlock(cfg, launcher=launcher)
    block.on_activate_item()
    block.focus_idx.value = 0
    block.on_item_confirm()  # launch A.exe
    assert calls == [("C:/a.exe", "EXE")]
    block.focus_idx.value = 1
    block.on_item_confirm()  # launch URL
    assert calls[-1] == ("http://x", "URL")


def test_img_tile_does_not_launch():
    cfg = AppConfig()
    cfg.tiles = [Tile(name="P", launch_path="C:/p.png", launch_type="IMG", col=0, row=0)]
    launcher, calls = _launcher()
    block = TileBlock(cfg, launcher=launcher)
    block.on_activate_item()
    assert block.launch_focused() is False
    assert calls == []


def test_navigation_matrix():
    cfg = AppConfig()
    cfg.tiles = _tiles()
    block = TileBlock(cfg)
    block.on_activate_item()
    assert block.focus_idx.value == 0
    block.on_navigate(1)  # down within col 0
    assert block.focus_idx.value == 1  # B is at col1row0 -> matrix may pick different; just ensure in-range
    assert 0 <= block.focus_idx.value < len(block.tiles)


def test_save_and_load_roundtrip():
    cfg = AppConfig()
    cfg.tiles = _tiles()
    block = TileBlock(cfg)
    data = block.save_config()
    assert len(data) == 2
    assert data[0]["Name"] == "A"
    cfg2 = AppConfig()
    cfg2.tiles = [Tile.from_dict(d) for d in data]
    block2 = TileBlock(cfg2)
    block2.load_config()
    assert [t.name for t in block2.tiles] == ["A", "B"]
