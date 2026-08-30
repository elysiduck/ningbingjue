"""tile block — launch grid (磁贴).

Faithful logic port of ``blocks/tile.ps1``.  Tiles live in a fixed
grid (``MAX_COLS`` × auto rows); each tile has a 1–2 cell width/height
and a Col/Row position (auto-filled when missing).  Activating a tile
launches its target (file / exe / URL); ``IMG`` tiles only display.
Persistence flows through the host ``AppConfig`` (``Tiles`` /
``ExtraTileBlocks``), mirroring the original's ``Save-TileConfigs`` /
``Load-Layout``.  Launch I/O is routed through a ``launcher``
callable so it is fully testable headless.
"""

from __future__ import annotations

import os
import webbrowser
from typing import Any, Callable

from ..core.color.api import get_button_state
from ..core.color.palette import COLOR_MAUVE, TOAST_BAD, TOAST_OK
from ..core.color.rgb import Color
from ..core.color.semantic import get_theme_accent, is_light_theme
from ..core.config import ExtraTileBlock, Tile
from ..core.navigation import (
    FocusHolder,
    clear_block_focus,
    navigate_matrix,
    set_control_focus_visual,
)
from ..core.registry import Block
from ..platform.process_manager import ProcessManager
from .base import BlockBlueprint


class _Btn:
    def __init__(self, col: int = 0, row: int = 0) -> None:
        self.text = ""
        self._colors: tuple | None = None
        self.focused = False
        self.fore_color: Color | None = None
        self.ctrl_col = col
        self.ctrl_row = row
        self.tile: Tile | None = None

    def set_colors(self, *args: Any) -> None:
        self._colors = tuple(args)

    def set_focus_visual(self, focused: bool) -> None:
        self.focused = focused

    def invalidate(self) -> None:
        pass


class TileBlock:
    MAX_COLS = 5
    CELL_SIZE = 120
    CELL_GAP = 4
    MIN_W = 1
    MAX_W = 2
    MIN_H = 1
    MAX_H = 2
    MIN_CARD_H = 300
    LAUNCH_DEBOUNCE_S = 1.5

    def __init__(self, config: Any, process_manager: ProcessManager | None = None,
                 launcher: Callable[[str, str], bool] | None = None,
                 ui: dict[str, Any] | None = None) -> None:
        self.config = config
        self.pm = process_manager
        self.launcher = launcher
        self.ui: dict[str, Any] = dict(ui or {})
        self.theme_color = get_theme_accent()
        self.tiles: list[Tile] = list(getattr(self.config, "tiles", []) or [])
        self.extra_blocks: list[ExtraTileBlock] = list(
            getattr(self.config, "extra_tile_blocks", []) or [])
        self.buttons: list[_Btn] = []
        self.focus_idx = FocusHolder(0)
        self.in_item_mode = False
        self._last_launch_ts = 0.0
        self._rebuild()

    # -- grid helpers -------------------------------------------------
    @staticmethod
    def find_first_empty_slot(tiles: list[Tile], max_cols: int) -> tuple[int, int]:
        max_cols = max(1, int(max_cols))  # 防止 max_cols<=0 时内层循环空转→死循环
        occupied = {(t.col, t.row) for t in tiles}
        r = 0
        # 安全上限：最多扫描 len(tiles)+1 行，保证必然收敛并返回有效坐标。
        while r <= len(tiles) + 1:
            for c in range(max_cols):
                if (c, r) not in occupied:
                    return (c, r)
            r += 1
        return (0, 0)  # 兜底：极端情况下也返回有效坐标，绝不死循环

    def _rebuild(self) -> None:
        # auto-fill missing Col/Row so every tile has a slot
        for t in self.tiles:
            if t.col < 0 or t.row < 0:
                c, r = self.find_first_empty_slot(self.tiles, self.MAX_COLS)
                t.col, t.row = c, r
        if len(self.buttons) != len(self.tiles):
            self.buttons = [_Btn() for _ in self.tiles]
        for i, t in enumerate(self.tiles):
            b = self.buttons[i]
            b.ctrl_col = t.col
            b.ctrl_row = t.row
            b.tile = t
        if self.focus_idx.value >= len(self.tiles):
            self.focus_idx.value = 0

    # -- rendering ----------------------------------------------------
    def _render(self) -> None:
        for i, t in enumerate(self.tiles):
            if i >= len(self.buttons):
                break
            b = self.buttons[i]
            b.text = t.name if t.show_tile_name else ""
            st = get_button_state(self.theme_color, "frosted")
            b.set_colors(st["Back"], st["Hover"], st["Border"], st["Fore"])
            if t.tile_name_color:
                try:
                    r, g, bl = (int(x) for x in t.tile_name_color.split(","))
                    b.fore_color = Color.from_rgb(r, g, bl)
                except Exception:
                    pass

    def refresh_panel(self) -> None:
        self._rebuild()
        self._render()

    def layout_panel(self) -> None:
        self._render()

    def _card_height(self) -> int:
        if not self.tiles:
            return self.MIN_CARD_H
        max_row = max(t.row for t in self.tiles) + 1
        grid_h = max_row * (self.CELL_SIZE + self.CELL_GAP)
        return max(self.MIN_CARD_H, grid_h + 40)

    # -- launch --------------------------------------------------------
    def launch(self, tile: Tile) -> bool:
        """``Invoke-TileLaunch`` — open file/exe/URL (IMG = display only).

        Exception-safe: any failure (bad launcher callback, missing target,
        OS error) degrades to a "启动失败" toast and returns False instead of
        propagating up through the interaction controller's confirm() into a
        fatal Qt event-handler crash (PyQt6 aborts the process on an
        unhandled exception inside a key / gamepad event).
        """
        try:
            if (tile.launch_type or "").upper() == "IMG":
                return False
            path = tile.launch_path or ""
            ok = False
            if self.launcher is not None:
                try:
                    ok = self.launcher(path, tile.launch_type)
                except Exception:
                    ok = False
            elif (tile.launch_type or "").upper() == "URL":
                if not path:
                    return False
                try:
                    webbrowser.open(path)
                    ok = True
                except Exception:
                    ok = False
            elif self.pm is not None and self.pm.available and path:
                ok = self.pm.start(path)
            elif path and os.path.exists(path):
                try:
                    os.startfile(path)  # type: ignore[attr-defined]
                    ok = True
                except Exception:
                    ok = False
            if ok:
                # mirrors Show-TileLaunchToast
                self.show_toast(f"▶ {tile.name} 已启动", success=True)
            else:
                self.show_toast(f"目标不存在: {path}", success=False)
            return ok
        except Exception:
            try:
                self.show_toast(
                    f"启动失败: {tile.launch_path or '(空目标)'}", success=False)
            except Exception:
                pass
            return False

    def launch_focused(self) -> bool:
        if 0 <= self.focus_idx.value < len(self.tiles):
            return self.launch(self.tiles[self.focus_idx.value])
        return False

    @staticmethod
    def detect_type(path: str) -> str:
        """``New-TileConfig`` launch-type detection (URL / DIR / EXE / BAT / …)."""
        if not path:
            return ""
        if path.lower().startswith("http://") or path.lower().startswith("https://"):
            return "URL"
        if os.path.isdir(path):
            return "DIR"
        ext = os.path.splitext(path)[1].lower()
        return {
            ".exe": "EXE", ".bat": "BAT", ".cmd": "CMD", ".lnk": "LNK",
            ".ps1": "PS1",
            ".png": "IMG", ".jpg": "IMG", ".jpeg": "IMG", ".bmp": "IMG",
            ".gif": "IMG", ".ico": "IMG", ".webp": "IMG",
        }.get(ext, "FILE")

    # -- edit: add / remove / update tiles (Show-TileManager capability) --
    def add_tile(self, name: str, path: str) -> None:
        lt = self.detect_type(path)
        t = Tile(
            launch_type=lt,
            launch_path=path,
            name=name or "新磁贴",
            tile_name_color="255,255,255",
            width=1, height=1,
            col=-1, row=-1,
            show_tile_name=True,
        )
        self.tiles.append(t)
        self._rebuild()
        self.save_config()
        self.refresh_panel()

    def remove_tile(self, index: int) -> None:
        if 0 <= index < len(self.tiles):
            self.tiles.pop(index)
            if self.focus_idx.value >= len(self.tiles):
                self.focus_idx.value = max(0, len(self.tiles) - 1)
            self._rebuild()
            self.save_config()
            self.refresh_panel()

    def update_tile(self, index: int, **fields: Any) -> None:
        if not (0 <= index < len(self.tiles)):
            return
        t = self.tiles[index]
        if "name" in fields:
            t.name = fields["name"]
        if "launch_path" in fields:
            t.launch_path = fields["launch_path"]
            t.launch_type = self.detect_type(fields["launch_path"])
        if "width" in fields:
            t.width = max(self.MIN_W, min(self.MAX_W, int(fields["width"])))
        if "height" in fields:
            t.height = max(self.MIN_H, min(self.MAX_H, int(fields["height"])))
        if "show_tile_name" in fields:
            t.show_tile_name = bool(fields["show_tile_name"])
        self._rebuild()
        self.save_config()
        self.refresh_panel()

    # -- theme --------------------------------------------------------
    def _set_title_color(self, color: Color) -> None:
        title = self.ui.get("title")
        if title is not None:
            title.fore_color = color

    def on_theme_change(self, accent: dict[str, Any]) -> None:
        self.theme_color = get_theme_accent()
        self._set_title_color(self.theme_color)
        self._render()

    # -- item-mode navigation -----------------------------------------
    def on_activate_item(self) -> None:
        self.in_item_mode = True
        self.focus_idx.value = 0
        if self.buttons:
            set_control_focus_visual(self.buttons[0], True)

    def on_deactivate_item(self) -> None:
        self.in_item_mode = False
        clear_block_focus([b for b in self.buttons if b is not None])

    # -- persistence (host Save-Layout / Load-Layout) -----------------
    def save_config(self) -> list[dict[str, Any]]:
        self.config.tiles = list(self.tiles)
        # Keep extra blocks in sync with the LIVE config.  The layout editor
        # mutates ``config.extra_tile_blocks`` directly (New-TileBlock / delete
        # block), so we must NOT clobber it with this instance's (possibly
        # stale) ``extra_blocks`` — re-read from config, then write the same
        # list back.  This makes persistence of ExtraTileBlocks symmetric with
        # the base tiles (report §6.1 高优先级 + §6.2-1).
        self.extra_blocks = list(getattr(self.config, "extra_tile_blocks", []) or [])
        self.config.extra_tile_blocks = self.extra_blocks
        return [t.to_dict() for t in self.tiles]

    def load_config(self) -> None:
        self.tiles = list(getattr(self.config, "tiles", []) or [])
        self.extra_blocks = list(getattr(self.config, "extra_tile_blocks", []) or [])
        self._rebuild()
        self._render()

    # -- toast ---------------------------------------------------------
    def show_toast(self, text: str, success: bool = True) -> None:
        panel = self.ui.get("toast_panel")
        label = self.ui.get("toast_label")
        if panel is not None:
            panel.visible = True
        if label is not None:
            label.text = text
            label.fore_color = TOAST_OK if success else TOAST_BAD

    # -- registration ---------------------------------------------------
    def register(self, registry: Any) -> None:
        bp = BlockBlueprint(
            block_type="tile",
            name="► 磁贴",
            color=COLOR_MAUVE,
            card=self.ui.get("card"),
            hooks={
                "GetCardHeight": lambda: self._card_height(),
                "OnRefresh": self.refresh_panel,
                "OnLayout": self.layout_panel,
                "OnSyncTitleColor": self._set_title_color,
                "OnThemeChange": self.on_theme_change,
                "OnActivateItem": self.on_activate_item,
                "OnDeactivateItem": self.on_deactivate_item,
                "OnSaveConfig": self.save_config,
                "OnLoadConfig": self.load_config,
            },
        )
        registry.register(bp.to_block())

    def status(self) -> str:
        """Short live readout for the smoke report / GUI panel."""
        return f"{len(self.tiles)} 个磁贴"
