"""In-window editing overlay — faithful port of the original ``Build-EditLayout``
+ ``Build-TileEditLayout`` edit mode.

The original keeps *all* editing UI inside the main window: pressing the edit
key shows a single full-window overlay (``$pnlEdit``) that hides every card and
builds the editor inside itself.  This module reproduces that exactly — the
overlay is a child of the HUD window (it shares the header / theme), never a
separate dialog, and offers three stacked sub-modes:

* **layout**  — card ordering / show-hide / theme / button style (port of
  ``Build-EditLayout``); Save(Y) / Cancel(B) leave edit mode.
* **tile**    — port of ``Build-TileEditLayout``: three stacked cards
  (block settings / 2-D draggable preview grid / tile properties) + Save(Y) /
  Cancel(B).  The preview grid uses the original's drag-to-rearrange with AABB
  overlap swap (``Refresh-BlockPreview`` + the MouseUp snap logic).
* **procmon** — port of ``Build-ProcMonEditLayout``: item list + add / delete /
  save.

Every control is exposed through :class:`InteractionController` so the same
keyboard **and** gamepad navigation drives the editor.
"""

from __future__ import annotations

import copy
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from PyQt6.QtCore import (Qt, QTimer, pyqtSignal, QPoint, QRect, QRectF, QSize,
                          QThread, QEvent, QPropertyAnimation, QEasingCurve,
                          QObject)
from PyQt6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRegion,
)
from PyQt6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QColorDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    QWidgetItem,
    QLayout,
    QLayoutItem,
)

from ..core.color import get_theme_accent
from ..core.color.rgb import Color
from ..core.color.palette import TILE_BG, TILE_BG_HOVER, TILE_FOCUS_BORDER
from ..core.config import Tile
from ..core.config import breath_speed_to_period
from ..core import style
from .blur_util import blur_pixmap
from .button_edit import ButtonEditPanel
from .gamepad_hint import HintButton, GamepadHintBar
from .interaction import NavItem
from .tile_button import TileButton
from .faq_popup import ClickableTitle
from ._scroll_style import apply_thin_scroll
from .panels import _card_mark_name, _NoWheelComboBox, _Label, _Toast, _show_block_toast

# 每一页最多显示的卡片数量上限。
# ``None`` 表示「不封顶」：一页可以容纳任意数量的卡片，分页只由
# 用户手动插入的断点决定，不再自动按数量拆分。所有分页纯逻辑
# （``._pagination``）在 cap 为 None 时跳过自动分栏与容量上限判断。
PAGE_CAP: int | None = None

# 编辑器各「页面」主标题的字号（明显大于正文，确保一眼能认出是页面标题）。
# 子卡片标题用 ``_SUB_TITLE_SIZE``，比主标题略小，保持层级。
PAGE_TITLE_SIZE = 15
_SUB_TITLE_SIZE = 16

# 降压控制编辑项里「启用自动降压 / 允许-50mV 重载档」开关的样式（开=绿）。
_UV_EDIT_TOGGLE_STYLE = (
    "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
    " border: none; border-radius: 8px;"
    " padding: 6px 14px; font-size: 13px; }"
    "QPushButton:checked { background: rgba(130,255,30,0.22);"
    " color: #b6ff8a; border: none; }"
)

# 降压控制编辑项里「空闲降压幅度」滑块样式（拖动即时改、松手落盘）。
_UV_EDIT_SLIDER_STYLE = (
    "QSlider { border: none; background: transparent; }"
    "QSlider::groove:horizontal { border: none; height: 6px;"
    " background: rgba(255,255,255,0.15); border-radius: 3px; }"
    "QSlider::handle:horizontal { border: none; background: #62e0a8;"
    " width: 14px; height: 14px; margin: -4px 0; border-radius: 7px; }"
    "QSlider::sub-page:horizontal { background: #62e0a8; border-radius: 3px; }"
)

# Tile name-colour presets (original btnNameColor).
_NAME_COLORS = [
    ("白", "255,255,255"),
    ("粉", "255,170,200"),
    ("蓝", "170,210,255"),
    ("绿", "180,255,180"),
    ("橙", "255,220,150"),
    ("黄", "245,215,75"),
]

# 磁贴边框/背景预设色（rrggbb，供点选）。空点（allow_empty）含义：
# 边框 = 跟随呼吸灯主题色；背景 = 透明（用默认磁贴底色）。
_TILE_BORDER_PRESETS = ["f25f5f", "f2a24b", "f2d24b", "7bd66b",
                        "4bd6c8", "4b9bf2", "a86bf2", "f26bc4", "ffffff"]
_TILE_BG_PRESETS = ["1c1f29", "2b3242", "3a2b2b", "2b3a2b",
                    "2b3a3a", "3a2b3a", "2e2a1b", "ffffff", "000000"]


def _rgb_str_to_hex(s: str) -> str:
    """'255,0,0' -> 'ff0000'；空串或非法 -> ''。"""
    s = (s or "").strip()
    if not s:
        return ""
    try:
        r, g, b = (int(x) for x in s.split(","))
        return (f"{max(0, min(255, r)):02x}{max(0, min(255, g)):02x}"
                f"{max(0, min(255, b)):02x}")
    except Exception:
        return ""


def _hex_to_rgb_str(hx: str) -> str:
    """'ff0000' -> '255,0,0'；空串 -> ''。"""
    hx = (hx or "").strip().lstrip("#")
    if not hx:
        return ""
    try:
        return f"{int(hx[0:2], 16)},{int(hx[2:4], 16)},{int(hx[4:6], 16)}"
    except Exception:
        return ""


def _tile_apply_colors(btn: "TileButton", t: Any, accent: Any) -> None:
    """把磁贴的边框/背景覆盖应用到 TileButton（HUD 与编辑预览共用）。

    空覆盖：边框回退呼吸灯主题色 ``accent``，背景回退默认磁贴底色
    ``TILE_BG``；非空覆盖按 'R,G,B' 解析后生效（解析失败则沿用回退值）。
    边框 'none' = 透明（无边框，边框色取背景色，视觉无描边）。
    """
    back_col = TILE_BG
    bgc = getattr(t, "bg_color", "") or ""
    if bgc:
        try:
            r, g, b = (int(x) for x in bgc.split(","))
            back_col = Color(r, g, b)
        except Exception:
            pass
    border_col = accent
    bc = getattr(t, "border_color", "") or ""
    if bc == "none":
        border_col = back_col
    elif bc:
        try:
            r, g, b = (int(x) for x in bc.split(","))
            border_col = Color(r, g, b)
        except Exception:
            pass
    btn.set_colors(back_col, TILE_BG_HOVER, border_col, TILE_FOCUS_BORDER)

# Card (title + frame) colour presets for the new 卡片颜色 edit item.
# Stored as "R,G,B"; the layout editor + homepage read these to recolour the
# card title (in both views) and the homepage card's outer neon frame.
_CARD_COLOR_PRESETS = [
    ("青", "127,209,255"),
    ("薄荷", "110,255,180"),
    ("翠绿", "130,255,120"),
    ("柠黄", "230,255,120"),
    ("金黄", "255,209,120"),
    ("橙", "255,170,90"),
    ("珊瑚", "255,120,130"),
    ("玫红", "255,130,200"),
    ("紫", "190,150,255"),
    ("靛", "150,160,255"),
    ("天蓝", "120,190,255"),
    ("青绿", "120,235,220"),
    ("雪白", "230,240,255"),
    ("赤红", "255,100,100"),
]


def _hex(c: Any) -> str:
    if c is None:
        return "#cfd8e3"
    r = getattr(c, "r", 207)
    g = getattr(c, "g", 216)
    b = getattr(c, "b", 227)
    return f"#{r:02x}{g:02x}{b:02x}"


def _parse_color(csv: str) -> "Color":
    """Parse a "R,G,B" string into a Color; transparent (empty) → (0,0,0,0)."""
    if not csv or not str(csv).strip():
        return Color(0, 0, 0, 0)
    parts = [p.strip() for p in str(csv).split(",")]
    if len(parts) == 3:
        try:
            return Color.from_rgb(int(parts[0]), int(parts[1]), int(parts[2]))
        except (ValueError, TypeError):
            pass
    return Color(0, 0, 0, 0)


def _rgb_to_hex(rgb: str) -> str:
    parts = [int(x) for x in rgb.split(",")]
    return f"{parts[0]:02x}{parts[1]:02x}{parts[2]:02x}"


def _hex_to_csv(hexv: str) -> str:
    h = hexv.lstrip("#")
    return f"{int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)}"


def _block_card_color_hex(app: Any, block_key: str) -> str:
    """返回某区块功能卡片（HUD 上那张卡）的标题颜色，格式 ``#rrggbb``。

    与主页卡片标题一致：``config.card_colors`` 覆盖优先；tile 区块跟随呼吸灯
    主题色（tile 无注册表色）；其余区块回退注册表里的默认色。外框才是统一
    跟随呼吸灯的单旋钮来源。
    """
    if app is None or not block_key:
        return "#eaf2ff"
    cfg_mgr = getattr(app, "config_manager", None)
    cfg = getattr(cfg_mgr, "config", None) if cfg_mgr is not None else None
    if cfg is not None:
        card_colors = getattr(cfg, "card_colors", None)
        if card_colors:
            csv = card_colors.get(block_key)
            if csv:
                c = _parse_color(csv)
                if not (c.r == 0 and c.g == 0 and c.b == 0 and c.a == 0):
                    return _hex(c)
    if block_key == "tile" or block_key.startswith("tile#"):
        return _hex(get_theme_accent())
    base = block_key.split("#")[0] if "#" in block_key else block_key
    reg = getattr(app, "registry", None)
    blk = reg.get(base) if reg is not None else None
    if blk is not None and getattr(blk, "color", None) is not None:
        return _hex(blk.color)
    return "#eaf2ff"


# --------------------------------------------------------------------------
# 2-D draggable arrangement grid (port of pnlBlockGrid + Refresh-BlockPreview)
# --------------------------------------------------------------------------
class TileGridEditor(QWidget):
    """4×4 grid that mirrors the original's drag-to-rearrange preview.

    * Click a tile -> select it (loads its properties below).
    * Drag a tile -> follows the cursor, a ghost shows the snapped cell, and on
      release the tile is placed with the original's AABB overlap swap.
    * Gamepad: when focused, the stick / D-pad nudges the selected tile one cell
      (clamped + overlap swap); A toggles selection.
    """

    COLS = 5
    ROWS = 5
    CELL = 50
    GAP = 5
    PAD = 6

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.work: list[Tile] = []
        self.sel: int = -1
        self._tiles: list[TileButton] = []
        self._drag: Optional[dict] = None
        self._on_sel: Optional[Callable[[int], None]] = None
        self._on_move: Optional[Callable[[], None]] = None
        w = self.PAD * 2 + self.COLS * self.CELL + (self.COLS - 1) * self.GAP
        h = self.PAD * 2 + self.ROWS * self.CELL + (self.ROWS - 1) * self.GAP
        self.setFixedSize(w, h)
        self.setStyleSheet(
            "background: rgba(20,24,32,0.65); border: none;"
            " border-radius: 10px;")
        self._ghost = QLabel(self)
        self._ghost.setStyleSheet(
            "background: rgba(127,209,255,0.22); border: 1px dashed #7fd1ff;"
            " border-radius: 6px;")
        self._ghost.hide()
        self.setMouseTracking(True)

    # -- geometry helpers ----------------------------------------------
    def _cs(self) -> int:
        return self.CELL + self.GAP

    def _btn_geom(self, col: int, row: int, w: int, h: int) -> tuple[int, int, int, int]:
        cs = self._cs()
        return (self.PAD + col * cs, self.PAD + row * cs,
                w * self.CELL + (w - 1) * self.GAP, h * self.CELL + (h - 1) * self.GAP)

    def _clamp(self, t: Tile) -> tuple[int, int, int, int]:
        w = max(1, min(self.COLS, int(t.width)))
        h = max(1, min(self.ROWS, int(t.height)))
        col = max(0, min(int(t.col), self.COLS - w))
        row = max(0, min(int(t.row), self.ROWS - h))
        return w, h, col, row

    # -- data -----------------------------------------------------------
    def _aabb_overlap(self, c1: int, r1: int, w1: int, h1: int,
                      c2: int, r2: int, w2: int, h2: int) -> bool:
        return c1 < c2 + w2 and c1 + w1 > c2 and r1 < r2 + h2 and r1 + h1 > r2

    def _first_overlap(self, idx: int) -> int:
        """Return the index of the first *other* tile whose footprint overlaps
        ``work[idx]`` (by AABB), or -1 if none."""
        t = self.work[idx]
        w, h, c, r = self._clamp(t)
        for oi, o in enumerate(self.work):
            if oi == idx:
                continue
            ow, oh, oc, orr = self._clamp(o)
            if self._aabb_overlap(c, r, w, h, oc, orr, ow, oh):
                return oi
        return -1

    def _free_slot(self, w: int, h: int,
                   exclude: Optional[int] = None) -> Optional[tuple[int, int]]:
        """First (col, row) where a ``w``×``h`` tile fits entirely inside the
        grid **and** does not overlap any other tile's footprint (optionally
        ignoring ``exclude``).  Returns ``None`` when the grid is full."""
        w = max(1, min(self.COLS, w))
        h = max(1, min(self.ROWS, h))
        for row in range(self.ROWS - h + 1):
            for col in range(self.COLS - w + 1):
                ok = True
                for oi, o in enumerate(self.work):
                    if exclude is not None and oi == exclude:
                        continue
                    ow, oh, oc, orr = self._clamp(o)
                    if self._aabb_overlap(col, row, w, h, oc, orr, ow, oh):
                        ok = False
                        break
                if ok:
                    return (col, row)
        return None

    def _resolve_all(self) -> None:
        """Guarantee a collision-free grid: for every tile that currently
        overlaps another, relocate it to the first free footprint slot.  Run on
        load so an inconsistent saved layout never renders overlapping tiles."""
        for i in range(len(self.work)):
            if self._first_overlap(i) >= 0:
                slot = self._free_slot(
                    *self._clamp(self.work[i])[:2], exclude=i)
                if slot is not None:
                    self.work[i].col, self.work[i].row = slot[0], slot[1]

    def set_data(self, work: list[Tile], sel: int,
                 on_sel: Optional[Callable[[int], None]] = None) -> None:
        self.work = work
        self.sel = sel
        self._on_sel = on_sel
        self._resolve_all()
        self.rebuild()

    def rebuild(self) -> None:
        for b in self._tiles:
            b.deleteLater()
        self._tiles = []
        for i, t in enumerate(self.work):
            w, h, col, row = self._clamp(t)
            btn = TileButton(self)
            btn._tile_idx = i
            btn.set_tile_name(t.name or f"磁贴{i + 1}")
            if t.icon_path and os.path.exists(t.icon_path):
                btn.set_icon(t.icon_path)
            elif (t.launch_type or "").upper() == "IMG" and t.launch_path \
                    and os.path.exists(t.launch_path):
                btn.set_icon(t.launch_path)
            elif t.launch_path and not str(t.launch_path).lower().startswith("http") \
                    and os.path.exists(t.launch_path):
                btn.set_file_icon(t.launch_path)
            else:
                btn.clear_icon()
            x, y, bw, bh = self._btn_geom(col, row, w, h)
            btn.setGeometry(x, y, bw, bh)
            btn.set_selected(i == self.sel)
            # 应用 per-tile 边框/背景覆盖，使编辑预览与 HUD 渲染一致。
            _tile_apply_colors(btn, t, get_theme_accent())
            btn.show()
            self._tiles.append(btn)

    # -- hit testing ----------------------------------------------------
    def _tile_idx_at(self, pos: Any) -> Optional[int]:
        child = self.childAt(pos)
        w: Optional[QWidget] = child
        while w is not None and w is not self:
            if isinstance(w, TileButton):
                return w._tile_idx
            w = w.parent()
        return None

    def _cell_at(self, cx: int, cy: int) -> tuple[int, int]:
        cs = self._cs()
        col = max(0, min(self.COLS - 1, round((cx - self.PAD - self.CELL / 2) / cs)))
        row = max(0, min(self.ROWS - 1, round((cy - self.PAD - self.CELL / 2) / cs)))
        return col, row

    # -- mouse ----------------------------------------------------------
    def mousePressEvent(self, ev: Any) -> None:  # noqa: N802
        idx = self._tile_idx_at(ev.position().toPoint())
        if idx is None:
            self.sel = -1
            self.rebuild()
            if self._on_sel:
                self._on_sel(-1)
            self._drag = None
            return
        btn = self._tiles[idx]
        self._drag = {
            "idx": idx,
            "offx": ev.position().x() - btn.x(),
            "offy": ev.position().y() - btn.y(),
            "startx": ev.position().x(),
            "starty": ev.position().y(),
            "btn": btn,
        }
        btn.raise_()

    def mouseMoveEvent(self, ev: Any) -> None:  # noqa: N802
        if self._drag is None:
            return
        btn = self._drag["btn"]
        nx = max(0, ev.position().x() - self._drag["offx"])
        ny = max(0, ev.position().y() - self._drag["offy"])
        # ``move`` requires integer pixel coords; the stick / cursor deltas are
        # floats and would raise ``TypeError`` (crashing the whole window).
        btn.move(int(nx), int(ny))
        t = self.work[self._drag["idx"]]
        w, h, _, _ = self._clamp(t)
        col, row = self._cell_at(btn.x() + btn.width() // 2, btn.y() + btn.height() // 2)
        gx, gy, gw, gh = self._btn_geom(col, row, w, h)
        self._ghost.setGeometry(int(gx), int(gy), int(gw), int(gh))
        self._ghost.show()

    def mouseReleaseEvent(self, ev: Any) -> None:  # noqa: N802
        if self._drag is None:
            return
        btn = self._drag["btn"]
        idx = self._drag["idx"]
        dx = abs(ev.position().x() - self._drag["startx"])
        dy = abs(ev.position().y() - self._drag["starty"])
        self._ghost.hide()
        self._drag = None
        if dx < 6 and dy < 6:
            self.sel = idx
            self.rebuild()
            if self._on_sel:
                self._on_sel(idx)
            return
        col, row = self._cell_at(btn.x() + btn.width() // 2, btn.y() + btn.height() // 2)
        self._move_selected_to(idx, row, col)

    # -- movement (shared by mouse drop + gamepad nudge) ----------------
    def _move_selected_to(self, idx: int, row: int, col: int) -> None:
        """Place tile ``idx`` at (col,row), guaranteeing a collision-free grid.

        Clamps to bounds, then resolves any overlap with a *swap chain*: the
        dragged tile and the first tile it overlaps exchange positions (the
        original ``tile.ps1`` MouseUp handler); if the dragged tile still
        overlaps a third tile afterwards, the chain repeats until clean.  As a
        last resort it is pushed to the first fully-free footprint slot.
        """
        t = self.work[idx]
        w, h, _, _ = self._clamp(t)
        col = max(0, min(col, self.COLS - w))
        row = max(0, min(row, self.ROWS - h))
        # Save the original position *before* moving so the swap chain can
        # restore it on the displaced tile.
        old_col, old_row = t.col, t.row
        t.col, t.row = col, row

        guard = 0
        while guard < 256:
            guard += 1
            victim = self._first_overlap(idx)
            if victim < 0:
                break
            o = self.work[victim]
            # A↔B exchange: A takes B's cell, B takes A's *pre-move* cell
            # (which was free of every other tile → no new overlap).
            t.col, t.row = o.col, o.row
            o.col, o.row = old_col, old_row

        # Pathological fallback: if anything still overlaps, drop into a free slot.
        if self._first_overlap(idx) >= 0:
            slot = self._free_slot(w, h, exclude=idx)
            if slot is not None:
                t.col, t.row = slot[0], slot[1]

        self.sel = idx
        self.rebuild()
        if self._on_sel:
            self._on_sel(idx)
        if self._on_move:
            self._on_move()

    def nudge(self, dr: int, dc: int) -> bool:
        """Gamepad / arrow: move the selected tile one cell. Returns True if it
        actually moved (so the controller knows to consume the direction)."""
        if self.sel < 0 or self.sel >= len(self.work):
            return False
        t = self.work[self.sel]
        w, h, col, row = self._clamp(t)
        ncol = max(0, min(col + dc, self.COLS - w))
        nrow = max(0, min(row + dr, self.ROWS - h))
        if ncol == col and nrow == row:
            return False
        self._move_selected_to(self.sel, nrow, ncol)
        return True

    def confirm(self) -> bool:
        """Gamepad A on the grid: toggle selection of the first tile."""
        if self.sel >= 0:
            self.sel = -1
        elif self.work:
            self.sel = 0
        self.rebuild()
        if self._on_sel:
            self._on_sel(self.sel)
        return True


# --------------------------------------------------------------------------
# Tile editor (port of Build-TileEditLayout — three stacked cards)
# --------------------------------------------------------------------------
class TileEditPanel(QWidget):
    def __init__(self, app: Any, overlay: Any) -> None:
        super().__init__()
        self.setStyleSheet(
            "QLabel { background: transparent; border: none; }"
            "QLineEdit { background: #0e1117; color: #dfe6f0;"
            " border: 1px solid rgba(255,255,255,0.10);"
            " border-radius: 4px; padding: 3px 6px; }"
            "QLineEdit:focus { border: 1px solid rgba(255,255,255,0.18); }"
            "QComboBox { background: #0e1117; color: #dfe6f0;"
            " border: 1px solid rgba(255,255,255,0.10);"
            " border-radius: 4px; padding: 3px 6px; }"
            "QComboBox:hover { border: 1px solid rgba(255,255,255,0.14); }"
            "QComboBox:focus { border: 1px solid rgba(255,255,255,0.18); }"
            "QComboBox::drop-down { border: none; width: 20px; }"
            "QComboBox QAbstractItemView { border: none; background: #0e1117; }"
            "QPushButton { border: none; }"
            "QPushButton:hover { border: none; }"
            "QPushButton:pressed { border: none; }"
            "QPushButton:focus { border: none; }"
            "QPushButton:checked { border: none; }"
        )
        self._app = app
        self._overlay = overlay
        self.block: Any = None
        self.work: list[Tile] = []
        self.sel: int = -1
        self._editing = False
        # None → editing the base tile block; an ExtraTileBlock → editing that
        # additional block's own tiles + block-level metadata.
        self._extra: Any = None

        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(10, 10, 10, 10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        apply_thin_scroll(scroll, always_on=False)
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setSpacing(8)
        bl.setContentsMargins(0, 0, 0, 0)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        # --- Card 1: block settings ---
        s_card = self._card("磁贴区块设置", title_click=self._open_faq,
                            title_color=_block_card_color_hex(self._app, "tile"),
                            title_size=PAGE_TITLE_SIZE)
        sf = s_card.content()
        # Row: block name input + the show/hide-title toggle right after it.
        f_bn = QHBoxLayout()
        f_bn.addWidget(QLabel("区块名称"))
        self._block_name = QLineEdit()
        self._block_name.editingFinished.connect(self._apply_block_name)
        f_bn.addWidget(self._block_name, 1)
        self._show_title = QPushButton("显标题")
        self._show_title.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 5px; padding: 3px 8px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.16); }")
        self._show_title.clicked.connect(self._toggle_show_title)
        f_bn.addWidget(self._show_title)
        sf.addLayout(f_bn)
        # 全局开关（不属于某个磁贴，而是整个磁贴区块）：启动目标程序成功后
        # 是否把凝冰结魄本体收进托盘 / 任务栏，让位给刚打开的程序。
        f_ml = QHBoxLayout()
        f_ml.addWidget(QLabel("启动后最小化本体"))
        self._min_after_launch = QPushButton("已开启")
        self._min_after_launch.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 5px; padding: 3px 8px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.16); }")
        self._min_after_launch.clicked.connect(self._toggle_min_after_launch)
        f_ml.addWidget(self._min_after_launch)
        f_ml.addStretch(1)
        sf.addLayout(f_ml)
        bl.addWidget(s_card)

        # --- Card: card colour (title + frame preset picker) ----------
        cc_card = self._card("卡片颜色", title_click=self._open_faq)
        ccf = cc_card.content()
        self._card_color_picker = CardColorPicker(
            self._app, self._overlay, "", on_change=self._refresh_card_color_preview)
        ccf.addWidget(self._card_color_picker)
        # 「卡片颜色」卡（含「跟随呼吸灯」开关）已移至页面最底部，见 t_card 之后。

        # --- Card 2: preview grid (drag to arrange) ---
        p_card = self._card("► 区块预览（拖动磁贴可排列）")
        pf = p_card.content()
        self._grid = TileGridEditor(p_card)
        self._grid._on_move = self._auto_save  # persist after every drag/nudge
        pf.addWidget(self._grid)
        bl.addWidget(p_card)

        # --- Card 3: tile properties ---
        t_card = self._card("磁贴属性")
        tf = t_card.content()

        f_name = QHBoxLayout()
        f_name.addWidget(QLabel("名称"))
        self._name = QLineEdit()
        self._name.editingFinished.connect(self._apply_name)
        f_name.addWidget(self._name)
        tf.addLayout(f_name)

        f_path = QHBoxLayout()
        f_path.addWidget(QLabel("路径"))
        self._path = QLineEdit()
        self._path.editingFinished.connect(self._apply_path)
        f_path.addWidget(self._path)
        self._path_browse = HintButton("浏览")
        self._path_browse.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 6px;"
            " padding: 5px 10px 5px 20px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.16); }")
        self._path_browse.clicked.connect(self._browse_path)
        f_path.addWidget(self._path_browse)
        tf.addLayout(f_path)

        f_icon = QHBoxLayout()
        f_icon.addWidget(QLabel("图标"))
        self._icon = QLineEdit()
        self._icon.editingFinished.connect(self._apply_icon)
        f_icon.addWidget(self._icon)
        self._icon_browse = HintButton("图片")
        self._icon_browse.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 6px;"
            " padding: 5px 10px 5px 20px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.16); }")
        self._icon_browse.clicked.connect(self._browse_icon)
        f_icon.addWidget(self._icon_browse)
        self._icon_clear = HintButton("用目标图标")
        self._icon_clear.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 6px;"
            " padding: 5px 10px 5px 20px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.16); }")
        self._icon_clear.clicked.connect(self._clear_icon)
        f_icon.addWidget(self._icon_clear)
        tf.addLayout(f_icon)

        f_size = QHBoxLayout()
        f_size.addWidget(QLabel("尺寸"))
        self._size = _NoWheelComboBox()
        self._size.addItems(["1×1", "1×2", "2×1", "2×2"])
        self._size.currentTextChanged.connect(self._apply_size)
        f_size.addWidget(self._size, 1)
        self._show_name = QPushButton("显名")
        self._show_name.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 5px; padding: 3px 8px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.16); }")
        self._show_name.clicked.connect(self._toggle_show_name)
        f_size.addWidget(self._show_name)
        tf.addLayout(f_size)

        f_nc = QHBoxLayout()
        f_nc.addWidget(QLabel("名色"))
        self._name_color = _NoWheelComboBox()
        for label, rgb in _NAME_COLORS:
            self._name_color.addItem(label, rgb)
        self._name_color.currentTextChanged.connect(self._apply_name_color)
        f_nc.addWidget(self._name_color, 1)
        tf.addLayout(f_nc)

        # 边框色：点选预设 + 自定义取色。空点=跟随呼吸灯主题色；额外透明点=无边框。
        f_bdr = QHBoxLayout()
        f_bdr.addWidget(QLabel("边框"))
        self._border_row = _TMPresetRow(
            _TILE_BORDER_PRESETS, "", self._on_border_pick,
            allow_empty=True, empty_label="跟随呼吸灯主题色",
            transparent_value="none", transparent_label="透明（无边框）")
        f_bdr.addWidget(self._border_row, 1)
        tf.addLayout(f_bdr)

        # 背景色：点选预设 + 自定义取色。透明点=默认底色（空串），用红斜线棋盘点。
        f_bg = QHBoxLayout()
        f_bg.addWidget(QLabel("背景"))
        self._bg_row = _TMPresetRow(
            _TILE_BG_PRESETS, "", self._on_bg_pick,
            transparent_value="", transparent_label="透明（默认底色）")
        f_bg.addWidget(self._bg_row, 1)
        tf.addLayout(f_bg)

        # 新增磁贴：从「磁贴区块设置」卡移到此处，与「删除磁贴」同组在底部。
        self._add_tile_btn = HintButton("新增磁贴")
        self._add_tile_btn.setStyleSheet(
            "QPushButton { background: rgba(130,255,30,0.14); color: #b6ff8a;"
            " border: none; border-radius: 7px;"
            " padding: 7px 12px 7px 20px; }"
            "QPushButton:hover { background: rgba(130,255,30,0.28); }")
        self._add_tile_btn.clicked.connect(self._add_tile)
        self._del_tile_btn = HintButton("删除磁贴")
        self._del_tile_btn.setStyleSheet(
            "QPushButton { background: rgba(230,75,90,0.18); color: #ff9b9b;"
            " border: none; border-radius: 7px;"
            " padding: 6px 12px 6px 20px; }"
            "QPushButton:hover { background: rgba(230,75,90,0.32); }")
        self._del_tile_btn.clicked.connect(self._del_tile)
        f_ops = QHBoxLayout()
        f_ops.addWidget(self._add_tile_btn)
        f_ops.addWidget(self._del_tile_btn)
        tf.addLayout(f_ops)
        bl.addWidget(t_card)
        # 「卡片颜色」卡（含「跟随呼吸灯」开关）移至页面最底部。
        bl.addWidget(cc_card)

        # --- Footer ---
        foot = QHBoxLayout()
        self._del_block_btn = HintButton("删除区块")
        self._del_block_btn.setStyleSheet(
            "QPushButton { background: rgba(230,75,90,0.18); color: #ff9b9b;"
            " border: none; border-radius: 7px;"
            " padding: 7px 12px 7px 20px; }"
            "QPushButton:hover { background: rgba(230,75,90,0.32); }")
        self._del_block_btn.clicked.connect(self._del_block)
        self._save_btn = HintButton("保存")
        self._save_btn.setStyleSheet(
            "QPushButton { background: #82ff1e; color: #0c0f16; border: none;"
            " border-radius: 7px; padding: 7px 16px 7px 22px; font-weight: bold; }"
            "QPushButton:hover { background: #9bff52; }")
        self._save_btn.clicked.connect(self._save)
        self._cancel_btn = HintButton("取消")
        self._cancel_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.1); color: #dfe6f0;"
            " border: none; border-radius: 7px;"
            " padding: 7px 16px 7px 22px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.20); }")
        self._cancel_btn.clicked.connect(self._cancel)
        foot.addWidget(self._del_block_btn)
        foot.addStretch(1)
        foot.addWidget(self._save_btn)
        foot.addWidget(self._cancel_btn)
        # 保存(Y)/取消(B) 手柄提示常驻显示：进页面即见，不依赖手柄/键盘导航激活。
        self._save_btn.set_gamepad_hint("Y")
        self._save_btn.set_hint_always_visible(True)
        self._cancel_btn.set_gamepad_hint("B")
        self._cancel_btn.set_hint_always_visible(True)
        root.addLayout(foot)

    # -- card helper ----------------------------------------------------
    def _card(self, title: str, title_click: Any = None,
              title_color: str | None = None,
              title_size: int = 13) -> "_EditCard":
        return _EditCard(title, self, on_title_click=title_click,
                         title_color=title_color, title_size=title_size)

    # -- 疑惑解答（点击顶部「磁贴区块设置」标题打开）----------------
    def _open_faq(self) -> None:
        faq = self._overlay.faq()
        if faq is not None:
            faq.open_for("tile_edit", "鼠标 标题")

    # -- load / refresh ------------------------------------------------
    def edit_target(self, extra_block: Any = None) -> None:
        """Select which block to edit: ``None`` = the base tile block, an
        ``ExtraTileBlock`` = that additional block.  Mirrors PS1 where every
        ``New-TileBlockPanel`` gets its own editor scope."""
        self._extra = extra_block
        # Resolve this block's card-colour key: "tile" for the base block,
        # "tile#N" for the Nth additional block.
        if extra_block is None:
            key = "tile"
        else:
            extra_blocks = getattr(
                self._app.config_manager.config, "extra_tile_blocks", []) or []
            key = "tile"
            for i, eb in enumerate(extra_blocks):
                if eb is extra_block:
                    key = f"tile#{i}"
                    break
        if getattr(self, "_card_color_picker", None) is not None:
            self._card_color_picker.set_block_key(key)
        self.refresh()

    def _refresh_card_color_preview(self) -> None:
        # The tile page has no dedicated preview strip; recolor happens live on
        # the homepage + layout rows via CardColorPicker._persist_and_apply.
        pass

    def refresh(self) -> None:
        self.block = self._app.blocks.get("tile") if self._app else None
        if self._extra is None:
            self.work = [copy.deepcopy(t) for t in (self.block.tiles if self.block else [])]
            meta: Any = self.work[0] if self.work else None
        else:
            # Edit the additional block's own tiles + block-level metadata.
            self.work = [copy.deepcopy(t) for t in self._extra.tiles]
            meta = self._extra
        self.sel = len(self.work) - 1 if self.work else -1
        self._editing = True
        # 全局开关：启动后是否最小化本体（与选中哪个磁贴无关）。
        _cfg = getattr(getattr(self._app, "config_manager", None), "config", None)
        self._min_after_launch.setText(
            "已开启" if bool(getattr(_cfg, "tile_minimize_after_launch", False))
            else "已关闭")
        if meta is not None:
            bn = getattr(meta, "block_name", "") or ""
            self._block_name.setText(bn)
            show = bool(getattr(meta, "show_block_title", True))
            self._show_title.setText("隐标题" if not show else "显标题")
        else:
            self._block_name.clear()
            self._show_title.setText("显标题")
        self._load_sel()
        self._grid.set_data(self.work, self.sel, self._on_grid_sel)
        self._editing = False

    def _on_grid_sel(self, idx: int) -> None:
        self.sel = idx
        self._load_sel()

    def _load_sel(self) -> None:
        if not (0 <= self.sel < len(self.work)):
            self._name.clear()
            self._path.clear()
            self._icon.clear()
            self._size.setCurrentIndex(-1)
            self._show_name.setText("显名")
            self._show_name.setStyleSheet("")
            return
        t = self.work[self.sel]
        self._name.setText(t.name)
        self._path.setText(t.launch_path)
        self._icon.setText(t.icon_path)
        sz = f"{t.width}×{t.height}"
        i = self._size.findText(sz)
        self._size.setCurrentIndex(i if i >= 0 else 0)
        self._show_name.setText("隐名" if not t.show_tile_name else "显名")
        nc = t.tile_name_color
        for k in range(self._name_color.count()):
            if self._name_color.itemData(k) == nc:
                self._name_color.setCurrentIndex(k)
                break
        braw = getattr(t, "border_color", "") or ""
        # 边框特殊值：''=跟随呼吸灯主题色，'none'=透明（无边框）。
        self._border_row.set_current(
            "none" if braw == "none" else _rgb_str_to_hex(braw))
        self._bg_row.set_current(
            _rgb_str_to_hex(getattr(t, "bg_color", "") or ""))

    # -- property apply (live, into the working copy) ------------------
    def _auto_save(self) -> None:
        """Persist the current working copy to the block, save config, and
        refresh the HUD panel so it always reflects the latest edits."""
        if self.block is None:
            return
        new_work = [copy.deepcopy(t) for t in self.work]
        if self._extra is None:
            self.block.tiles = new_work
            try:
                self.block._rebuild()
            except Exception:
                pass
            self.block.save_config()
            if self._app is not None:
                self._app.save_all()
            try:
                self.block.refresh_panel()
            except Exception:
                pass
        else:
            # Additional block: write to its own tiles list (which lives inside
            # config.extra_tile_blocks) and keep tile[0] block-level metadata in
            # sync so the proxy panel's _sync_block_title shows name/colour/title.
            if new_work:
                new_work[0].block_name = self._extra.block_name
                new_work[0].show_block_title = self._extra.show_block_title
            self._extra.tiles = new_work
            if self._app is not None:
                self._app.save_all()
            # Refresh the matching proxy panel in place so the HUD tile cards
            # update live (the final HUD rebuild also happens on editor close).
            try:
                win = getattr(self._app, "_window", None)
                if win is not None:
                    for p in getattr(win, "panels", []) or []:
                        inst = getattr(p, "instance", None)
                        if getattr(inst, "_extra", None) is self._extra:
                            inst.tiles = new_work
                            p.sync()
                            break
            except Exception:
                pass

    def _toggle_min_after_launch(self) -> None:
        """全局开关：磁贴启动成功后是否自动最小化凝冰结魄本体。

        写回 AppConfig 后立即保存（save_all），无需等别的编辑项触发；
        只有成功启动才最小化，失败路径见 TileBlock.launch。
        """
        cfg = getattr(getattr(self._app, "config_manager", None), "config", None)
        if cfg is None:
            return
        new = not bool(getattr(cfg, "tile_minimize_after_launch", False))
        cfg.tile_minimize_after_launch = new
        self._min_after_launch.setText("已开启" if new else "已关闭")
        if self._app is not None:
            self._app.save_all()

    def _apply_block_name(self) -> None:
        name = self._block_name.text()
        for t in self.work:
            t.block_name = name
        if self._extra is not None:
            self._extra.block_name = name
        self._auto_save()

    def _toggle_show_title(self) -> None:
        if not self.work:
            return
        new = not self.work[0].show_block_title
        for t in self.work:
            t.show_block_title = new
        if self._extra is not None:
            self._extra.show_block_title = new
        self._show_title.setText("隐标题" if not new else "显标题")
        self._auto_save()

    def _apply_name(self) -> None:
        if 0 <= self.sel < len(self.work):
            self.work[self.sel].name = self._name.text() or "新磁贴"
            self._grid.rebuild()
            self._auto_save()

    def _apply_path(self) -> None:
        if 0 <= self.sel < len(self.work):
            t = self.work[self.sel]
            t.launch_path = self._path.text()
            if self.block is not None:
                t.launch_type = self.block.detect_type(t.launch_path)
            self._grid.rebuild()
            self._auto_save()

    def _apply_icon(self) -> None:
        if 0 <= self.sel < len(self.work):
            self.work[self.sel].icon_path = self._icon.text()
            self._grid.rebuild()
            self._auto_save()

    def _apply_size(self, text: str) -> None:
        if 0 <= self.sel < len(self.work) and "×" in text:
            w, h = text.split("×")
            self.work[self.sel].width = int(w)
            self.work[self.sel].height = int(h)
            self._grid.rebuild()
            self._auto_save()

    def _toggle_show_name(self) -> None:
        if 0 <= self.sel < len(self.work):
            new = not self.work[self.sel].show_tile_name
            self.work[self.sel].show_tile_name = new
            self._show_name.setText("隐名" if not new else "显名")
            self._grid.rebuild()
            self._auto_save()

    def _apply_name_color(self, _: str) -> None:
        if 0 <= self.sel < len(self.work):
            self.work[self.sel].tile_name_color = self._name_color.currentData() or "255,255,255"
            self._auto_save()

    def _on_border_pick(self, token: str) -> None:
        """点选/自定义取色后写入边框色。token：hex 具体色 / '' 跟随主题 / 'none' 透明无边框。"""
        if 0 <= self.sel < len(self.work):
            # 'none' 是特殊透明标记，不转 hex；其余 hex -> 'R,G,B'（''=跟随主题）。
            self.work[self.sel].border_color = (
                "none" if token == "none" else _hex_to_rgb_str(token))
            self._grid.rebuild()
            self._auto_save()

    def _on_bg_pick(self, hexv: str) -> None:
        """点选/自定义取色后写入背景色（hex -> 'R,G,B'，空串=透明）。"""
        if 0 <= self.sel < len(self.work):
            self.work[self.sel].bg_color = _hex_to_rgb_str(hexv)
            self._grid.rebuild()
            self._auto_save()

    def _browse_path(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "选择启动目标", "", "全部文件 (*.*)")
        if p:
            self._path.setText(p)
            self._apply_path()

    def _browse_icon(self) -> None:
        p, _ = QFileDialog.getOpenFileName(
            self, "选择图标/图片", "",
            "图片 (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;全部 (*.*)")
        if p:
            self._icon.setText(p)
            self._apply_icon()

    def _clear_icon(self) -> None:
        self._icon.clear()
        if 0 <= self.sel < len(self.work):
            self.work[self.sel].icon_path = ""
            self._grid.rebuild()

    # -- add / delete --------------------------------------------------
    def _add_tile(self) -> None:
        # Inherit the block-level metadata so the new tile belongs to this
        # block (name / theme colour / title visibility) — matching PS1 where
        # every tile in a block shares those values.
        if self._extra is not None:
            bname = self._extra.block_name
            bshow = self._extra.show_block_title
        elif self.work:
            bname = self.work[0].block_name
            bshow = self.work[0].show_block_title
        else:
            bname, bshow = "", True
        # Drop the new tile into the first slot large enough for its 1×1
        # footprint (footprint-aware, so it never lands on top of another tile).
        slot = (0, 0)
        fs = self._grid._free_slot(1, 1)
        if fs is not None:
            slot = fs
        t = Tile(name="新磁贴", launch_path="", launch_type="", icon_path="",
                 width=1, height=1, col=slot[0], row=slot[1],
                 tile_name_color="255,255,255", block_name=bname,
                 show_block_title=bshow,
                 show_tile_name=True)
        self.work.append(t)
        self.sel = len(self.work) - 1
        self._load_sel()
        self._grid.set_data(self.work, self.sel, self._on_grid_sel)
        self._auto_save()

    def _del_tile(self) -> None:
        if 0 <= self.sel < len(self.work):
            self.work.pop(self.sel)
            self.sel = min(self.sel, len(self.work) - 1)
            self._load_sel()
            self._grid.set_data(self.work, self.sel, self._on_grid_sel)
            self._auto_save()

    def _del_block(self) -> None:
        # An additional block can be fully removed; the base block can't, so we
        # just clear its tiles.
        if self._extra is not None:
            cfg = self._app.config_manager.config
            cfg.extra_tile_blocks = [
                eb for eb in (getattr(cfg, "extra_tile_blocks", []) or [])
                if eb is not self._extra
            ]
            self._app.save_all()
            try:
                getattr(self._app, "_window", None).rebuild()
            except Exception:
                pass
            self._extra = None
            self._overlay.open_layout()
            return
        self.work = []
        self.sel = -1
        self._load_sel()
        self._grid.set_data(self.work, self.sel, self._on_grid_sel)
        self._auto_save()

    # -- commit -------------------------------------------------------
    def _save(self) -> None:
        if self.block is not None:
            if self._extra is None:
                self.block.tiles = [copy.deepcopy(t) for t in self.work]
                try:
                    self.block._rebuild()
                except Exception:
                    pass
                self.block.save_config()
            else:
                # Persist the additional block's tiles (the in-place HUD refresh
                # already ran via _auto_save on each edit).
                self._extra.tiles = [copy.deepcopy(t) for t in self.work]
            self._app.save_all()
            try:
                self.block.refresh_panel()
            except Exception:
                pass
            self.block.show_toast("已保存磁贴", success=True)
        self._overlay.open_layout()

    def _cancel(self) -> None:
        self._overlay.open_layout()

    # -- nav items -----------------------------------------------------
    def nav_items(self) -> list[NavItem]:
        items: list[NavItem] = []
        items.append(NavItem(self._block_name, "input",
                             confirm=lambda w=self._block_name: w.setFocus()))
        items.append(NavItem(self._show_title, "toggle",
                             confirm=self._toggle_show_title))
        items.append(NavItem(self._add_tile_btn, "action",
                             confirm=self._add_tile, hint="A"))
        items.append(NavItem(self._grid, "grid",
                             confirm=self._grid.confirm, nudge=self._grid.nudge))
        items.append(NavItem(self._name, "input",
                             confirm=lambda w=self._name: w.setFocus()))
        items.append(NavItem(self._path, "input",
                             confirm=lambda w=self._path: w.setFocus()))
        items.append(NavItem(self._path_browse, "action",
                             confirm=lambda: self._browse_path(), hint="A"))
        items.append(NavItem(self._icon, "input",
                             confirm=lambda w=self._icon: w.setFocus()))
        items.append(NavItem(self._icon_browse, "action",
                             confirm=lambda: self._browse_icon(), hint="A"))
        items.append(NavItem(self._icon_clear, "action",
                             confirm=lambda: self._clear_icon(), hint="A"))
        items.append(NavItem(self._size, "select",
                             confirm=lambda w=self._size: (w.setFocus(), w.showPopup())))
        items.append(NavItem(self._show_name, "toggle",
                             confirm=self._toggle_show_name))
        items.append(NavItem(self._name_color, "select",
                             confirm=lambda w=self._name_color: (w.setFocus(), w.showPopup())))
        # 边框/背景色：预设点用鼠标点选；手柄导航只挂「自定义」取色按钮。
        items.append(NavItem(self._border_row.custom_button(), "action",
                             confirm=self._border_row._pick_custom, hint="A"))
        items.append(NavItem(self._bg_row.custom_button(), "action",
                             confirm=self._bg_row._pick_custom, hint="A"))
        items.append(NavItem(self._del_tile_btn, "action",
                             confirm=self._del_tile, hint="X"))
        items.append(NavItem(self._del_block_btn, "action",
                             confirm=self._del_block, hint="X"))
        # 卡片颜色 preset picker (swatches are mouse-only, like the breathing
        # editor; only the 自定义 / 跟随 buttons are keyboard-navigable).
        if getattr(self, "_card_color_picker", None) is not None:
            items.append(NavItem(self._card_color_picker._custom_btn, "action",
                                 confirm=self._card_color_picker._pick_custom, hint="C"))
            items.append(NavItem(self._card_color_picker._follow_btn, "action",
                                 confirm=self._card_color_picker._follow, hint="A"))
        items.append(NavItem(self._save_btn, "action",
                             confirm=self._save, hint="Y"))
        items.append(NavItem(self._cancel_btn, "action",
                             confirm=self._cancel, hint="B"))
        return items


# --------------------------------------------------------------------------
# ProcMon editor (port of Build-ProcMonEditLayout)
# --------------------------------------------------------------------------
class ProcMonEditPanel(QWidget):
    def __init__(self, app: Any, overlay: Any) -> None:
        super().__init__()
        self.setStyleSheet(
            "QLabel { background: transparent; border: none; }"
            "QLineEdit { background: #0e1117; color: #dfe6f0;"
            " border: 1px solid rgba(255,255,255,0.10);"
            " border-radius: 4px; padding: 3px 6px; }"
            "QLineEdit:focus { border: 1px solid rgba(255,255,255,0.18); }"
            "QPushButton { border: none; }"
            "QPushButton:hover { border: none; }"
            "QPushButton:pressed { border: none; }"
            "QPushButton:focus { border: none; }"
            "QPushButton:checked { border: none; }"
        )
        self._app = app
        self._overlay = overlay
        self.block = None
        self.row = -1

        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(10, 10, 10, 10)

        # ---- header: back | 标题（点击打开疑惑解答）----
        header = QHBoxLayout()
        header.setSpacing(8)
        back = QPushButton("← 返回")
        back.setFixedHeight(24)
        back.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #cfe6ff;"
            " border: none; border-radius: 5px;"
            " padding: 2px 10px; font-size: 11px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.16); }")
        back.clicked.connect(lambda _=False: self._overlay.open_layout())
        header.addWidget(back)
        title = ClickableTitle(_card_mark_name("进程监控设置"),
                                color=_block_card_color_hex(self._app, "procmon"),
                                size=PAGE_TITLE_SIZE)
        title.clicked.connect(lambda _=False: self._open_faq())
        header.addWidget(title)
        header.addStretch(1)
        root.addLayout(header)

        self.lst = QListWidget()
        self.lst.setStyleSheet(
            "QListWidget { background: #14161c; color: #dfe6f0; border: none;"
            " border-radius: 6px; font-size: 11px; }")
        self.lst.currentRowChanged.connect(self._on_sel)
        root.addWidget(self.lst, 1)

        f_name = QHBoxLayout()
        f_name.addWidget(QLabel("名称"))
        self.name = QLineEdit()
        f_name.addWidget(self.name)
        root.addLayout(f_name)

        f_path = QHBoxLayout()
        f_path.addWidget(QLabel("路径"))
        self.path = QLineEdit()
        f_path.addWidget(self.path)
        self._browse_btn = HintButton("浏览")
        self._browse_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 6px;"
            " padding: 5px 10px 5px 20px; }")
        self._browse_btn.clicked.connect(self._browse)
        f_path.addWidget(self._browse_btn)
        root.addLayout(f_path)

        row = QHBoxLayout()
        _pm_style = (
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 6px;"
            " padding: 6px 12px 6px 20px; }")
        self._add_btn = HintButton("新增")
        self._del_btn = HintButton("删除")
        self._save_btn = HintButton("保存")
        self._close = HintButton("关闭")
        for _b in (self._add_btn, self._del_btn, self._save_btn, self._close):
            _b.setStyleSheet(_pm_style)
        self._add_btn.clicked.connect(self._add)
        self._del_btn.clicked.connect(self._del)
        self._save_btn.clicked.connect(self._save)
        self._close.clicked.connect(self._overlay.open_layout)
        row.addWidget(self._add_btn)
        row.addWidget(self._del_btn)
        row.addWidget(self._save_btn)
        row.addWidget(self._close)
        root.addLayout(row)

        # --- card colour (title + frame preset picker)，置于按钮下方，
        #     不再把「监控进程设置」与新增/删除等按钮隔开 ---
        cc_lbl = QLabel("卡片颜色")
        cc_lbl.setStyleSheet("color: #7fd1ff; font-weight: bold; font-size: 13px;")
        root.addWidget(cc_lbl)
        self._card_color_picker = CardColorPicker(
            self._app, self._overlay, "procmon")
        root.addWidget(self._card_color_picker)

    # -- 疑惑解答（点击顶部「进程监控设置」标题打开）----------------
    def _open_faq(self) -> None:
        faq = self._overlay.faq()
        if faq is not None:
            faq.open_for("procmon_edit", "鼠标 标题")

    def refresh(self) -> None:
        self.block = self._app.blocks.get("procmon") if self._app else None
        self._refresh()

    def _refresh(self) -> None:
        self.lst.blockSignals(True)
        self.lst.clear()
        for c in (self.block.configs if self.block else []):
            # 空名称（新建尚未命名的监控项）显示占位文本，避免列表里出现
            # 裸空白行。
            self.lst.addItem((c.get("Name") or "").strip() or "(新监控项)")
        self.lst.blockSignals(False)
        if self.block and self.block.configs:
            self.lst.setCurrentRow(0)
            self._on_sel(0)

    def _on_sel(self, r: int) -> None:
        self.row = r
        if self.block and 0 <= r < len(self.block.configs):
            self.name.setText(self.block.configs[r].get("Name", ""))
            self.path.setText(self.block.configs[r].get("Path", ""))

    def _browse(self) -> None:
        p, _ = QFileDialog.getOpenFileName(
            self, "选择脚本 / 程序", "",
            "AutoHotkey 脚本 (*.ahk);;"
            "脚本 (*.bat *.cmd *.ps1 *.vbs *.ahk);;"
            "可执行文件 (*.exe);;全部 (*.*)")
        if p:
            self.path.setText(p)

    def _add(self) -> None:
        if not self.block:
            return
        # 新建监控进程：从干净的「空条目」开始，不要继承当前选中项的
        # 名称 / 路径（否则会复制出一条一模一样的旧条目）。随后把输入框也
        # 清空并把焦点放到名称框，让用户从零填写（进程属性啥都不要有）。
        self.block.add_config("", "")
        self._refresh()
        self.lst.setCurrentRow(len(self.block.configs) - 1)
        # _refresh -> _on_sel 已把输入框同步成空条目的 ""，这里再显式清空并
        # 置焦点，确保新建那一刻输入框里没有任何旧内容。
        self.name.setText("")
        self.path.setText("")
        self.name.setFocus()

    def _del(self) -> None:
        r = self.lst.currentRow()
        if self.block and r >= 0:
            self.block.remove_config(r)
            self._refresh()

    def _save(self) -> None:
        r = self.lst.currentRow()
        if not (self.block and 0 <= r < len(self.block.configs)):
            return
        try:
            self.block.configs[r]["Name"] = self.name.text()
            self.block.configs[r]["Path"] = self.path.text()
            self.block.save_config()
            self._app.save_all()
            self.block.show_toast("已保存监控项", success=True)
        except Exception as exc:  # 任何异常都不让程序闪退，改为给出失败提示
            try:
                self.block.show_toast(f"保存失败：{exc}", success=False)
            except Exception:
                pass

    def nav_items(self) -> list[NavItem]:
        items: list[NavItem] = []
        items.append(NavItem(self.name, "input",
                             confirm=lambda w=self.name: w.setFocus()))
        items.append(NavItem(self.path, "input",
                             confirm=lambda w=self.path: w.setFocus()))
        items.append(NavItem(self._browse_btn, "action",
                             confirm=lambda: self._browse(), hint="A"))
        items.append(NavItem(self._add_btn, "action",
                             confirm=self._add, hint="A"))
        items.append(NavItem(self._del_btn, "action",
                             confirm=self._del, hint="X"))
        if getattr(self, "_card_color_picker", None) is not None:
            items.append(NavItem(self._card_color_picker._custom_btn, "action",
                                 confirm=self._card_color_picker._pick_custom, hint="C"))
            items.append(NavItem(self._card_color_picker._follow_btn, "action",
                                 confirm=self._card_color_picker._follow, hint="A"))
        items.append(NavItem(self._save_btn, "action",
                             confirm=self._save, hint="Y"))
        items.append(NavItem(self._close, "action",
                             confirm=self._overlay.open_layout, hint="B"))
        return items


# --------------------------------------------------------------------------
# Freeze editor — single boolean toggle (显示 Windows 应用)
# --------------------------------------------------------------------------
class FreezeEditPanel(QWidget):
    """进程冻结卡片编辑项。

    Controls:
      * 显示 Windows 应用 — toggle the NOISE processes in the candidate list.
      * 优先前台窗口 — when on, processes with a visible window rank above
        background ones of equal game/memory tier.
      * 排除列表 — a checkable list of candidate / already-excluded process
        names.  Checking one adds it to ``FreezeBlock.hide_list`` (persisted to
        layout.json) so it never appears in the freeze candidate list again.
      * 清空排除列表 — reset the user exclusion list (built-in SERVICE_HELPERS
        blacklist is unaffected).

    Hard-dangerous processes (dwm, lsass, …) stay hidden regardless — see
    ``FreezeBlock.EXCLUDED``.
    """

    def __init__(self, app: Any, overlay: Any) -> None:
        super().__init__()
        self.setStyleSheet(
            "QLabel { background: transparent; border: none; }"
            "QPushButton { border: none; }"
            "QPushButton:hover { border: none; }"
            "QPushButton:pressed { border: none; }"
            "QPushButton:focus { border: none; }"
            "QPushButton:checked { border: none; }"
            "QListWidget { background: #14161c; color: #dfe6f0; border: none;"
            " border-radius: 8px; font-size: 11px; }"
            "QListWidget::item { padding: 4px 6px; }"
            "QListWidget::item:checked { color: #ff9b7a; }")
        self._app = app
        self._overlay = overlay
        self.block = None
        # 后台枚举的代际计数器：每次进入 / 刷新本页自增，丢弃过期的枚举结果，
        # 避免后台线程把旧的候选列表填回已切走的页面。
        self._enum_gen = 0

        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(12, 12, 12, 12)

        title = QLabel(_card_mark_name("进程冻结") + " · 编辑项")
        title.setStyleSheet(
            f"QLabel{{color: {_block_card_color_hex(self._app, 'freeze')};"
            f"font-size: {PAGE_TITLE_SIZE}px; font-weight: bold;}}")
        root.addWidget(title)

        # --- toggle: show Windows apps --------------------------------
        self._toggle = QPushButton("显示 Windows 应用")
        self._toggle.setMinimumHeight(38)
        self._toggle.setCheckable(True)
        self._toggle.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 8px;"
            " padding: 6px 14px 6px 14px; font-size: 13px; }"
            "QPushButton:checked { background: rgba(130,255,30,0.22);"
            " color: #b6ff8a; border: none; }")
        self._toggle.clicked.connect(self._on_toggle)
        root.addWidget(self._toggle)

        # --- toggle: prioritise foreground windows --------------------
        self._fg_toggle = QPushButton("优先前台窗口")
        self._fg_toggle.setMinimumHeight(38)
        self._fg_toggle.setCheckable(True)
        self._fg_toggle.setStyleSheet(self._toggle.styleSheet())
        self._fg_toggle.clicked.connect(self._on_fg_toggle)
        root.addWidget(self._fg_toggle)

        # --- toggle: minimise app after un-freeze ---------------------
        self._min_toggle = QPushButton("解冻后最小化")
        self._min_toggle.setMinimumHeight(38)
        self._min_toggle.setCheckable(True)
        self._min_toggle.setStyleSheet(self._toggle.styleSheet())
        self._min_toggle.clicked.connect(self._on_min_toggle)
        root.addWidget(self._min_toggle)

        warn = QLabel(
            "⚠ 受保护/关键系统进程（dwm、lsass、winlogon 等）"
            "始终隐藏，不受此开关影响。")
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #ffb27a; font-size: 11px;")
        root.addWidget(warn)

        # --- exclusion list -------------------------------------------
        ex_lbl = QLabel("排除以下进程（冻结候选列表不再显示）")
        ex_lbl.setStyleSheet("color: #7fd1ff; font-weight: bold; font-size: 13px;")
        root.addWidget(ex_lbl)
        self._list = QListWidget()
        self._list.setMinimumHeight(150)
        self._list.setMaximumHeight(220)
        self._list.itemChanged.connect(self._on_hide_changed)
        root.addWidget(self._list)

        clear = QPushButton("清空排除列表")
        clear.setMinimumHeight(34)
        clear.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 6px; padding: 6px 14px; font-size: 11px; }"
            "QPushButton:hover { background: rgba(255,120,90,0.22); }")
        clear.clicked.connect(self._on_clear)
        self._clear = clear
        hb = QHBoxLayout()
        hb.addStretch(1)
        hb.addWidget(clear)
        root.addLayout(hb)

        # --- 解冻诊断（一键抓取解冻失败现场，只读） -------------------
        diag_note = QLabel(
            "解冻诊断：在游戏「冻结成功但解冻失败/卡住/窗口回不来」后点击，"
            "抓取冻结组存活状态、窗口句柄、虚拟桌面日志现场，写入安装目录 "
            "diag_unfreeze_<时间戳>.txt（只读，不改变游戏状态）。")
        diag_note.setWordWrap(True)
        diag_note.setStyleSheet("color: #aeb9c9; font-size: 11px;")
        root.addWidget(diag_note)

        self._diag_btn = HintButton("解冻诊断")
        self._diag_btn.set_style("#7fd1ff", False)
        # 固定宽度：文字随状态切换（解冻诊断 ↔ 诊断中…），避免挤动同行。
        self._diag_btn.reserve_width_for(["解冻诊断", "诊断中…"])
        self._diag_btn.clicked.connect(lambda _=False: self._on_diag())
        diag_row = QHBoxLayout()
        diag_row.addWidget(self._diag_btn)
        diag_row.addStretch(1)
        root.addLayout(diag_row)

        # 诊断状态 1s 轮询：运行中按钮显示「诊断中…」；后台线程完成后
        # （_diag_active 置 False，此处轮询捕获转换）弹完成 toast 含报告路径。
        # 面板常驻编辑 overlay，定时器不随页面切换销毁——离开编辑页诊断
        # 结束后同样能收到完成 toast。
        self._diag_timer = QTimer(self)
        self._diag_timer.setInterval(1000)
        self._diag_timer.timeout.connect(self._diag_tick)
        self._diag_active = False
        self._diag_was_active = False
        self._diag_path = ""

        # --- card colour (title + frame preset picker) ----------------
        cc_lbl = QLabel("卡片颜色")
        cc_lbl.setStyleSheet("color: #7fd1ff; font-weight: bold; font-size: 13px;")
        root.addWidget(cc_lbl)
        self._card_color_picker = CardColorPicker(
            self._app, self._overlay, "freeze")
        root.addWidget(self._card_color_picker)

        root.addStretch(1)

        close = HintButton("关闭")
        close.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 6px;"
            " padding: 6px 14px 6px 18px; }")
        close.clicked.connect(self._overlay.open_layout)
        self._close = close
        hb2 = QHBoxLayout()
        hb2.addStretch(1)
        hb2.addWidget(close)
        root.addLayout(hb2)

    # -- exclusion helpers -------------------------------------------
    def _norm_name(self, name: str) -> str:
        n = (name or "").lower()
        if n.endswith(".exe"):
            n = n[:-4]
        return n

    def _rebuild_list(self) -> None:
        """重建排除列表。

        候选进程来自 ``block.pm.enumerate()``，它会同步拉起 wmic /
        Get-CimInstance（最坏 20s~45s）。若直接在 UI 线程跑，进入本编辑项
        页面会让界面卡死。这里先清空列表，再把枚举放到后台线程，完成后用
        代际计数器丢弃过期结果、把条目回填到主线程。
        """
        block = self.block
        if block is None:
            self._list.blockSignals(True)
            self._list.clear()
            self._list.blockSignals(False)
            return
        # 先清空，避免旧条目闪一下（枚举很快回填，无需加载占位）。
        self._list.blockSignals(True)
        self._list.clear()
        self._list.blockSignals(False)

        gen = self._enum_gen = self._enum_gen + 1
        block_ref = block

        def work() -> tuple[list[str], set[str]]:
            names: list[str] = []
            seen: set[str] = set()
            try:
                pm = getattr(block_ref, "pm", None)
                if pm is not None and getattr(pm, "available", False):
                    procs = pm.enumerate()
                    for p in block_ref.resolve_candidates(procs):
                        nm = self._norm_name(getattr(p, "name", "") or "")
                        if nm and nm not in seen:
                            seen.add(nm)
                            names.append(nm)
            except Exception:
                pass
            for h in block_ref.hide_list:
                if h and h not in seen:
                    seen.add(h)
                    names.append(h)
            return names, set(block_ref.hide_list)

        def done(result: tuple[list[str], set[str]]) -> None:
            # 已离开 / 重进本页（代际变化）或卡片已切换则丢弃过期结果，
            # 防止把旧的候选列表填回当前页面。
            if gen != self._enum_gen or self.block is not block_ref:
                return
            self._populate_list(result[0], result[1])

        def worker() -> None:
            res = work()
            QTimer.singleShot(0, lambda: done(res))

        threading.Thread(target=worker, daemon=True).start()

    def _populate_list(self, names: list[str], hidden: set[str]) -> None:
        """把候选 / 已排除的进程名填回排除列表（在主线程调用）。"""
        self._list.blockSignals(True)
        self._list.clear()
        for nm in sorted(names):
            it = QListWidgetItem(nm)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if nm in hidden else Qt.Unchecked)
            self._list.addItem(it)
        self._list.blockSignals(False)

    def _on_hide_changed(self, item: "QListWidgetItem") -> None:
        block = self.block
        if block is None:
            return
        nm = item.text()
        hidden = item.checkState() == Qt.Checked
        block.set_hidden(nm, hidden)
        # set_hidden calls refresh_panel; the live panel repaints via the app
        # bridge.  Persist to layout.json.
        try:
            self._app.save_all()
        except Exception:
            pass

    def _on_clear(self) -> None:
        block = self.block
        if block is None:
            return
        block.clear_hidden()
        try:
            self._app.save_all()
        except Exception:
            pass
        self._rebuild_list()
        try:
            block.show_toast("已清空用户排除列表", success=True)
        except Exception:
            pass

    # -- 解冻诊断（后台线程跑现场抓取，完成后 toast 报告路径） ---------
    def _on_diag(self) -> None:
        if self._diag_active:
            try:
                self.block.show_toast("解冻诊断已在运行中…", success=True)
            except Exception:
                pass
            return
        self._diag_active = True
        self._diag_path = ""
        root = getattr(self._app, "script_root", None) or os.getcwd()
        self._diag_btn.setText("诊断中…")
        self._diag_btn.set_style("#7fd1ff", True)
        self._diag_was_active = True
        self._diag_timer.start()

        def worker() -> None:
            path = ""
            try:
                from ningbingjue.platform.diag_unfreeze import run_diag_to_file
                path = run_diag_to_file(root, fix=False) or ""
            except Exception:
                path = ""
            # 直接写标志（与 GPU 监测同模式）：后台线程改 Python 属性，
            # UI 侧 _diag_tick 轮询捕获转换。切勿在此用 QTimer.singleShot——
            # 从无事件循环的线程调用它，回调永远不会执行。
            self._diag_path = path
            self._diag_active = False

        threading.Thread(target=worker, daemon=True).start()

    def _diag_tick(self) -> None:
        if self._diag_active:
            if self._diag_btn.text() != "诊断中…":
                self._diag_btn.setText("诊断中…")
                self._diag_btn.set_style("#7fd1ff", True)
            self._diag_was_active = True
            if not self._diag_timer.isActive():
                self._diag_timer.start()
        else:
            if self._diag_btn.text() != "解冻诊断":
                self._diag_btn.setText("解冻诊断")
                self._diag_btn.set_style("#7fd1ff", False)
            if self._diag_was_active:
                self._diag_was_active = False
                self._diag_timer.stop()
                blk = self.block
                if blk is None:
                    return
                try:
                    if self._diag_path:
                        blk.show_toast(
                            f"✓ 解冻诊断完成，报告：{self._diag_path}",
                            success=True)
                    else:
                        blk.show_toast(
                            "✗ 解冻诊断失败，请查看安装目录日志", success=False)
                except Exception:
                    pass

    def refresh(self) -> None:
        self.block = self._app.blocks.get("freeze") if self._app else None
        on = bool(getattr(self.block, "show_windows_apps", False)) if self.block else False
        self._toggle.blockSignals(True)
        self._toggle.setChecked(on)
        self._toggle.setText("显示 Windows 应用：开" if on else "显示 Windows 应用：关")
        self._toggle.blockSignals(False)

        fg = bool(getattr(self.block, "prioritize_foreground", True)) if self.block else True
        self._fg_toggle.blockSignals(True)
        self._fg_toggle.setChecked(fg)
        self._fg_toggle.setText("优先前台窗口：开" if fg else "优先前台窗口：关")
        self._fg_toggle.blockSignals(False)

        mn = bool(getattr(self.block, "minimize_on_unfreeze", True)) if self.block else True
        self._min_toggle.blockSignals(True)
        self._min_toggle.setChecked(mn)
        self._min_toggle.setText("解冻后最小化：开" if mn else "解冻后最小化：关")
        self._min_toggle.blockSignals(False)

        # 进入页面时同步诊断按钮状态（诊断仍在运行则显示「诊断中…」）
        self._diag_tick()

        self._rebuild_list()

    def _on_toggle(self) -> None:
        if not self.block:
            return
        on = self._toggle.isChecked()
        self.block.show_windows_apps = on
        self._toggle.setText("显示 Windows 应用：开" if on else "显示 Windows 应用：关")
        # Persist through the shared OnSaveConfig hook, then layout.json.
        try:
            self.block.save_config()
        except Exception:
            pass
        try:
            self._app.save_all()
        except Exception:
            pass
        # Re-filter the live candidate list immediately (harmless if hidden).
        try:
            self.block.refresh_panel()
        except Exception:
            pass
        try:
            self.block.show_toast(
                "已显示 Windows 应用" if on else "已隐藏 Windows 应用", success=True)
        except Exception:
            pass

    def _on_fg_toggle(self) -> None:
        if not self.block:
            return
        on = self._fg_toggle.isChecked()
        self.block.prioritize_foreground = on
        self._fg_toggle.setText("优先前台窗口：开" if on else "优先前台窗口：关")
        try:
            self.block.save_config()
        except Exception:
            pass
        try:
            self._app.save_all()
        except Exception:
            pass
        try:
            self.block.refresh_panel()
        except Exception:
            pass
        try:
            self.block.show_toast(
                "已优先前台窗口进程" if on else "已关闭前台优先", success=True)
        except Exception:
            pass

    def _on_min_toggle(self) -> None:
        if not self.block:
            return
        on = self._min_toggle.isChecked()
        self.block.minimize_on_unfreeze = on
        self._min_toggle.setText("解冻后最小化：开" if on else "解冻后最小化：关")
        try:
            self.block.save_config()
        except Exception:
            pass
        try:
            self._app.save_all()
        except Exception:
            pass
        try:
            self.block.show_toast(
                "解冻后将最小化程序" if on else "已关闭解冻自动最小化", success=True)
        except Exception:
            pass

    def nav_items(self) -> list[NavItem]:
        items = [
            NavItem(self._toggle, "toggle", confirm=self._on_toggle, hint="A"),
            NavItem(self._fg_toggle, "toggle", confirm=self._on_fg_toggle, hint="Y"),
            NavItem(self._min_toggle, "toggle", confirm=self._on_min_toggle, hint="U"),
            NavItem(self._clear, "action", confirm=self._on_clear, hint="X"),
            NavItem(self._diag_btn, "action", confirm=self._on_diag, hint="D"),
        ]
        if getattr(self, "_card_color_picker", None) is not None:
            items.append(NavItem(self._card_color_picker._custom_btn, "action",
                                 confirm=self._card_color_picker._pick_custom, hint="C"))
            items.append(NavItem(self._card_color_picker._follow_btn, "action",
                                 confirm=self._card_color_picker._follow, hint="A"))
        items.append(NavItem(self._close, "action",
                             confirm=self._overlay.open_layout, hint="B"))
        return items


class PowerEditPanel(QWidget):
    """电源设置卡片编辑项：GPU 监测 + 卡片颜色。

    自动降压相关的开关已整体迁至独立的「降压控制」卡片编辑项
    （UndervoltEditPanel），本面板不再持有任何降压逻辑。GPU 监测（2 分钟
    诊断录制）原在卡片主页，已移至本编辑项页面。
    """

    def __init__(self, app: Any, overlay: Any) -> None:
        super().__init__()
        self.setStyleSheet(
            "QLabel { background: transparent; border: none; }"
            "QPushButton { border: none; }"
            "QPushButton:hover { border: none; }"
            "QPushButton:pressed { border: none; }"
            "QPushButton:focus { border: none; }"
            "QPushButton:checked { border: none; }")
        self._app = app
        self._overlay = overlay
        self._key = "power"

        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(12, 12, 12, 12)

        title = QLabel(_card_mark_name("电源设置") + " · 编辑项")
        title.setStyleSheet(
            f"QLabel{{color: {_block_card_color_hex(self._app, 'power')};"
            f"font-size: {PAGE_TITLE_SIZE}px; font-weight: bold;}}")
        root.addWidget(title)

        note = QLabel("电源相关开关（TDP / GPU / 锁帧）在卡片面上直接调节；"
                      "降压控制已独立成「降压控制」卡片。")
        note.setWordWrap(True)
        note.setStyleSheet("color: #aeb9c9; font-size: 11px;")
        root.addWidget(note)

        # --- GPU 监测（2 分钟诊断录制，原在卡片主页）------------------
        gm_lbl = QLabel("GPU 监测")
        gm_lbl.setStyleSheet("color: #7fd1ff; font-weight: bold; font-size: 13px;")
        root.addWidget(gm_lbl)

        gm_note = QLabel(
            "录制 2 分钟 GPU 占用与浮动锁频诊断日志（列含 util / known / float / "
            "freq / ceiling / applied_gfx / circuit / sampler 等），用于排查 GPU 占用"
            "读不到、浮动锁频下发失败等根因，完成后写入安装目录 logs/。")
        gm_note.setWordWrap(True)
        gm_note.setStyleSheet("color: #aeb9c9; font-size: 11px;")
        root.addWidget(gm_note)

        self._gpu_monitor_btn = HintButton("GPU监测")
        self._gpu_monitor_btn.set_style("#7fd1ff", False)
        # 固定宽度：文字会随状态切换（GPU监测 ↔ 监测中…），避免切换时挤动同行。
        self._gpu_monitor_btn.reserve_width_for(["GPU监测", "监测中…"])
        self._gpu_monitor_btn.clicked.connect(
            lambda _=False: self._on_gpu_monitor())
        gm_row = QHBoxLayout()
        gm_row.addWidget(self._gpu_monitor_btn)
        gm_row.addStretch(1)
        root.addLayout(gm_row)

        # 监测状态 1s 轮询：录制中按钮显示「监测中…」；录制结束（后台线程置
        # _gpu_monitor_active=False，此处轮询捕获转换）弹完成 toast 含日志路径。
        # 面板常驻编辑 overlay，定时器不随页面切换销毁——离开编辑页监测结束后
        # 同样能收到完成 toast。
        self._monitor_timer = QTimer(self)
        self._monitor_timer.setInterval(1000)
        self._monitor_timer.timeout.connect(self._monitor_tick)
        self._monitor_was_active = False

        # --- 卡片颜色 --------------------------------------------------
        cc_lbl = QLabel("卡片颜色")
        cc_lbl.setStyleSheet("color: #7fd1ff; font-weight: bold; font-size: 13px;")
        root.addWidget(cc_lbl)
        self._card_color_picker = CardColorPicker(
            self._app, self._overlay, "power")
        root.addWidget(self._card_color_picker)

        root.addStretch(1)

        close = HintButton("关闭")
        close.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 6px;"
            " padding: 6px 14px 6px 18px; }")
        close.clicked.connect(self._overlay.open_layout)
        self._close = close
        hb = QHBoxLayout()
        hb.addStretch(1)
        hb.addWidget(close)
        root.addLayout(hb)

    # -- 公开 API ------------------------------------------------------
    def edit_target(self, token: str) -> None:
        self._key = token or "power"
        self._card_color_picker.set_block_key(self._key)
        # 进入页面时同步监测按钮状态：若监测仍在录制中，按钮直接显示「监测中…」
        # 并启动轮询；已结束则复位为「GPU监测」。
        self._monitor_tick()
        self.refresh()

    def refresh(self) -> None:
        pass

    def _block(self) -> Any:
        return self._app.blocks.get("power") if self._app else None

    # -- GPU 监测 ------------------------------------------------------
    def _on_gpu_monitor(self) -> None:
        blk = self._block()
        if blk is None:
            return
        path = blk.start_gpu_monitor()
        if path is None:
            blk.show_toast("GPU 监测已在录制中…", success=True)
        else:
            blk.show_toast(
                f"▶ GPU 监测开始（2 分钟），完成后日志写入：{path}", success=True)
        self._monitor_was_active = bool(
            getattr(blk, "_gpu_monitor_active", False))
        self._monitor_tick()
        if getattr(blk, "_gpu_monitor_active", False):
            self._monitor_timer.start()

    def _monitor_tick(self) -> None:
        blk = self._block()
        active = bool(getattr(blk, "_gpu_monitor_active", False)) if blk else False
        if active:
            if self._gpu_monitor_btn.text() != "监测中…":
                self._gpu_monitor_btn.setText("监测中…")
                self._gpu_monitor_btn.set_style("#7fd1ff", True)
            self._monitor_was_active = True
            if not self._monitor_timer.isActive():
                self._monitor_timer.start()
        else:
            if self._gpu_monitor_btn.text() != "GPU监测":
                self._gpu_monitor_btn.setText("GPU监测")
                self._gpu_monitor_btn.set_style("#7fd1ff", False)
            if self._monitor_was_active:
                self._monitor_was_active = False
                self._monitor_timer.stop()
                if blk is None:
                    return
                path = getattr(blk, "_gpu_monitor_path", "") or ""
                blk.show_toast(
                    f"✓ GPU 监测完成（2 分钟），日志已写入：{path}", success=True)

    def nav_items(self) -> list[NavItem]:
        items: list[NavItem] = []
        if getattr(self, "_gpu_monitor_btn", None) is not None:
            items.append(NavItem(self._gpu_monitor_btn, "action",
                                 confirm=self._on_gpu_monitor, hint="M"))
        if getattr(self, "_card_color_picker", None) is not None:
            items.append(NavItem(self._card_color_picker._custom_btn, "action",
                                 confirm=self._card_color_picker._pick_custom,
                                 hint="C"))
            items.append(NavItem(self._card_color_picker._follow_btn, "action",
                                 confirm=self._card_color_picker._follow))
        items.append(NavItem(self._close, "action",
                             confirm=self._overlay.open_layout, hint="B"))
        return items


# --------------------------------------------------------------------------
# PerfMon edit panel — 卡片颜色（只读监控卡片无需其它设置）
# --------------------------------------------------------------------------
class PerfMonEditPanel(QWidget):
    """性能监控卡片编辑项：仅卡片颜色。

    性能监控是只读实时卡片，没有需要调节的开关；编辑项只提供卡片颜色编辑，
    让你把它和降压控制 / 锁帧控制等卡片在视觉上区分开。
    """

    def __init__(self, app: Any, overlay: Any) -> None:
        super().__init__()
        self.setStyleSheet(
            "QLabel { background: transparent; border: none; }"
            "QPushButton { border: none; }"
            "QPushButton:hover { border: none; }"
            "QPushButton:pressed { border: none; }"
            "QPushButton:focus { border: none; }"
            "QPushButton:checked { border: none; }")
        self._app = app
        self._overlay = overlay
        self._key = "perfmon"

        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(12, 12, 12, 12)

        title = QLabel(_card_mark_name("性能监控") + " · 编辑项")
        title.setStyleSheet(
            f"QLabel{{color: {_block_card_color_hex(self._app, 'perfmon')};"
            f"font-size: {PAGE_TITLE_SIZE}px; font-weight: bold;}}")
        root.addWidget(title)

        note = QLabel("性能监控是只读实时卡片，展示当前窗口 / CPU / GPU 占用与"
                      "降压·锁帧的判断结果；无需额外开关，仅可在此调整卡片颜色。")
        note.setWordWrap(True)
        note.setStyleSheet("color: #aeb9c9; font-size: 11px;")
        root.addWidget(note)

        # --- 卡片颜色 --------------------------------------------------
        cc_lbl = QLabel("卡片颜色")
        cc_lbl.setStyleSheet("color: #7fd1ff; font-weight: bold; font-size: 13px;")
        root.addWidget(cc_lbl)
        self._card_color_picker = CardColorPicker(
            self._app, self._overlay, "perfmon")
        root.addWidget(self._card_color_picker)

        root.addStretch(1)

        close = HintButton("关闭")
        close.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 6px;"
            " padding: 6px 14px 6px 18px; }")
        close.clicked.connect(self._overlay.open_layout)
        self._close = close
        hb = QHBoxLayout()
        hb.addStretch(1)
        hb.addWidget(close)
        root.addLayout(hb)

    # -- 公开 API ------------------------------------------------------
    def edit_target(self, token: str) -> None:
        self._key = token or "perfmon"
        self._card_color_picker.set_block_key(self._key)
        self.refresh()

    def refresh(self) -> None:
        pass

    def nav_items(self) -> list[NavItem]:
        items: list[NavItem] = []
        if getattr(self, "_card_color_picker", None) is not None:
            items.append(NavItem(self._card_color_picker._custom_btn, "action",
                                 confirm=self._card_color_picker._pick_custom,
                                 hint="C"))
            items.append(NavItem(self._card_color_picker._follow_btn, "action",
                                 confirm=self._card_color_picker._follow))
        items.append(NavItem(self._close, "action",
                             confirm=self._overlay.open_layout, hint="B"))
        return items


# --------------------------------------------------------------------------
# Card colour picker (shared by every edit page)
# --------------------------------------------------------------------------
_CC_BTN = (
    "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
    " border: none; border-radius: 6px;"
    " padding: 5px 10px; font-size: 11px; }"
    "QPushButton:hover { background: rgba(255,255,255,0.16); }"
)
_CC_BTN_CHK = (
    "QPushButton:checked { background: rgba(127,209,255,0.28);"
    " color: #bfe8ff; border: none; }"
)


class FlowLayout(QLayout):
    """Wrapping layout (port of Qt's FlowLayout example).

    Lays out items left-to-right and wraps to a new row when the next item
    would exceed the available width — used by :class:`CardColorPicker` so the
    colour swatches never overflow the window width.
    """

    def __init__(self, parent: Optional[QWidget] = None,
                 margin: int = 0, spacing: int = -1) -> None:
        super().__init__(parent)
        if parent is not None:
            self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing if spacing >= 0 else 5)
        self._items: list[QLayoutItem] = []  # type: ignore[type-arg]

    def addItem(self, item: "QLayoutItem") -> None:  # type: ignore[name-defined]
        self._items.append(item)

    def addWidget(self, w: QWidget) -> None:  # convenience
        pw = self.parentWidget()
        if pw is not None:
            w.setParent(pw)
        self.addItem(QWidgetItem(w))

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> "Optional[QLayoutItem]":  # type: ignore[name-defined]
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> "Optional[QLayoutItem]":  # type: ignore[name-defined]
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientation:  # type: ignore[override]
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # type: ignore[override]
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), apply=False)

    def minimumSize(self) -> QSize:  # type: ignore[override]
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(2 * m.left(), 2 * m.top())
        return size

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return self.minimumSize()

    def setGeometry(self, rect: QRect) -> None:  # type: ignore[override]
        super().setGeometry(rect)
        self._do_layout(rect, apply=True)

    def _do_layout(self, rect: QRect, apply: bool) -> int:
        m = self.contentsMargins()
        x = rect.x() + m.left()
        y = rect.y() + m.top()
        line_h = 0
        sp = self.spacing()
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + sp
            if next_x - sp > rect.right() and line_h > 0:
                x = rect.x() + m.left()
                y = y + line_h + sp
                next_x = x + hint.width() + sp
                line_h = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()


class CardColorPicker(QWidget):
    """Preset swatches + 跟随呼吸灯 + 自定义颜色 for one card.

    Selecting a preset writes ``config.card_colors[block_key]`` (a "R,G,B"
    string) and live-applies it: the homepage card title + neon frame and the
    layout-editor row recolour immediately.  Clearing (跟随呼吸灯) removes the
    override so the card falls back to the breathing-light theme accent.
    """

    _PRESETS = _CARD_COLOR_PRESETS

    def __init__(self, app: Any, overlay: Any, block_key: str = "",
                 on_change: Any = None) -> None:
        super().__init__()
        self._app = app
        self._overlay = overlay
        self._key = block_key
        self._on_change = on_change
        self._swatches: list[QFrame] = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        # Swatches wrap onto multiple rows within the window width (no overflow).
        swrap = QWidget()
        row = FlowLayout(swrap, margin=0, spacing=5)
        for label, rgb in self._PRESETS:
            sw = QFrame()
            sw.setFixedSize(30, 26)
            hx = _rgb_to_hex(rgb)
            sw._hex = hx
            sw._label = label
            sw.setStyleSheet(self._sw_style(hx))
            sw.setCursor(Qt.CursorShape.PointingHandCursor)
            sw.setToolTip(label)
            sw.mousePressEvent = lambda _e, hx=hx: self._pick(hx)  # type: ignore[assignment]
            row.addWidget(sw)
            self._swatches.append(sw)
        lay.addWidget(swrap)

        brow = QHBoxLayout()
        self._custom_btn = QPushButton("自定义颜色…")
        self._custom_btn.setStyleSheet(_CC_BTN)
        self._custom_btn.clicked.connect(self._pick_custom)
        self._follow_btn = QPushButton("跟随呼吸灯")
        self._follow_btn.setCheckable(True)
        self._follow_btn.setStyleSheet(_CC_BTN + _CC_BTN_CHK)
        self._follow_btn.clicked.connect(self._follow)
        brow.addWidget(self._custom_btn)
        brow.addWidget(self._follow_btn)
        lay.addLayout(brow)

        hint = QLabel("选择后：卡片标题颜色（主页 / 布局编辑）与主页卡片外框颜色随之改变；"
                      "「跟随呼吸灯」则还原为单一主题色。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8b97aa; font-size: 10px;")
        lay.addWidget(hint)

    # -- public API -----------------------------------------------------
    def set_block_key(self, key: str) -> None:
        self._key = key
        self.refresh()

    def refresh(self) -> None:
        cfg = self._app.config_manager.config if self._app else None
        cur = cfg.card_colors.get(self._key) if (cfg and self._key) else None
        cur_hex = _rgb_to_hex(cur) if cur else ""
        for sw in self._swatches:
            sel = (getattr(sw, "_hex", "") == cur_hex)
            sw.setStyleSheet(self._sw_style(sw._hex, sel))
        self._follow_btn.setChecked(not cur_hex)

    # -- internals ------------------------------------------------------
    @staticmethod
    def _sw_style(hexv: str, selected: bool = False) -> str:
        border = ("3px solid #ffffff" if selected
                  else "1px solid rgba(255,255,255,0.25)")
        return (f"QFrame {{ background-color: #{hexv}; border: {border};"
                f" border-radius: 5px; }}")

    def _pick(self, hexv: str) -> None:
        cfg = self._app.config_manager.config if self._app else None
        if cfg is None or not self._key:
            return
        cfg.card_colors[self._key] = _hex_to_csv(hexv)
        self._persist_and_apply()
        self.refresh()
        if self._on_change is not None:
            self._on_change()

    def _pick_custom(self) -> None:
        dlg = QColorDialog()
        cfg = self._app.config_manager.config if self._app else None
        cur = cfg.card_colors.get(self._key) if (cfg and self._key) else None
        if cur:
            try:
                dlg.setCurrentColor(QColor("#" + _rgb_to_hex(cur)))
            except Exception:
                pass
        if dlg.exec() == QColorDialog.DialogCode.Accepted:
            c = dlg.currentColor()
            self._pick(f"{c.red():02x}{c.green():02x}{c.blue():02x}")

    def _follow(self) -> None:
        cfg = self._app.config_manager.config if self._app else None
        if cfg is None or not self._key:
            return
        cfg.card_colors.pop(self._key, None)
        self._persist_and_apply()
        self.refresh()
        if self._on_change is not None:
            self._on_change()

    def _persist_and_apply(self) -> None:
        try:
            self._app.save_all()
        except Exception:
            lay = getattr(self._overlay, "_layout_panel", None)
            if lay is not None:
                try:
                    lay._render_rows()
                except Exception:
                    pass


class CardColorPreview(QFrame):
    """Small live preview of a card title + neon frame in the chosen colour."""

    def __init__(self) -> None:
        super().__init__()
        self._accent = QColor(127, 209, 255)
        self.setFixedHeight(64)
        self._title = QLabel("► 卡片标题预览")
        self._title.setStyleSheet(
            "QLabel{color: #7fd1ff; font-size: 13px; font-weight: bold; background: transparent;}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.addWidget(self._title)

    def set_accent(self, qc: QColor) -> None:
        self._accent = qc
        self._title.setStyleSheet(
            f"QLabel{{color: #{qc.red():02x}{qc.green():02x}{qc.blue():02x};"
            " font-size: 13px; font-weight: bold; background: transparent;}}")
        self.update()

    def paintEvent(self, ev: Any) -> None:  # noqa: N802 - Qt camelCase
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        if w <= 2 or h <= 2:
            p.end()
            return
        path = QPainterPath()
        path.addRoundedRect(1, 1, w - 2, h - 2, 10, 10)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(20, 24, 33, 205))
        p.drawPath(path)
        a = self._accent
        for i in range(3, 0, -1):
            p.setPen(QColor(a.red(), a.green(), a.blue(), int(46 * i / 3)))
            p.drawPath(path)
        p.setPen(QColor(a.red(), a.green(), a.blue(), 230))
        p.drawPath(path)
        p.end()


class _UVNoWheelSlider(QSlider):
    """降压编辑项滑块：忽略鼠标滚轮事件。

    普通 QSlider 在「悬停滚轮」时会直接改值，与用户「滚轮只滚页面」的预期冲突。
    这里重写 wheelEvent 调用 ``ev.ignore()``，让事件冒泡给外层 QScrollArea（视口
    接收后滚动页面），从而滚轮只滚页面、不动滑块。手柄方向键 nudge / 拖动改值不受
    影响（仍走 valueChanged / nudge）。不重绘聚焦边框——导航层已有自己的聚焦视觉。
    """

    def wheelEvent(self, ev: Any) -> None:  # noqa: N802 - Qt camelCase
        ev.ignore()


class UndervoltEditPanel(QWidget):
    """降压控制卡片编辑项：启用自动降压 + 允许-50mV 重载档 + 卡片颜色。

    enabled / heavy_enabled 是 config/undervolt.json 的运行时开关（不在 block
    实例上），通过主窗口的 undervolt_get_flags / undervolt_set_flag 实时读写
    live config 并持久化——改完不必重启即可生效。
    """

    def __init__(self, app: Any, overlay: Any) -> None:
        super().__init__()
        self.setStyleSheet(
            "QLabel { background: transparent; border: none; }"
            "QPushButton { border: none; }"
            "QPushButton:hover { border: none; }"
            "QPushButton:pressed { border: none; }"
            "QPushButton:focus { border: none; }"
            "QPushButton:checked { border: none; }")
        self._app = app
        self._overlay = overlay
        self._key = "undervolt"

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 滚动区域：编辑项内容较长（标题/说明/3 开关/警告/阈值标题+说明/2 滑块/
        # 幅度标题+说明/1 滑块/幅度警告/卡片颜色），窗口高度不够时下方会被裁切、文字
        # 遮挡。包进 QScrollArea 后内容超出可视高度即出现竖向滚动条、可滚轮/键盘滚动。
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea{border:none; background:transparent;}"
            "QScrollBar:vertical{background:rgba(255,255,255,0.06); width:8px;"
            " border-radius:4px; margin:2px;}"
            "QScrollBar::handle:vertical{background:rgba(255,255,255,0.22);"
            " border-radius:4px;}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical"
            "{height:0px;}")
        inner = QWidget()
        inner.setStyleSheet("QWidget{background:transparent;}")
        # 约束 inner 最大宽度 = 滚动区可视宽度，确保内容不超出编辑 overlay 可用
        # 宽度（窗口宽度），横向不会溢出、不被压扁。
        inner.setMaximumWidth(2000)
        self._scroll = scroll
        self._inner = inner
        lay = QVBoxLayout(inner)
        lay.setSpacing(10)
        lay.setContentsMargins(14, 14, 14, 14)
        self._lay = lay

        title = ClickableTitle(_card_mark_name("降压控制") + " · 编辑项",
                                color=_block_card_color_hex(self._app, "undervolt"),
                                size=PAGE_TITLE_SIZE)
        title.clicked.connect(lambda _=False: self._open_faq())
        lay.addWidget(title)

        desc = QLabel(
            "降压（负载门控 Curve Optimizer）：按游戏负载实时调 ryzenadj 降压。"
            "下三个开关对应 config/undervolt.json 的运行时参数。")
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #aeb9c9; font-size: 11px;")
        lay.addWidget(desc)

        self._enabled_toggle = QPushButton("启用自动降压")
        self._enabled_toggle.setMinimumHeight(38)
        self._enabled_toggle.setCheckable(True)
        self._enabled_toggle.setStyleSheet(_UV_EDIT_TOGGLE_STYLE)
        self._enabled_toggle.clicked.connect(
            lambda _=False: self._on_enabled())
        lay.addWidget(self._enabled_toggle)

        self._heavy_toggle = QPushButton("允许 -50mV 重载档")
        self._heavy_toggle.setMinimumHeight(38)
        self._heavy_toggle.setCheckable(True)
        self._heavy_toggle.setStyleSheet(_UV_EDIT_TOGGLE_STYLE)
        self._heavy_toggle.clicked.connect(
            lambda _=False: self._on_heavy())
        lay.addWidget(self._heavy_toggle)

        # -50 双重门控：heavy_enabled（上一开关）只「放行」重载档，但 -50 真正下发
        # 还需 heavy_validated=True（-50 键盘受控验证通过）。未验证时即便判定为重载
        # 也只给 -20（轻载档），性能监控卡片会显示「-50未验证→-20」。这是防 -50 致
        # 掌机键盘失灵的硬门控——完成 30 分钟 -50 实机验证（键盘不冻、无 WHEA）后再开。
        self._validated_toggle = QPushButton("已完成 -50 键盘受控验证")
        self._validated_toggle.setMinimumHeight(38)
        self._validated_toggle.setCheckable(True)
        self._validated_toggle.setStyleSheet(_UV_EDIT_TOGGLE_STYLE)
        self._validated_toggle.clicked.connect(
            lambda _=False: self._on_validated())
        lay.addWidget(self._validated_toggle)

        warn = QLabel(
            "⚠ 启用后状态机会按负载写 ryzenadj。-50mV 档曾致掌机键盘失灵，"
            "请先以 dry_run 观察、确认键盘稳定后再放开「允许 -50mV」。"
            "「允许 -50mV」开后仍只给 -20，是因为「-50 键盘受控验证」未开——"
            "完成 30 分钟 -50 实机验证（键盘不冻、事件查看器无 WHEA-Logger 欠压报错）"
            "后再开它，-50 才会真正下发。")
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #ffb27a; font-size: 11px;")
        lay.addWidget(warn)

        # --- 降压阈值（重载 / 轻载判定边界）---------------------------
        th_title = QLabel("降压阈值（重载判定边界）")
        th_title.setStyleSheet("color: #62e0a8; font-weight: bold; font-size: 13px;")
        lay.addWidget(th_title)

        th_desc = QLabel(
            "单核最大占用 ≥ 重载阈值(hmax) 或 单核均值 ≥ 轻载阈值(hmean) 时判定为重载，"
            "下发 -50mV（需先放开「允许 -50mV」）；否则走 -20mV 轻载档。数值越高越保守。"
            "拖动即时生效，松手写入配置。")
        th_desc.setWordWrap(True)
        th_desc.setStyleSheet("color: #aeb9c9; font-size: 11px;")
        lay.addWidget(th_desc)

        self._hmax_slider, self._hmax_val = self._make_threshold_row(
            "hmax", "重载阈值 hmax", lay)
        self._hmean_slider, self._hmean_val = self._make_threshold_row(
            "hmean", "轻载阈值 hmean", lay)

        # --- 降压幅度（各档 CO 偏移，mV）-----------------------------
        off_title = QLabel("降压幅度（各档 CO 偏移 mV）")
        off_title.setStyleSheet("color: #62e0a8; font-weight: bold; font-size: 13px;")
        lay.addWidget(off_title)

        off_desc = QLabel(
            "各档位下发的 CO 偏移幅度。滑块值为降压幅度(0–50mV)，实际 CO = -幅度"
            "（负值=降压）。空闲档在桌面触发；轻载/重载档仅在游戏中按负载触发"
            "（重载需先放开「允许 -50mV」并验证）。拖动即时生效，松手写入配置。")
        off_desc.setWordWrap(True)
        off_desc.setStyleSheet("color: #aeb9c9; font-size: 11px;")
        lay.addWidget(off_desc)

        self._idle_slider, self._idle_val = self._make_offset_row(
            "idle", "空闲降压幅度", lay)
        self._light_slider, self._light_val = self._make_offset_row(
            "light", "轻载降压幅度", lay)
        self._heavy_slider, self._heavy_val = self._make_offset_row(
            "heavy", "重载降压幅度", lay)

        off_warn = QLabel(
            "⚠ 空闲(桌面/未开游戏)是 CO 降压最深、键盘最易失灵的场景——你此前排查到"
            "轻载掉压致 EC 经 eSPI 中断、键盘冻住。除非已用 dry_run 观察并确认键盘稳定，"
            "否则请保持 0。想要恒定轻微降压请用卡片「关闭降压」按钮钉固定 -20。")
        off_warn.setWordWrap(True)
        off_warn.setStyleSheet("color: #ffb27a; font-size: 11px;")
        lay.addWidget(off_warn)

        # --- 录制标定（原卡片面按钮，迁至设置项）---------------------------
        rec_title = QLabel("录制标定（负载采样）")
        rec_title.setStyleSheet("color: #62e0a8; font-weight: bold; font-size: 13px;")
        lay.addWidget(rec_title)

        rec_desc = QLabel(
            "实机跑游戏，采样 CPU/GPU 占用分布，生成降压阈值标定报表。"
            "点击开始，再次点击停止并生成 HTML 报表（报表落在 logs/ 下）。")
        rec_desc.setWordWrap(True)
        rec_desc.setStyleSheet("color: #aeb9c9; font-size: 11px;")
        lay.addWidget(rec_desc)

        self._rec_btn = QPushButton("开始录制")
        self._rec_btn.setMinimumHeight(38)
        self._rec_btn.setStyleSheet(_UV_EDIT_TOGGLE_STYLE)
        self._rec_btn.clicked.connect(self._on_record)
        lay.addWidget(self._rec_btn)

        # 录制运行态
        self._rec_proc = None
        self._rec_running = False
        self._rec_stopping = False
        self._rec_csv = None
        self._rec_html = None
        self._rec_stop = None
        self._rec_timer = QTimer(self)
        self._rec_timer.timeout.connect(self._poll_record)
        self._rec_stop_t0 = 0.0

        # --- 卡片颜色 --------------------------------------------------
        cc_lbl = QLabel("卡片颜色")
        cc_lbl.setStyleSheet("color: #7fd1ff; font-weight: bold; font-size: 13px;")
        lay.addWidget(cc_lbl)
        self._card_color_picker = CardColorPicker(
            self._app, self._overlay, "undervolt")
        lay.addWidget(self._card_color_picker)

        lay.addStretch(1)

        scroll.setWidget(inner)
        self._install_toast()
        root.addWidget(scroll, 1)

        close = HintButton("关闭")
        close.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 6px;"
            " padding: 6px 14px 6px 18px; }")
        close.clicked.connect(self._overlay.open_layout)
        self._close = close
        hb = QHBoxLayout()
        hb.addStretch(1)
        hb.addWidget(close)
        root.addLayout(hb)

    def resizeEvent(self, ev: Any) -> None:  # noqa: N802 - Qt camelCase
        # 让 inner 内容宽度始终不超过滚动区可视宽度（= 窗口宽度），避免编辑项
        # 在窄窗口下横向溢出被裁切。
        super().resizeEvent(ev)
        if getattr(self, "_scroll", None) is not None:
            vw = self._scroll.viewport().width()
            if vw > 0:
                self._inner.setMaximumWidth(vw)

    # -- 疑惑解答（点击顶部「降压控制 · 编辑项」标题打开）------------
    def _open_faq(self) -> None:
        faq = self._overlay.faq()
        if faq is not None:
            faq.open_for("undervolt", "鼠标 标题")

    # -- 与主窗口（live config 真相源）交互 ---------------------------
    def _flags(self) -> dict:
        win = self._overlay.window()
        if win is not None and hasattr(win, "undervolt_get_flags"):
            try:
                return win.undervolt_get_flags()
            except Exception:
                pass
        return {"enabled": False, "heavy_enabled": False,
                "heavy_validated": False}

    def _set_flag(self, name: str, value: bool) -> None:
        win = self._overlay.window()
        if win is not None and hasattr(win, "undervolt_set_flag"):
            try:
                win.undervolt_set_flag(name, value)
            except Exception:
                pass

    # -- 降压阈值（hmax / hmean）读写 ---------------------------------
    def _thresholds(self) -> dict:
        win = self._overlay.window()
        if win is not None and hasattr(win, "undervolt_get_thresholds"):
            try:
                return win.undervolt_get_thresholds()
            except Exception:
                pass
        return {"hmax": 75.0, "hmean": 35.0}

    def _set_threshold(self, name: str, value: float, save: bool = True) -> None:
        win = self._overlay.window()
        if win is not None and hasattr(win, "undervolt_set_threshold"):
            try:
                win.undervolt_set_threshold(name, value, save=save)
            except Exception:
                pass

    # -- 降压幅度（各档 CO 偏移 mV）读写 -------------------------------
    def _offsets(self) -> dict:
        win = self._overlay.window()
        if win is not None and hasattr(win, "undervolt_get_offsets"):
            try:
                return win.undervolt_get_offsets()
            except Exception:
                pass
        return {"idle": 0, "light": -20, "heavy": -50}

    def _set_offset(self, name: str, value: int, save: bool = True) -> None:
        win = self._overlay.window()
        if win is not None and hasattr(win, "undervolt_set_offset"):
            try:
                win.undervolt_set_offset(name, value, save=save)
            except Exception:
                pass

    def _make_threshold_row(self, key: str, label_text: str,
                            root: "QVBoxLayout") -> "tuple[QSlider, QLabel]":
        """创建一行「标签 + 滑块 + 数值」，拖动即时改、松手落盘。"""
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel(label_text)
        lbl.setStyleSheet("color: #cdd6e2; font-size: 11px;")
        lbl.setFixedWidth(104)
        slider = _UVNoWheelSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        # 响应式：滑块占满剩余宽度（窗口宽时舒展、窄时不溢出），
        # 设 60px 下限避免被压到无法拖动。
        slider.setMinimumWidth(60)
        slider.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Fixed)
        slider.setStyleSheet(
            "QSlider { border: none; background: transparent; }"
            "QSlider::groove:horizontal { border: none; height: 6px;"
            " background: rgba(255,255,255,0.15); border-radius: 3px; }"
            "QSlider::handle:horizontal { border: none; background: #62e0a8;"
            " width: 14px; height: 14px; margin: -4px 0; border-radius: 7px; }"
            "QSlider::sub-page:horizontal { background: #62e0a8; border-radius: 3px; }")
        val = QLabel("75%")
        val.setStyleSheet("color: #62e0a8; font-size: 11px;")
        val.setFixedWidth(40)
        val.setAlignment(Qt.AlignmentFlag.AlignRight)
        row.addWidget(lbl)
        row.addWidget(slider)
        row.addWidget(val)
        root.addLayout(row)
        slider.valueChanged.connect(
            lambda v, k=key: self._on_threshold(k, v, save=False))
        slider.sliderReleased.connect(
            lambda k=key: self._on_threshold(k, slider.value(), save=True))
        return slider, val

    def _on_threshold(self, key: str, v: int, save: bool) -> None:
        lab = self._hmax_val if key == "hmax" else self._hmean_val
        lab.setText(f"{v}%")
        self._set_threshold(key, float(v), save=save)

    def _make_offset_row(self, key: str, label_text: str,
                         root: "QVBoxLayout") -> "tuple[QSlider, QLabel]":
        """创建一行「标签 + 滑块(0..50 降压幅度) + 数值」，拖动即时改、松手落盘。

        滑块值为降压幅度(0..50mV)，实际下发的 CO = -幅度（负值=降压）。
        """
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel(label_text)
        lbl.setStyleSheet("color: #cdd6e2; font-size: 11px;")
        lbl.setFixedWidth(104)
        slider = _UVNoWheelSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 50)
        # 响应式：滑块占满剩余宽度，设 60px 下限避免被压到无法拖动。
        slider.setMinimumWidth(60)
        slider.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Fixed)
        slider.setStyleSheet(_UV_EDIT_SLIDER_STYLE)
        val = QLabel("0 mV")
        val.setStyleSheet("color: #62e0a8; font-size: 11px;")
        # 足以放下「0 mV (不降压)」（mag=0 时的长文案），不随窗口伸缩。
        val.setFixedWidth(92)
        val.setAlignment(Qt.AlignmentFlag.AlignRight)
        row.addWidget(lbl)
        row.addWidget(slider)
        row.addWidget(val)
        root.addLayout(row)
        slider.valueChanged.connect(
            lambda v, k=key: self._on_offset(k, v, save=False))
        slider.sliderReleased.connect(
            lambda k=key: self._on_offset(k, slider.value(), save=True))
        return slider, val

    def _on_offset(self, key: str, mag: int, save: bool) -> None:
        """mag = 降压幅度(0..50)，实际 CO = -mag（负值=降压）。"""
        mag = max(0, min(50, int(mag)))
        val_lbl = getattr(self, f"_{key}_val", None) or self._idle_val
        val_lbl.setText(
            "0 mV (不降压)" if mag == 0 else f"-{mag} mV")
        self._set_offset(key, -mag, save=save)

    # -- 录制标定（从降压控制卡片面迁来）------------------------------
    def _win(self):
        w = getattr(self, "_overlay", None)
        if w is not None:
            w = w.window()
        if w is not None and hasattr(w, "undervolt_status"):
            return w
        return None

    def _install_toast(self) -> None:
        """设置项内独立的右上角 toast（与卡片 toast 互不干扰）。"""
        self._toast_label = _Label()
        self._toast_panel = _Toast(self._toast_label, self._overlay)
        self._uv_ui = {
            "toast_label": self._toast_label,
            "toast_panel": self._toast_panel,
        }

    def _toast(self, text: str, success: bool = True) -> None:
        _show_block_toast(self._uv_ui, text, success=success)

    def _set_status(self, text: str, color: str = "#9aa6b8") -> None:
        success = not (color.startswith("#ff") or color.startswith("#e6"))
        self._toast(text, success=success)

    def _on_record(self) -> None:
        # 状态开关：点击在「录制中 / 已停止」之间切换，起止完全由用户手动控制。
        if self._rec_stopping:
            if self._rec_proc is None or self._rec_proc.poll() is not None:
                self._finish_record()
            return
        if self._rec_running:
            self._stop_record()
        else:
            self._start_record()

    def _start_record(self) -> None:
        win = self._win()
        if win is None:
            return
        app_root = Path(__file__).resolve().parents[3]
        tool_script = app_root / "test-launch" / "py-script" / "measure_game_load.py"
        if not tool_script.exists():
            self._set_status(f"找不到录制脚本: {tool_script}", "#ff6b6b")
            return
        logs_dir = app_root / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_csv = logs_dir / f"measure_{ts}.csv"
        stop_file = logs_dir / f"measure_{ts}.stop"
        self._rec_csv = out_csv
        self._rec_html = out_csv.with_suffix(".html")
        self._rec_stop = stop_file
        try:
            cf = (logs_dir / f"measure_{ts}.console.log").open("w", encoding="utf-8")
            cmd = [sys.executable, str(tool_script), "--auto",
                   "--stop-file", str(stop_file),
                   "--duration", "86400",
                   "--out", str(out_csv)]
            proc = subprocess.Popen(
                cmd, stdout=cf, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except OSError as e:
            self._set_status(f"启动失败: {e}", "#ff6b6b")
            return
        self._rec_proc = proc
        self._rec_running = True
        self._rec_stopping = False
        self._set_rec_btn(True)
        self._set_status("录制中…（再次点击按钮停止并生成报表）", "#4be38a")
        self._rec_timer.start(200)

    def _stop_record(self) -> None:
        if not self._rec_running:
            return
        self._rec_running = False
        self._rec_stopping = True
        self._rec_stop_t0 = time.monotonic()
        stop_path = self._rec_stop
        wrote = False
        if stop_path is not None:
            try:
                stop_path.write_text("stop", encoding="utf-8")
                wrote = True
            except OSError:
                wrote = False
        if not wrote:
            proc = self._rec_proc
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
        self._set_rec_btn(False)
        self._set_status("正在停止录制…", "#9aa6b8")
        if self._rec_proc is not None and self._rec_proc.poll() is not None:
            self._finish_record()

    def _poll_record(self) -> None:
        """每 200ms 检查一次录制进程是否退出。"""
        if self._rec_proc is None:
            self._finish_record()
            return
        if self._rec_proc.poll() is None:
            if self._rec_stopping and time.monotonic() - self._rec_stop_t0 > 15.0:
                try:
                    self._rec_proc.kill()
                except OSError:
                    pass
            return
        self._finish_record()

    def _finish_record(self) -> None:
        """进程退出/被强制停止后：生成报表、清理、复位 UI。"""
        if self._rec_timer is not None:
            self._rec_timer.stop()
        self._rec_running = False
        self._rec_stopping = False
        self._rec_proc = None
        csv_path = self._rec_csv
        stop_path = self._rec_stop
        produced = None
        if csv_path is not None and csv_path.exists():
            try:
                script_dir = str(Path(__file__).resolve().parents[3]
                                 / "test-launch" / "py-script")
                if script_dir not in sys.path:
                    sys.path.insert(0, script_dir)
                from gen_uv_report import generate_report
                produced = generate_report(csv_path)
            except Exception as e:
                produced = None
                print(f"[undervolt] 报表生成失败: {e}", file=sys.stderr)
        try:
            if stop_path is not None and stop_path.exists():
                stop_path.unlink()
        except OSError:
            pass
        self._set_rec_btn(False)
        if produced:
            self._set_status(f"✓ 已停止 → 报表: {produced}", "#4be38a")
        elif csv_path is not None and csv_path.exists() \
                and csv_path.stat().st_size > 0:
            self._set_status("已停止，但报表生成失败（详见 console.log）",
                             "#ffd166")
        else:
            self._set_status("已停止（无有效数据）", "#9aa6b8")

    def _set_rec_btn(self, recording: bool) -> None:
        """更新录制按钮文本（plain QPushButton，无 set_style）。"""
        self._rec_btn.setText("停止录制" if recording else "开始录制")

    # -- 公开 API ------------------------------------------------------
    def edit_target(self, token: str) -> None:
        self._key = token or "undervolt"
        self._card_color_picker.set_block_key(self._key)
        self.refresh()
        # 进入编辑项后等布局落定再同步一次 inner 最大宽度：避免首次 resize
        # 时 viewport 宽度尚未就绪（vw=0 被跳过），inner 仍停在初始 2000 导致
        # 窄窗口下内容横向溢出。
        QTimer.singleShot(0, self._sync_inner_width)

    def _sync_inner_width(self) -> None:
        if getattr(self, "_scroll", None) is None:
            return
        vw = self._scroll.viewport().width()
        if vw > 0:
            self._inner.setMaximumWidth(vw)

    def refresh(self) -> None:
        flags = self._flags()
        en = bool(flags.get("enabled", False))
        hv = bool(flags.get("heavy_enabled", False))
        vd = bool(flags.get("heavy_validated", False))
        self._enabled_toggle.blockSignals(True)
        self._enabled_toggle.setChecked(en)
        self._enabled_toggle.setText("启用自动降压：开" if en else "启用自动降压：关")
        self._enabled_toggle.blockSignals(False)
        self._heavy_toggle.blockSignals(True)
        self._heavy_toggle.setChecked(hv)
        self._heavy_toggle.setText(
            "允许 -50mV 重载档：开" if hv else "允许 -50mV 重载档：关")
        self._heavy_toggle.blockSignals(False)
        self._validated_toggle.blockSignals(True)
        self._validated_toggle.setChecked(vd)
        self._validated_toggle.setText(
            "已完成 -50 验证：开" if vd else "已完成 -50 验证：关")
        self._validated_toggle.blockSignals(False)

        # 阈值滑块（拖动 / 手柄 nudge 即时生效，松手落盘）
        th = self._thresholds()
        hmax = int(round(th.get("hmax", 75.0)))
        hmean = int(round(th.get("hmean", 35.0)))
        self._hmax_slider.blockSignals(True)
        self._hmax_slider.setValue(hmax)
        self._hmax_slider.blockSignals(False)
        self._hmax_val.setText(f"{hmax}%")
        self._hmean_slider.blockSignals(True)
        self._hmean_slider.setValue(hmean)
        self._hmean_slider.blockSignals(False)
        self._hmean_val.setText(f"{hmean}%")

        # 降压幅度滑块（幅度 0..50 → 实际 CO = -幅度），空闲/轻载/重载三档
        offs = self._offsets()
        for key in ("idle", "light", "heavy"):
            slider = getattr(self, f"_{key}_slider", None)
            val_lbl = getattr(self, f"_{key}_val", None)
            if slider is None or val_lbl is None:
                continue
            mv = int(offs.get(key, 0))
            mag = max(0, min(50, abs(mv)))
            slider.blockSignals(True)
            slider.setValue(mag)
            slider.blockSignals(False)
            val_lbl.setText(
                "0 mV (不降压)" if mag == 0 else f"-{mag} mV")

    def _on_enabled(self) -> None:
        en = self._enabled_toggle.isChecked()
        self._enabled_toggle.setText(
            "启用自动降压：开" if en else "启用自动降压：关")
        self._set_flag("enabled", en)

    def _on_heavy(self) -> None:
        hv = self._heavy_toggle.isChecked()
        self._heavy_toggle.setText(
            "允许 -50mV 重载档：开" if hv else "允许 -50mV 重载档：关")
        self._set_flag("heavy_enabled", hv)

    def _on_validated(self) -> None:
        vd = self._validated_toggle.isChecked()
        self._validated_toggle.setText(
            "已完成 -50 验证：开" if vd else "已完成 -50 验证：关")
        self._set_flag("heavy_validated", vd)

    def nav_items(self) -> list[NavItem]:
        items = [
            NavItem(self._enabled_toggle, "toggle",
                    confirm=self._on_enabled, hint="A"),
            NavItem(self._heavy_toggle, "toggle",
                    confirm=self._on_heavy),
            NavItem(self._validated_toggle, "toggle",
                    confirm=self._on_validated),
        ]
        if getattr(self, "_card_color_picker", None) is not None:
            items.append(NavItem(self._card_color_picker._custom_btn, "action",
                                 confirm=self._card_color_picker._pick_custom,
                                 hint="C"))
            items.append(NavItem(self._card_color_picker._follow_btn, "action",
                                 confirm=self._card_color_picker._follow))

        # 阈值滑块（手柄：A×2 武装 → 方向键 nudge；nudge 在边界返回 False 回退焦点）
        def _nudge_hmax(d: int) -> bool:
            old = self._hmax_slider.value()
            nv = max(0, min(100, old + d))
            if nv == old:
                return False
            self._hmax_slider.setValue(nv)
            self._on_threshold("hmax", nv, save=True)
            return True

        def _nudge_hmean(d: int) -> bool:
            old = self._hmean_slider.value()
            nv = max(0, min(100, old + d))
            if nv == old:
                return False
            self._hmean_slider.setValue(nv)
            self._on_threshold("hmean", nv, save=True)
            return True

        items.append(NavItem(
            self._hmax_slider, "slider",
            confirm=lambda: self._on_threshold(
                "hmax", self._hmax_slider.value(), save=True),
            nudge=_nudge_hmax, hint="H"))
        items.append(NavItem(
            self._hmean_slider, "slider",
            confirm=lambda: self._on_threshold(
                "hmean", self._hmean_slider.value(), save=True),
            nudge=_nudge_hmean, hint="M"))

        # 降压幅度滑块（手柄：A×2 武装 → 方向键 nudge；幅度 0..50），空闲/轻载/重载三档
        def _nudge_offset(slider, key, d: int) -> bool:
            old = slider.value()
            nv = max(0, min(50, old + d))
            if nv == old:
                return False
            slider.setValue(nv)
            self._on_offset(key, nv, save=True)
            return True

        items.append(NavItem(
            self._idle_slider, "slider",
            confirm=lambda: self._on_offset(
                "idle", self._idle_slider.value(), save=True),
            nudge=lambda d, s=self._idle_slider: _nudge_offset(s, "idle", d),
            hint="I"))
        items.append(NavItem(
            self._light_slider, "slider",
            confirm=lambda: self._on_offset(
                "light", self._light_slider.value(), save=True),
            nudge=lambda d, s=self._light_slider: _nudge_offset(s, "light", d),
            hint="L"))
        items.append(NavItem(
            self._heavy_slider, "slider",
            confirm=lambda: self._on_offset(
                "heavy", self._heavy_slider.value(), save=True),
            nudge=lambda d, s=self._heavy_slider: _nudge_offset(s, "heavy", d),
            hint="G"))
        items.append(NavItem(self._close, "action",
                             confirm=self._overlay.open_layout, hint="B"))
        return items


class SmtEditPanel(QWidget):
    """SMT 热核隔离卡片编辑项：启用检测 + 热核判定阈值 + 调试记录。

    阈值 / 启用 都是 AppConfig.smt_config 的运行时参数，改完即落盘
    （app.save_all），并在卡片主页实时反映。调试记录按钮生成 HTML 报表。
    """

    _SLIDER_STYLE = (
        "QSlider { border: none; background: transparent; }"
        "QSlider::groove:horizontal { border: none; height: 6px;"
        " background: rgba(255,255,255,0.15); border-radius: 3px; }"
        "QSlider::handle:horizontal { border: none; background: #ff9f43;"
        " width: 14px; height: 14px; margin: -4px 0; border-radius: 7px; }"
        "QSlider::sub-page:horizontal { background: #ff9f43; border-radius: 3px; }")

    def __init__(self, app: Any, overlay: Any) -> None:
        super().__init__()
        self.setStyleSheet(
            "QLabel { background: transparent; border: none; }"
            "QPushButton { border: none; }"
            "QPushButton:hover { border: none; }"
            "QPushButton:pressed { border: none; }"
            "QPushButton:focus { border: none; }"
            "QPushButton:checked { border: none; }")
        self._app = app
        self._overlay = overlay
        self._key = "smt"

        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(12, 12, 12, 12)

        title = ClickableTitle(_card_mark_name("SMT 热核隔离") + " · 编辑项",
                                color=_block_card_color_hex(self._app, "smt"),
                                size=PAGE_TITLE_SIZE)
        title.clicked.connect(lambda _=False: self._open_faq())
        root.addWidget(title)

        desc = QLabel(
            "SMT 热核隔离：前台游戏主线程独占最热物理核（仅排除其 SMT 兄弟），"
            "其余进程整核让出，把热核彻底留给游戏单线程。")
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #aeb9c9; font-size: 11px;")
        root.addWidget(desc)

        # --- 启用热核检测（开关）-------------------------------------
        self._enabled_toggle = QPushButton("启用热核检测")
        self._enabled_toggle.setMinimumHeight(38)
        self._enabled_toggle.setCheckable(True)
        self._enabled_toggle.setStyleSheet(_UV_EDIT_TOGGLE_STYLE)
        self._enabled_toggle.clicked.connect(lambda _=False: self._on_enabled())
        root.addWidget(self._enabled_toggle)

        warn = QLabel(
            "⚠ 启用后后台每 3s 检测一次；瓶颈出现会对全部进程施加 CPU 集合。"
            "系统关键进程（csrss/lsass/dwm/svchost 等）自动跳过，不会导致桌面崩溃。")
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #ffb27a; font-size: 11px;")
        root.addWidget(warn)

        # --- 热核判定阈值 --------------------------------------------
        th_title = QLabel("热核判定阈值")
        th_title.setStyleSheet("color: #62e0a8; font-weight: bold; font-size: 13px;")
        root.addWidget(th_title)

        th_desc = QLabel(
            "逻辑核占用 ≥ 饱和线 且 超出同簇其余核均值 ≥ 离群下限 时，判定为单线程"
            "瓶颈并施加隔离。饱和线越高越保守。拖动即时生效，松手落盘。")
        th_desc.setWordWrap(True)
        th_desc.setStyleSheet("color: #aeb9c9; font-size: 11px;")
        root.addWidget(th_desc)

        self._sat_slider, self._sat_val = self._make_threshold_row(
            "saturated_thr", "饱和线 saturated_thr", root, 50, 100)
        self._stand_slider, self._stand_val = self._make_threshold_row(
            "min_standout", "离群下限 min_standout", root, 0, 80)

        # --- 调试 / 记录 ---------------------------------------------
        dbg_title = QLabel("调试 / 记录")
        dbg_title.setStyleSheet("color: #7fd1ff; font-weight: bold; font-size: 13px;")
        root.addWidget(dbg_title)
        self._debug_btn = HintButton("调试记录")
        self._debug_btn.set_style("#ff9f43", False)
        self._debug_btn.clicked.connect(lambda _=False: self._on_debug())
        root.addWidget(self._debug_btn)
        self._dbg_status = QLabel("")
        self._dbg_status.setWordWrap(True)
        self._dbg_status.setStyleSheet("color: #9aa6b8; font-size: 11px;")
        root.addWidget(self._dbg_status)

        # --- 卡片颜色 -------------------------------------------------
        cc_lbl = QLabel("卡片颜色")
        cc_lbl.setStyleSheet("color: #7fd1ff; font-weight: bold; font-size: 13px;")
        root.addWidget(cc_lbl)
        self._card_color_picker = CardColorPicker(self._app, self._overlay, "smt")
        root.addWidget(self._card_color_picker)

        root.addStretch(1)

        close = HintButton("关闭")
        close.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 6px;"
            " padding: 6px 14px 6px 18px; }")
        close.clicked.connect(self._overlay.open_layout)
        self._close = close
        hb = QHBoxLayout()
        hb.addStretch(1)
        hb.addWidget(close)
        root.addLayout(hb)

    # -- 疑惑解答 ------------------------------------------------------
    def _open_faq(self) -> None:
        faq = getattr(self._overlay, "faq", None)
        if callable(faq):
            faq = faq()
        if faq is not None:
            try:
                faq.open_for("smt", "鼠标 标题")
            except Exception:
                pass

    # -- 区块访问 ------------------------------------------------------
    def _block(self) -> Any:
        return self._app.blocks.get("smt") if self._app else None

    def _persist(self) -> None:
        try:
            self._app.save_all()
        except Exception:
            pass

    # -- 启用开关 ------------------------------------------------------
    def _on_enabled(self) -> None:
        en = self._enabled_toggle.isChecked()
        self._enabled_toggle.setText(
            "启用热核检测：开" if en else "启用热核检测：关")
        blk = self._block()
        if blk is not None:
            blk.set_auto(en)
        self._persist()

    # -- 阈值（拖动即时生效，松手落盘）--------------------------------
    def _make_threshold_row(self, key: str, label_text: str,
                            root: "QVBoxLayout",
                            min_val: int = 0, max_val: int = 100
                            ) -> "tuple[QSlider, QLabel]":
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel(label_text)
        lbl.setStyleSheet("color: #cdd6e2; font-size: 11px;")
        lbl.setFixedWidth(150)
        slider = _NoWheelSlider(Qt.Orientation.Horizontal)
        slider.setRange(min_val, max_val)
        # 响应式：窄窗口收缩到 60px 下限，宽窗口占满（与降压编辑项一致）。
        slider.setMinimumWidth(60)
        slider.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Fixed)
        slider.setStyleSheet(self._SLIDER_STYLE)
        val = QLabel(str(min_val))
        val.setStyleSheet("color: #ff9f43; font-size: 11px;")
        val.setFixedWidth(44)
        val.setAlignment(Qt.AlignmentFlag.AlignRight)
        row.addWidget(lbl)
        row.addWidget(slider)
        row.addWidget(val)
        root.addLayout(row)
        slider.valueChanged.connect(
            lambda v, k=key: self._on_threshold(k, v, save=False))
        slider.sliderReleased.connect(
            lambda k=key: self._on_threshold(k, slider.value(), save=True))
        return slider, val

    def _on_threshold(self, key: str, v: int, save: bool) -> None:
        lab = self._sat_val if key == "saturated_thr" else self._stand_val
        lab.setText(f"{v}")
        blk = self._block()
        if blk is not None:
            blk.set_threshold(key, float(v), save=save)
        if save:
            self._persist()

    def _nudge(self, key: str, slider: QSlider, d: int) -> bool:
        old = slider.value()
        nv = max(slider.minimum(), min(slider.maximum(), old + d))
        if nv == old:
            return False
        slider.setValue(nv)
        self._on_threshold(key, nv, save=True)
        return True

    # -- 调试记录 ------------------------------------------------------
    def _set_dbg(self, text: str, color: str) -> None:
        self._dbg_status.setText(text)
        self._dbg_status.setStyleSheet(f"color: {color}; font-size: 11px;")

    def _on_debug(self) -> None:
        blk = self._block()
        if blk is None:
            self._set_dbg("SMT 区块未就绪", "#ff6b6b")
            return
        self._set_dbg("生成调试报表中…", "#ffd166")
        try:
            path = blk.debug_simulate()
        except Exception as e:  # noqa: BLE001
            self._set_dbg(f"调试生成失败: {e}", "#ff6b6b")
            return
        self._set_dbg(f"已生成 HTML 报表：{path}", "#4be38a")
        try:
            from PyQt6.QtGui import QDesktopServices
            from PyQt6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        except Exception:  # noqa: BLE001
            pass

    # -- 公开 API ------------------------------------------------------
    def edit_target(self, token: str) -> None:
        self._key = token or "smt"
        self._card_color_picker.set_block_key(self._key)
        self.refresh()

    def refresh(self) -> None:
        blk = self._block()
        sc = getattr(blk.config, "smt_config", None) if blk else None
        en = bool(sc.enabled) if sc else False
        self._enabled_toggle.blockSignals(True)
        self._enabled_toggle.setChecked(en)
        self._enabled_toggle.setText(
            "启用热核检测：开" if en else "启用热核检测：关")
        self._enabled_toggle.blockSignals(False)

        sat = int(round(sc.saturated_thr)) if sc else 80
        stand = int(round(sc.min_standout)) if sc else 30
        self._sat_slider.blockSignals(True)
        self._sat_slider.setValue(sat)
        self._sat_slider.blockSignals(False)
        self._sat_val.setText(f"{sat}")
        self._stand_slider.blockSignals(True)
        self._stand_slider.setValue(stand)
        self._stand_slider.blockSignals(False)
        self._stand_val.setText(f"{stand}")

    def nav_items(self) -> list[NavItem]:
        items = [
            NavItem(self._enabled_toggle, "toggle",
                    confirm=self._on_enabled, hint="A"),
            NavItem(self._sat_slider, "slider",
                    confirm=lambda: self._on_threshold(
                        "saturated_thr", self._sat_slider.value(), save=True),
                    nudge=lambda d: self._nudge("saturated_thr", self._sat_slider, d),
                    hint="H"),
            NavItem(self._stand_slider, "slider",
                    confirm=lambda: self._on_threshold(
                        "min_standout", self._stand_slider.value(), save=True),
                    nudge=lambda d: self._nudge("min_standout", self._stand_slider, d),
                    hint="M"),
            NavItem(self._debug_btn, "action", confirm=self._on_debug, hint="D"),
        ]
        if getattr(self, "_card_color_picker", None) is not None:
            items.append(NavItem(self._card_color_picker._custom_btn, "action",
                                 confirm=self._card_color_picker._pick_custom,
                                 hint="C"))
            items.append(NavItem(self._card_color_picker._follow_btn, "action",
                                 confirm=self._card_color_picker._follow))
        items.append(NavItem(self._close, "action",
                             confirm=self._overlay.open_layout, hint="B"))
        return items


class _NoWheelSlider(QSlider):
    """编辑项滑块：忽略鼠标滚轮事件。

    普通 QSlider 在「悬停滚轮」时会直接改值，与用户「滚轮只滚页面 / 只能拖拽」的预期冲突。
    这里重写 wheelEvent 调用 ``ev.ignore()``，让事件冒泡给外层 QScrollArea（视口接收后滚动
    页面），从而滚轮只滚页面、不动滑块。拖动 / 手柄方向键 nudge 改值不受影响（仍走
    valueChanged / nudge）。
    """

    def wheelEvent(self, ev: Any) -> None:  # noqa: N802 - Qt camelCase
        ev.ignore()


class _NoWheelSpinBox(QSpinBox):
    """编辑项拐点数值框：同时修两个常见体验 bug：

    1. 滚轮误改值 — ``QSpinBox`` 默认把滚轮当成 ±1 改值；此处重写 ``wheelEvent``
       调用 ``ev.ignore()``，让事件冒泡到外层 QScrollArea 滚动页面，光标在框上
       滚动也不会改动数值。
    2. 数字重叠 + 中线穿过 — 根因是 ``QSpinBox`` 在半透明背景 + 分数 DPI 下，
       内部 QLineEdit 文本与 QSpinBox wrapper 文本会在重绘期间同时被绘制
       （WA_OpaquePaintEvent 未开，旧 frame 内容与新 frame 同时可见），表现为
       「数字重叠 / 中间一条线」；同时焦点态切换 ``border:1px`` 会让文本绘制
       框产生 1px 偏移，叠加后即出现「中线穿过数字」。修复策略：

       * ``WA_OpaquePaintEvent=True`` 强制不透明重绘，杜绝旧 frame 残留；
       * 背景从 ``rgba(255,255,255,0.08)`` 换成不透明 ``#2a3447``，去掉半透明
         缓存层的存在；
       * 焦点态只切背景色 ``#3a4357``、**不改 border**，避免焦点切换时让文本
         渲染位置再偏移 1px；
       * 选区背景 / 文字对比加强（``selection-background-color:#62e0a8``），
         方便「选中整段」状态下数字仍清晰可读。

    改值方式保持不变：滚轮被忽略后，改值仅依赖直接键入 / 上下方向键 ±1 /
    Home/End 到极值，三种方式在掌机和 PC 屏都顺手。
    """

    def __init__(self, lo: int, hi: int, parent=None):  # noqa: D401 - Qt API
        super().__init__(parent)
        self.setRange(lo, hi)
        # 关键修复 #1：去掉自带 ↑/↓ 箭头按钮，防止掌机分数 DPI 下数字被箭头遮挡
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 关键修复 #2：强制不透明重绘，杜绝 QSpinBox + 半透明背景的「数字重叠 /
        # 中线穿过」类 bug（Qt 在 translucent widget 上偶尔会让旧 frame 与新 frame
        # 同时可见，WA_OpaquePaintEvent=True 让每次 paintEvent 完整重画底层）
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        # 关键修复 #3：鼠标滚轮不响应，光标在哪段页面就只滚哪段
        self.setMinimumHeight(24)
        self.setMinimumWidth(72)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            "QSpinBox{background:#2a3447; color:#dfe6f0;"
            " border-radius:4px; padding:2px 6px; font-size:13px;"
            " selection-background-color:#62e0a8;"
            " selection-color:#102018;}"
            "QSpinBox:focus{background:#3a4357;}"
            "QSpinBox::up-button, QSpinBox::down-button{width:0; height:0;"
            " border:none;}"
            "QSpinBox:focus::up-button, QSpinBox:focus::down-button{"
            " width:0; height:0; border:none;}")

    def wheelEvent(self, ev: Any) -> None:  # noqa: N802 - Qt camelCase
        # 让滚轮冒泡到外层 QScrollArea，光标在框上只滚页面、不改拐点数值
        ev.ignore()


class FanEditPanel(QWidget):
    """风扇控制卡片编辑项：曲线拐点 / 手动·静音转速 / 滞回 / 设备自动检测 / 驱动测试。

    所有参数落在 AppConfig.fan_config 上，通过 FanBlock 的 setter 就地改、松手即
    落盘（app.save_all()）。manual / silent 滑块松手即切到对应模式并写入 EC，做到
    「在编辑项里直接控制风扇」。设备识别 + 寄存器映射 + 实时 RPM 读数一并展示。
    内容整体包在 QScrollArea 里，避免曲线拐点行被压扁看不见。
    """

    _SLIDER_STYLE = (
        "QSlider { border: none; background: transparent; }"
        "QSlider::groove:horizontal { border: none; height: 6px;"
        " background: rgba(255,255,255,0.15); border-radius: 3px; }"
        "QSlider::handle:horizontal { border: none; background: #62e0a8;"
        " width: 14px; height: 14px; margin: -4px 0; border-radius: 7px; }"
        "QSlider::sub-page:horizontal { background: #62e0a8; border-radius: 3px; }")

    def __init__(self, app: Any, overlay: Any) -> None:
        super().__init__()
        self.setStyleSheet(
            "QLabel { background: transparent; border: none; }"
            "QPushButton { border: none; }"
            "QPushButton:hover { border: none; }"
            "QPushButton:pressed { border: none; }"
            "QPushButton:focus { border: none; }"
            "QPushButton:checked { border: none; }")
        self._app = app
        self._overlay = overlay
        self._key = "fan"

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 滚动区域：编辑项内容较多，用滚动避免曲线行被压缩看不见
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea{border:none; background:transparent;}"
            "QScrollBar:vertical{background:rgba(255,255,255,0.06); width:8px;"
            " border-radius:4px; margin:2px;}"
            "QScrollBar::handle:vertical{background:rgba(255,255,255,0.22);"
            " border-radius:4px;}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical"
            "{height:0px;}")
        inner = QWidget()
        inner.setStyleSheet("QWidget{background:transparent;}")
        # 约束 inner 最大宽度 = 滚动区可视宽度，确保内容不超出编辑 overlay
        # 可用宽度（窗口宽度），横向不会溢出。
        inner.setMaximumWidth(2000)
        self._scroll = scroll
        self._inner = inner
        lay = QVBoxLayout(inner)
        lay.setSpacing(10)
        lay.setContentsMargins(14, 14, 14, 14)
        self._lay = lay

        title = ClickableTitle(_card_mark_name("风扇控制") + " · 编辑项",
                                color=_block_card_color_hex(self._app, "fan"),
                                size=PAGE_TITLE_SIZE)
        title.clicked.connect(lambda _=False: self._open_faq())
        lay.addWidget(title)

        desc = QLabel(
            "风扇控制（MotionAssistant 移植）：按机型路由 EC 寄存器，曲线模式 "
            "1Hz 调度 + 滞回。手动 / 静音转速与曲线拐点在此调节，改动即时生效；"
            "拖动手动 / 静音滑块会直接切到对应模式并控制风扇。")
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #aeb9c9; font-size: 11px;")
        lay.addWidget(desc)

        # --- 设备识别 --------------------------------------------------
        dev_title = QLabel("设备识别")
        dev_title.setStyleSheet("color: #7fd1ff; font-weight: bold; font-size: 13px;")
        lay.addWidget(dev_title)

        self._autodetect_toggle = QPushButton("自动检测设备")
        self._autodetect_toggle.setMinimumHeight(36)
        self._autodetect_toggle.setCheckable(True)
        self._autodetect_toggle.setStyleSheet(_UV_EDIT_TOGGLE_STYLE)
        self._autodetect_toggle.clicked.connect(
            lambda _=False: self._on_autodetect())
        lay.addWidget(self._autodetect_toggle)

        self._detect_btn = HintButton("重新检测")
        self._detect_btn.set_style("#7fd1ff", False)
        self._detect_btn.clicked.connect(lambda _=False: self._on_detect())
        lay.addWidget(self._detect_btn)

        self._device_status = QLabel("")
        self._device_status.setWordWrap(True)
        self._device_status.setStyleSheet("color: #7fd1ff; font-size: 11px;")
        lay.addWidget(self._device_status)

        # 寄存器映射提醒行（机型 → SIO 协议 + 寄存器地址，MA 风格查表）
        self._mapping_lbl = QLabel("")
        self._mapping_lbl.setWordWrap(True)
        self._mapping_lbl.setStyleSheet(
            "color: #9fb4c9; font-size: 11px; background: rgba(127,209,255,0.08);"
            " border-radius: 6px; padding: 5px 8px;")
        lay.addWidget(self._mapping_lbl)

        # 实时转速读数（实际 RPM；读不到时显示 PWM 估算）
        self._rpm_lbl = QLabel("转速：—")
        self._rpm_lbl.setStyleSheet(
            "color: #62e0a8; font-size: 13px; font-weight: bold;")
        lay.addWidget(self._rpm_lbl)

        # 转速计寄存器（十进制，可手动修正；读不到正确转速时改这里）
        rpm_row = QHBoxLayout()
        rpm_row.setSpacing(8)
        rpm_lbl = QLabel("转速计寄存器(十进制)")
        rpm_lbl.setStyleSheet("color: #cdd6e2; font-size: 11px;")
        rpm_row.addWidget(rpm_lbl)
        self._rpm_addr = QSpinBox()
        self._rpm_addr.setRange(0, 65535)
        self._rpm_addr.setFixedWidth(110)
        self._rpm_addr.setStyleSheet(
            "QSpinBox{background:rgba(255,255,255,0.08); color:#dfe6f0;"
            " border-radius:4px; padding:2px 6px;}")
        rpm_row.addWidget(self._rpm_addr)
        rpm_row.addStretch(1)
        lay.addLayout(rpm_row)
        self._rpm_addr.valueChanged.connect(
            lambda v: self._on_rpm_addr(v))

        # --- 手动 / 静音转速 -------------------------------------------
        pwm_title = QLabel("固定转速")
        pwm_title.setStyleSheet("color: #62e0a8; font-weight: bold; font-size: 13px;")
        lay.addWidget(pwm_title)

        self._manual_slider, self._manual_val = self._make_pwm_row(
            "manual", "手动转速", lay)
        self._silent_slider, self._silent_val = self._make_pwm_row(
            "silent", "静音转速", lay)

        # --- 滞回 ------------------------------------------------------
        self._hyst_slider, self._hyst_val = self._make_hyst_row(lay)

        # --- 温度来源（曲线/调速读哪里的温度）-------------------------
        # 根因：默认 acpi(WMI 热区) 在掌机比 CPU 核心低 10~20℃，导致「CPU
        # 80℃ 风扇仍低转」。提供 acpi / hwinfo(CPU Tctl/Tdie) / ec(EC 寄存器)
        # 三选一；选 ec 时显示 EC 温度寄存器地址输入框。
        temp_src_title = QLabel("温度来源（风扇曲线读哪里温度）")
        temp_src_title.setWordWrap(True)
        temp_src_title.setStyleSheet(
            "color: #62e0a8; font-weight: bold; font-size: 13px;")
        lay.addWidget(temp_src_title)

        self._temp_src_combo = _NoWheelComboBox()
        self._temp_src_combo.addItem("ACPI 热区（默认，零依赖）", "acpi")
        self._temp_src_combo.addItem("HWiNFO CPU 温度（最准，需开共享内存）", "hwinfo")
        self._temp_src_combo.addItem("EC 温度寄存器（GPD 原生）", "ec")
        self._temp_src_combo.setMinimumHeight(34)
        self._temp_src_combo.setStyleSheet(
            "QComboBox{background:rgba(255,255,255,0.08); color:#dfe6f0;"
            " border-radius:4px; padding:2px 8px; font-size:12px;"
            " combobox-popup:0;}"
            "QComboBox::drop-down{border:none; width:18px;}"
            "QComboBox QAbstractItemView{background:#1e2230; color:#dfe6f0;"
            " selection-background-color:#62e0a8;}")
        lay.addWidget(self._temp_src_combo)

        # EC 温度寄存器地址（仅来源=ec 时生效；默认隐藏）
        # 用独立 container widget 承载该行，便于整体显隐而不影响其他编辑项。
        ec_temp_container = QWidget()
        ec_temp_row = QHBoxLayout(ec_temp_container)
        ec_temp_row.setSpacing(8)
        ec_temp_row.setContentsMargins(0, 0, 0, 0)
        ec_temp_lbl = QLabel("EC 温度寄存器(十进制)")
        ec_temp_lbl.setStyleSheet("color:#cdd6e2; font-size:12px;")
        ec_temp_row.addWidget(ec_temp_lbl)
        self._ec_temp_addr = QSpinBox()
        self._ec_temp_addr.setRange(0, 65535)
        self._ec_temp_addr.setFixedWidth(110)
        self._ec_temp_addr.setStyleSheet(
            "QSpinBox{background:rgba(255,255,255,0.08); color:#dfe6f0;"
            " border-radius:4px; padding:2px 6px;}")
        ec_temp_row.addWidget(self._ec_temp_addr)
        ec_temp_row.addStretch(1)
        lay.addWidget(ec_temp_container)
        self._ec_temp_container = ec_temp_container

        self._temp_src_combo.currentIndexChanged.connect(
            lambda _=False: self._on_temp_source())
        self._ec_temp_addr.valueChanged.connect(
            lambda v: self._on_temp_source())
        # 默认来源=acpi，EC 温度寄存器行先隐藏（_on_temp_source / refresh 会按
        # 实际来源显隐）
        self._ec_temp_container.setVisible(False)

        # --- Thermostat 目标温度测试（独立窗口，不在此堆测试控件）----
        self._thermostat_btn = HintButton("Thermostat 目标温度测试")
        self._thermostat_btn.set_style("#7fd1ff", False)
        self._thermostat_btn.set_gamepad_hint("T")
        self._thermostat_btn.clicked.connect(
            lambda _=False: self._on_thermostat_test())
        lay.addWidget(self._thermostat_btn)
        self._thermostat_note = QLabel(
            "打开独立窗口实测：皮肤 vs 结温差多少、扫频曲线、实控钉温。"
            "窗口直接驱动风扇，关闭自动交还 BIOS。")
        self._thermostat_note.setWordWrap(True)
        self._thermostat_note.setStyleSheet(
            "color:#9fb4c9; font-size:10px;")
        lay.addWidget(self._thermostat_note)

        # --- 曲线拐点 --------------------------------------------------
        curve_title = QLabel("曲线拐点（温度 ℃ → 转速 %，按温度自动升序排列）")
        curve_title.setWordWrap(True)
        curve_title.setStyleSheet("color: #62e0a8; font-weight: bold; font-size: 13px;")
        lay.addWidget(curve_title)

        # 拐点操作提示（转速以百分制 0-100 显示；无 ↑/↓ 按钮，避免掌机分数 DPI
        # 下遮挡数字；改用滚轮 / 方向键或直接键入；行末 − 按钮仅用于删除整条）。
        curve_hint = QLabel(
            "操作提示：转速按 0-100% 编辑（满速=100%）   点击数字直接输入 / 滚轮 "
            "滚动 / 方向键 ±1   改动后按温度自动升序重排   行末 − 删除整条")
        curve_hint.setWordWrap(True)
        curve_hint.setStyleSheet(
            "color:#9aa6b8; font-size:11px; padding:2px 0 4px 0;")
        lay.addWidget(curve_hint)

        self._curve_container = QWidget()
        self._curve_container.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._curve_layout = QVBoxLayout(self._curve_container)
        # 行距收紧（原 8px）让多条拐点更紧凑、纵向占用更小
        self._curve_layout.setSpacing(4)
        self._curve_layout.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._curve_container)

        self._add_curve_btn = HintButton("＋ 添加拐点")
        self._add_curve_btn.set_style("#62e0a8", False)
        self._add_curve_btn.clicked.connect(lambda _=False: self._on_add_curve())
        lay.addWidget(self._add_curve_btn)

        # --- 驱动诊断（自测 / 暴力测试，与卡片同款）-------------------
        test_title = QLabel("驱动诊断")
        test_title.setStyleSheet("color: #7fd1ff; font-weight: bold; font-size: 13px;")
        lay.addWidget(test_title)
        test_row = QHBoxLayout()
        test_row.setSpacing(8)
        self._selftest_btn = HintButton("驱动自测")
        self._selftest_btn.set_style("#7fd1ff", False)
        self._selftest_btn.clicked.connect(lambda _=False: self._on_selftest())
        test_row.addWidget(self._selftest_btn)
        self._brutetest_btn = HintButton("暴力测试")
        self._brutetest_btn.set_style("#e68080", False)
        self._brutetest_btn.clicked.connect(lambda _=False: self._on_brutetest())
        test_row.addWidget(self._brutetest_btn)
        test_row.addStretch(1)
        lay.addLayout(test_row)

        # --- 卡片颜色 --------------------------------------------------
        cc_lbl = QLabel("卡片颜色")
        cc_lbl.setStyleSheet("color: #7fd1ff; font-weight: bold; font-size: 13px;")
        lay.addWidget(cc_lbl)
        self._card_color_picker = CardColorPicker(self._app, self._overlay, "fan")
        lay.addWidget(self._card_color_picker)

        lay.addStretch(1)

        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

        close = HintButton("关闭")
        close.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 6px;"
            " padding: 6px 14px 6px 18px; }")
        close.clicked.connect(self._overlay.open_layout)
        self._close = close
        hb = QHBoxLayout()
        hb.addStretch(1)
        hb.addWidget(close)
        root.addLayout(hb)

        self._curve_rows: list = []

    def resizeEvent(self, ev: Any) -> None:  # noqa: N802 - Qt camelCase
        # 让 inner 内容宽度始终不超过滚动区可视宽度（= 窗口宽度），避免编辑项
        # 在窄窗口下横向溢出被裁切。
        super().resizeEvent(ev)
        if getattr(self, "_scroll", None) is not None:
            vw = self._scroll.viewport().width()
            if vw > 0:
                self._inner.setMaximumWidth(vw)

    # -- 疑惑解答 ------------------------------------------------------
    def _open_faq(self) -> None:
        faq = self._overlay.faq()
        if faq is not None:
            faq.open_for("fan", "鼠标 标题")

    # -- 区块访问 ------------------------------------------------------
    def _block(self) -> Any:
        return self._app.blocks.get("fan") if self._app else None

    def _persist(self) -> None:
        try:
            self._app.save_all()
        except Exception:  # noqa: BLE001
            pass

    # -- 行构造 --------------------------------------------------------
    def _make_pwm_row(self, key: str, label_text: str,
                      root: "QVBoxLayout") -> "tuple[QSlider, QLabel]":
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel(label_text)
        lbl.setStyleSheet("color: #cdd6e2; font-size: 11px;")
        lbl.setFixedWidth(88)
        slider = _NoWheelSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 255)
        slider.setSingleStep(5)
        # 响应式：窄窗口收缩到 60px 下限，宽窗口占满（与降压编辑项一致）。
        slider.setMinimumWidth(60)
        slider.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Fixed)
        slider.setStyleSheet(self._SLIDER_STYLE)
        val = QLabel("0")
        val.setStyleSheet("color: #62e0a8; font-size: 11px;")
        val.setFixedWidth(56)
        val.setAlignment(Qt.AlignmentFlag.AlignRight)
        row.addWidget(lbl)
        row.addWidget(slider)
        row.addWidget(val)
        root.addLayout(row)
        slider.valueChanged.connect(
            lambda v, k=key: self._on_pwm(k, v, save=False))
        slider.sliderReleased.connect(
            lambda k=key: self._on_pwm(k, slider.value(), save=True))
        return slider, val

    def _make_hyst_row(self, root: "QVBoxLayout") -> "tuple[QSlider, QLabel]":
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel("滞回 ℃")
        lbl.setStyleSheet("color: #cdd6e2; font-size: 11px;")
        lbl.setFixedWidth(88)
        slider = _NoWheelSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 10)
        # 响应式：窄窗口收缩到 60px 下限，宽窗口占满（与降压编辑项一致）。
        slider.setMinimumWidth(60)
        slider.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Fixed)
        slider.setStyleSheet(self._SLIDER_STYLE)
        val = QLabel("3")
        val.setStyleSheet("color: #62e0a8; font-size: 11px;")
        val.setFixedWidth(56)
        val.setAlignment(Qt.AlignmentFlag.AlignRight)
        row.addWidget(lbl)
        row.addWidget(slider)
        row.addWidget(val)
        root.addLayout(row)
        slider.valueChanged.connect(lambda v: self._on_hyst(v, save=False))
        slider.sliderReleased.connect(
            lambda: self._on_hyst(slider.value(), save=True))
        return slider, val

    # -- 事件处理 ------------------------------------------------------
    def _on_pwm(self, key: str, v: int, save: bool) -> None:
        lab = self._manual_val if key == "manual" else self._silent_val
        blk = self._block()
        pct = blk.pwm_to_pct(v) if blk is not None else max(
            0, min(100, round(v / 255 * 100)))
        lab.setText(f"{pct}%")
        blk = self._block()
        if blk is not None:
            if key == "manual":
                blk.set_manual_pct(v)
                if save:
                    # 松手即切到 manual 模式并写 EC，做到「编辑项里直接控制风扇」
                    blk.set_mode("manual")
                    blk.apply()
            else:
                blk.set_silent_pct(v)
                if save:
                    blk.set_mode("silent")
                    blk.apply()
        if save:
            self._persist()
        self.refresh_rpm()

    def _on_hyst(self, v: int, save: bool) -> None:
        self._hyst_val.setText(f"{v}℃")
        blk = self._block()
        if blk is not None:
            blk.set_hysteresis_c(v)
        if save:
            self._persist()

    def _on_rpm_addr(self, v: int) -> None:
        blk = self._block()
        if blk is not None:
            blk.set_rpm_addr(v)
        self._persist()
        self.refresh_rpm()

    def _on_temp_source(self) -> None:
        blk = self._block()
        if blk is None:
            return
        src = self._temp_src_combo.currentData() or "acpi"
        ec_addr = int(self._ec_temp_addr.value())
        blk.set_temp_source(src, ec_addr)
        # ec 来源才显示 EC 温度寄存器地址行
        self._ec_temp_container.setVisible(src == "ec")
        self._persist()
        self.refresh_rpm()

    def _on_autodetect(self) -> None:
        en = self._autodetect_toggle.isChecked()
        self._autodetect_toggle.setText(
            "自动检测设备：开" if en else "自动检测设备：关")
        blk = self._block()
        if blk is not None:
            blk.set_auto_detect(en)
        self._persist()
        self.refresh()

    def _on_detect(self) -> None:
        blk = self._block()
        if blk is None:
            self._device_status.setText("风扇区块未就绪")
            return
        self._device_status.setText("检测中…")
        try:
            blk.detect_device(silent=False)
        except Exception as exc:  # noqa: BLE001
            self._device_status.setText(f"检测异常：{exc}")
            return
        self._persist()
        self.refresh()

    def _on_selftest(self) -> None:
        from .panels import run_fan_selftest_dialog
        run_fan_selftest_dialog(self)

    def _on_brutetest(self) -> None:
        from .panels import run_fan_brutetest_dialog
        run_fan_brutetest_dialog(self)

    def _on_thermostat_test(self) -> None:
        """打开 Thermostat 目标温度测试窗口（独立顶层窗口，复用活的 FanBlock）。"""
        blk = self._block()
        if blk is None:
            self._device_status.setText("风扇区块未就绪，无法打开测试窗口")
            return
        from .thermostat_test_window import ThermostatTestWindow
        win = getattr(self, "_thermostat_win", None)
        if win is None or not win.isVisible():
            win = ThermostatTestWindow(blk, self)
            self._thermostat_win = win
        win.show()
        win.raise_()
        win.activateWindow()

    # -- 曲线编辑 ------------------------------------------------------
    def _clear_curve_rows(self) -> None:
        while self._curve_layout.count():
            item = self._curve_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._curve_rows = []

    def _rebuild_curve_rows(self) -> None:
        self._clear_curve_rows()
        blk = self._block()
        # 显示前先按温度升序（稳定排序）确保曲线点恒有序；sort_curve 仅排序 +
        # 重置档位缓存，不触发 _render，避免编辑期间卡片闪烁。
        if blk is not None:
            blk.sort_curve()
        pts = list(blk.curve) if blk is not None else []
        for i, pt in enumerate(pts):
            row = QHBoxLayout()
            # 行内间距收紧（原 8px）让每条拐点纵向更扁
            row.setSpacing(6)
            row.setContentsMargins(0, 0, 0, 0)
            # 序号
            idx_lbl = QLabel(f"#{i + 1}")
            idx_lbl.setStyleSheet("color:#9aa6b8; font-size:12px;")
            idx_lbl.setFixedWidth(28)
            idx_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            # 温度（用 _NoWheelSpinBox 子类：去 ↑/↓ + 不透明重绘 + 焦点态不切
            # border + 滚轮 ignore → 修复「数字重叠 / 中线穿过 / 滚轮误改」三联 bug）
            temp = _NoWheelSpinBox(0, 110)
            temp.blockSignals(True)
            temp.setValue(int(pt["temp"]))
            temp.blockSignals(False)
            temp_unit = QLabel("℃")
            temp_unit.setStyleSheet("color:#9aa6b8; font-size:12px;")
            temp_unit.setFixedWidth(18)
            temp_unit.setAlignment(Qt.AlignmentFlag.AlignLeft)
            # 转速（百分制 0-100：UI 显示/编辑用 %，底层 pwm 仍是 0-255，由
            # _on_curve_point 在写回时换算；blk.pwm_to_pct 负责 0-255→0-100）。
            # 同样用 _NoWheelSpinBox（与温度框共用同一修复类，避免重复 QSS / 重复
            # WA_OpaquePaintEvent / 重复 wheelEvent override）。
            pwm = _NoWheelSpinBox(0, 100)
            pwm.blockSignals(True)
            pwm.setValue(blk.pwm_to_pct(int(pt["pwm"])) if blk is not None
                         else int(pt["pwm"]))
            pwm.blockSignals(False)
            pwm_unit = QLabel("%")
            pwm_unit.setStyleSheet("color:#9aa6b8; font-size:12px;")
            pwm_unit.setFixedWidth(18)
            pwm_unit.setAlignment(Qt.AlignmentFlag.AlignLeft)
            # 删除整条拐点
            rm = HintButton("−")
            rm.set_style("#e68080", False)
            rm.setMinimumHeight(24)
            rm.setFixedWidth(30)
            rm.setToolTip("删除此拐点")
            row.addWidget(idx_lbl)
            row.addWidget(temp, 1)
            row.addWidget(temp_unit)
            row.addWidget(pwm, 1)
            row.addWidget(pwm_unit)
            row.addWidget(rm)
            self._curve_layout.addLayout(row)
            # 实时写值（不立即重排，避免键盘连续输入多位数字时行跳动）
            temp.valueChanged.connect(
                lambda _v, idx=i: self._on_curve_point(idx))
            pwm.valueChanged.connect(
                lambda _v, idx=i: self._on_curve_point(idx))
            # 改动后启动延迟排序计时器：400ms 内无新改动才按温度升序重排 + 重建，
            # 键盘输入 / 滚轮 / 方向键三种改值方式都能平滑触发、互不打架。
            temp.valueChanged.connect(lambda _=False: self._schedule_curve_sort())
            pwm.valueChanged.connect(lambda _=False: self._schedule_curve_sort())
            rm.clicked.connect(lambda _=False, idx=i: self._on_remove_curve(idx))
            self._curve_rows.append((temp, pwm, rm))

    def _on_curve_point(self, idx: int) -> None:
        if not (0 <= idx < len(self._curve_rows)):
            return
        temp, pwm, _ = self._curve_rows[idx]
        blk = self._block()
        if blk is not None:
            # UI 转速是百分制 0-100，底层 pwm 仍是 0-255；写回时换算回去。
            pct = max(0, min(100, int(pwm.value())))
            pwm_raw = round(pct / 100 * blk.PWM_MAX)
            blk.set_curve_point(idx, int(temp.value()), int(pwm_raw))
        self._persist()

    def _schedule_curve_sort(self) -> None:
        """改动后延迟（400ms 安静期）按温度升序重排：避免键盘连续输入多位数字
        时每键一次就重建行导致输入框焦点丢失，也兼容滚轮 / 方向键改值。"""
        if getattr(self, "_sort_timer", None) is None:
            self._sort_timer = QTimer(self)
            self._sort_timer.setSingleShot(True)
            self._sort_timer.setInterval(400)
            self._sort_timer.timeout.connect(self._commit_curve_sort)
        self._sort_timer.start()

    def _commit_curve_sort(self) -> None:
        blk = self._block()
        if blk is None:
            return
        blk.sort_curve()
        # 重建会再次排序（幂等），并把序号 / 行顺序刷新为温度升序
        self._rebuild_curve_rows()

    def _on_remove_curve(self, idx: int) -> None:
        blk = self._block()
        if blk is None:
            return
        blk.remove_curve_point(idx)
        self._persist()
        self._rebuild_curve_rows()

    def _on_add_curve(self) -> None:
        blk = self._block()
        if blk is None:
            return
        pts = blk.curve
        last_temp = int(pts[-1]["temp"]) if pts else 60
        new_temp = min(110, last_temp + 5)
        # 新拐点默认满速（百分制 100% → 底层 255）
        blk.add_curve_point(new_temp, blk.PWM_MAX)
        self._persist()
        self._rebuild_curve_rows()

    # -- 转速读数 ------------------------------------------------------
    def refresh_rpm(self) -> None:
        blk = self._block()
        if blk is None:
            self._rpm_lbl.setText("转速：—")
            return
        self._rpm_lbl.setText("转速：" + blk.fan_rpm_display())

    # -- 公开 API ------------------------------------------------------
    def edit_target(self, token: str) -> None:
        self._key = token or "fan"
        self._card_color_picker.set_block_key(self._key)
        self.refresh()
        # 兜底：首次 resize 时 viewport 宽度可能未就绪（vw=0 被跳过），
        # 延后一拍再约束 inner 最大宽度，避免编辑项在窄窗口下超出。
        QTimer.singleShot(0, self._sync_inner_width)

    def _sync_inner_width(self) -> None:
        vw = self._scroll.viewport().width()
        if vw:
            self._inner.setMaximumWidth(vw)

    def refresh(self) -> None:
        blk = self._block()
        if blk is None:
            return
        en = bool(getattr(blk, "auto_detect", True))
        self._autodetect_toggle.blockSignals(True)
        self._autodetect_toggle.setChecked(en)
        self._autodetect_toggle.setText(
            "自动检测设备：开" if en else "自动检测设备：关")
        self._autodetect_toggle.blockSignals(False)

        self._manual_slider.blockSignals(True)
        self._manual_slider.setValue(int(blk.manual_pct))
        self._manual_slider.blockSignals(False)
        self._manual_val.setText(f"{blk.pwm_to_pct(blk.manual_pct)}%")

        self._silent_slider.blockSignals(True)
        self._silent_slider.setValue(int(getattr(blk, "silent_pct", 0)))
        self._silent_slider.blockSignals(False)
        self._silent_val.setText(f"{blk.pwm_to_pct(getattr(blk, 'silent_pct', 0))}%")

        self._hyst_slider.blockSignals(True)
        self._hyst_slider.setValue(int(blk.hysteresis_c))
        self._hyst_slider.blockSignals(False)
        self._hyst_val.setText(f"{blk.hysteresis_c}℃")

        self._rpm_addr.blockSignals(True)
        self._rpm_addr.setValue(int(getattr(blk, "ec_rpm_addr", 0)))
        self._rpm_addr.blockSignals(False)

        model = getattr(blk, "detected_model", "") or ""
        quirk = getattr(blk, "board_quirk", "") or ""
        proto = "2E2F" if quirk == "win4" else "4E4F"
        enable = getattr(blk, "ec_enable_addr", 0)
        pwm_addr = getattr(blk, "ec_pwm_addr", 0)
        rpm_addr = getattr(blk, "ec_rpm_addr", 0)
        if model:
            self._device_status.setText(
                f"识别到：{model} → {quirk} · {proto}")
        else:
            self._device_status.setText(
                f"当前映射：{quirk} · {proto}"
                "（未识别到机型，点「重新检测」）")
        self._mapping_lbl.setText(
            f"寄存器映射：{quirk} · {proto} · 使能={enable} · 转速={pwm_addr}"
            f" · 转速计={rpm_addr}")

        # 同步温度来源选择 + EC 温度寄存器地址 + ec 行可见性
        src = str(getattr(blk, "temp_source", "acpi") or "acpi")
        idx = self._temp_src_combo.findData(src)
        if idx < 0:
            idx = 0
        self._temp_src_combo.blockSignals(True)
        self._temp_src_combo.setCurrentIndex(idx)
        self._temp_src_combo.blockSignals(False)
        self._ec_temp_addr.blockSignals(True)
        self._ec_temp_addr.setValue(int(getattr(blk, "ec_temp_addr", 0) or 0))
        self._ec_temp_addr.blockSignals(False)
        self._ec_temp_container.setVisible(src == "ec")

        self._rebuild_curve_rows()
        self.refresh_rpm()

    def nav_items(self) -> list[NavItem]:
        items = [
            NavItem(self._autodetect_toggle, "toggle",
                    confirm=self._on_autodetect, hint="A"),
            NavItem(self._detect_btn, "action", confirm=self._on_detect, hint="D"),
            NavItem(self._manual_slider, "slider",
                    confirm=lambda: self._on_pwm(
                        "manual", self._manual_slider.value(), save=True),
                    nudge=lambda d: self._nudge_pwm("manual", d), hint="M"),
            NavItem(self._silent_slider, "slider",
                    confirm=lambda: self._on_pwm(
                        "silent", self._silent_slider.value(), save=True),
                    nudge=lambda d: self._nudge_pwm("silent", d), hint="S"),
            NavItem(self._hyst_slider, "slider",
                    confirm=lambda: self._on_hyst(
                        self._hyst_slider.value(), save=True),
                    nudge=lambda d: self._nudge_hyst(d), hint="H"),
            NavItem(self._rpm_addr, "spin",
                    confirm=lambda: self._on_rpm_addr(self._rpm_addr.value()),
                    nudge=lambda d: self._nudge_rpm_addr(d), hint="R"),
            NavItem(self._add_curve_btn, "action",
                    confirm=self._on_add_curve, hint="N"),
            NavItem(self._selftest_btn, "action",
                    confirm=self._on_selftest, hint="T"),
            NavItem(self._brutetest_btn, "action",
                    confirm=self._on_brutetest, hint="B"),
        ]
        if getattr(self, "_card_color_picker", None) is not None:
            items.append(NavItem(self._card_color_picker._custom_btn, "action",
                                 confirm=self._card_color_picker._pick_custom,
                                 hint="C"))
            items.append(NavItem(self._card_color_picker._follow_btn, "action",
                                 confirm=self._card_color_picker._follow))
        items.append(NavItem(self._close, "action",
                             confirm=self._overlay.open_layout, hint="X"))
        return items

    def _nudge_pwm(self, key: str, d: int) -> bool:
        slider = self._manual_slider if key == "manual" else self._silent_slider
        old = slider.value()
        nv = max(0, min(255, old + d))
        if nv == old:
            return False
        slider.setValue(nv)
        self._on_pwm(key, nv, save=True)
        return True

    def _nudge_hyst(self, d: int) -> bool:
        old = self._hyst_slider.value()
        nv = max(0, min(10, old + d))
        if nv == old:
            return False
        self._hyst_slider.setValue(nv)
        self._on_hyst(nv, save=True)
        return True

    def _nudge_rpm_addr(self, d: int) -> bool:
        old = self._rpm_addr.value()
        nv = max(0, min(65535, old + d))
        if nv == old:
            return False
        self._rpm_addr.setValue(nv)
        self._on_rpm_addr(nv)
        return True



class CardColorEditPanel(QWidget):
    """Generic edit page for cards whose only edit item is the card colour."""

    def __init__(self, app: Any, overlay: Any) -> None:
        super().__init__()
        self.setStyleSheet(
            "QLabel { background: transparent; border: none; }"
            "QPushButton { border: none; }"
            "QPushButton:hover { border: none; }"
            "QPushButton:pressed { border: none; }"
            "QPushButton:focus { border: none; }"
            "QPushButton:checked { border: none; }")
        self._app = app
        self._overlay = overlay
        self._key = ""

        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(12, 12, 12, 12)

        header = QHBoxLayout()
        header.setSpacing(8)
        back = QPushButton("← 返回")
        back.setFixedHeight(24)
        back.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #cfe6ff;"
            " border: none; border-radius: 5px;"
            " padding: 2px 10px; font-size: 11px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.16); }")
        back.clicked.connect(lambda _=False: self._overlay.open_layout())
        self._title = ClickableTitle("卡片颜色", size=PAGE_TITLE_SIZE)
        self._title.clicked.connect(lambda _=False: self._open_faq())
        header.addWidget(back)
        header.addWidget(self._title)
        header.addStretch(1)
        root.addLayout(header)

        self._preview = CardColorPreview()
        root.addWidget(self._preview)

        self._picker = CardColorPicker(app, overlay, "", on_change=self._on_change)
        root.addWidget(self._picker)

        root.addStretch(1)

        close = HintButton("关闭")
        close.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 6px;"
            " padding: 6px 14px 6px 18px; }")
        close.clicked.connect(lambda _=False: self._overlay.open_layout())
        self._close = close
        hb = QHBoxLayout()
        hb.addStretch(1)
        hb.addWidget(close)
        root.addLayout(hb)

    # -- public API -----------------------------------------------------
    def edit_target(self, token: str) -> None:
        self._key = token
        base = token.split("#")[0] if "#" in token else token
        block = self._app.registry.get(base) if self._app else None
        name = token
        if "#" in token and base == "tile":
            idx = int(token.split("#")[1])
            extra = getattr(self._app.config_manager.config,
                            "extra_tile_blocks", []) or []
            name = extra[idx].block_name if 0 <= idx < len(extra) else token
        elif block is not None:
            name = block.name
        self._title.setText(f"{_card_mark_name(name)} · 编辑项")
        self._title.set_color(_block_card_color_hex(self._app, token))
        self._picker.set_block_key(token)
        self._on_change()

    # -- FAQ（点击顶部「<名称> · 编辑项」标题打开）---------------------
    def _open_faq(self) -> None:
        faq = self._overlay.faq()
        if faq is None:
            return
        # 该页同时承载 power / 锁帧控制 / LS预设 / 主题 / 磁贴 等区块的
        # 「编辑项」界面，按当前区块映射到对应 FAQ topic。
        base = (self._key.split("#")[0]
                if self._key and "#" in self._key else (self._key or ""))
        mapping = {
            "power": "power",
            "rtss": "rtss",
            "ls": "ls",
            "theme": "breathing",
            "tile": "tile",
        }
        topic = mapping.get(base, base)
        faq.open_for(topic, "鼠标 标题")

    # -- internals ------------------------------------------------------
    def _on_change(self) -> None:
        cfg = self._app.config_manager.config if self._app else None
        csv = cfg.card_colors.get(self._key) if (cfg and self._key) else None
        if csv:
            c = _parse_color(csv)
            if not (c.r == 0 and c.g == 0 and c.b == 0 and c.a == 0):
                self._preview.set_accent(QColor(c.r, c.g, c.b))
                self._title.set_color(_hex(c))
                return
        # 无自定义卡片色：回退到该区块自身 registry 颜色（而非全局主题色），
        # 使「编辑项」页标题与卡片外框颜色一致
        # （power=橙 / rtss=浅蓝 / ls=黄 / tile=主题色 …）。
        ac_hex = _block_card_color_hex(self._app, self._key)
        c = _parse_color(_hex_to_csv(ac_hex))
        self._preview.set_accent(QColor(c.r, c.g, c.b))
        self._title.set_color(ac_hex)

    def nav_items(self) -> list[NavItem]:
        return [
            NavItem(self._picker._custom_btn, "action",
                    confirm=self._picker._pick_custom, hint="C"),
            NavItem(self._picker._follow_btn, "action",
                    confirm=self._picker._follow, hint="A"),
            NavItem(self._close, "action",
                    confirm=self._overlay.open_layout, hint="B"),
        ]



# --------------------------------------------------------------------------
# Layout editor (port of Build-EditLayout) — card rows / theme / style
# --------------------------------------------------------------------------
# 纯分页逻辑抽到 ``._pagination``（不依赖 Qt，可单测）。
# 模块级函数以 ``pg_`` 前缀导出：pg_norm_breaks / pg_pages /
# pg_page_index_of / pg_move_in_page / pg_move_to_prev_page /
# pg_move_to_next_page。
from ._pagination import (
    pg_move_in_page,
    pg_move_to_next_page,
    pg_move_to_prev_page,
    pg_norm_breaks,
    pg_page_index_of,
    pg_pages,
    pg_prune_empty_pages,
)


# ---------------------------------------------------------------------------
# 重启子进程拉起（布局编辑「↻ 重启」与 DevSync「拉取源码并重启」共用）。
#
# 2026-09-01 重写（掌机「拉取源码并重启 = 卡住 / 起不来」专项）：
#   1. 旧实现在主线程 ``time.sleep(1.0)`` 干等 → 事件循环停摆 1 秒，窗口不刷新；
#      而且只有 1 秒观察窗口：掌机冷启动要过 PyQt6 + 一堆 DLL 的载入，常常还没
#      撑到窗口出现就被判「已退出」，或反过来撑过 1 秒后晚一步崩掉，于是要么
#      拒绝自杀（用户看到「点了没反应」），要么旧进程退出后新进程才崩（程序消失）。
#      现改为 ``_RestartWatcher`` 用 QTimer 每 250ms 轮询、最长 8 秒，全程不阻塞，
#      期间 UI 可以照常刷新「正在重启…」。
#   2. 子进程 stdout/stderr 原先被丢弃：启动失败时掌机上没有任何现场信息，只能靠猜。
#      现统一落盘 logs/restart_child.log（含完整 traceback）。
#   3. Windows 下加 DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：子进程独立进程组，
#      旧进程随后退出/崩溃都不会把它连带带走，也不与父进程共享控制台句柄。
#
# 修复历史（2026-08-30）：旧实现 ``Popen([sys.executable] + sys.argv[1:])``
# 在源码模式下丢掉了 run.py —— launch.bat 以 ``python run.py --gui`` 启动时
# ``sys.argv = ['run.py', '--gui']``，拼接结果是 ``python.exe --gui``，
# Python 收到未知选项立即退出；随后旧进程 ``sys.exit(0)`` 自杀 →
# 「点重启 = 整个程序消失」。frozen exe 模式 sys.executable 即本体、
# 不需要脚本参数，原拼接恰好成立，故 bug 只打源码启动路径。
# ---------------------------------------------------------------------------
def _restart_child_log_path() -> str:
    """重启子进程的 stdout/stderr 落盘位置（掌机排查「起不来」的唯一线索）。

    返回空串 = logs 目录不可用，调用方退化为 DEVNULL。
    """
    try:
        logs = os.path.join(_devsync_base_dir(), "logs")
        os.makedirs(logs, exist_ok=True)
        return os.path.join(logs, "restart_child.log")
    except Exception:  # noqa: BLE001
        return ""


def _build_restart_cmd() -> "tuple[Optional[list[str]], Optional[str]]":
    """构造重启命令行，并保证一定带 ``--gui``。返回 ``(cmd, err)``。

    **为什么必须强制 ``--gui``**（2026-09-01 补回，此前被弄丢过一次）：
    ``run.main()`` 只在 argv 里有 ``--gui`` 时才进 GUI，否则跑 headless 引擎
    自检——跑完正常退出。所以重启命令一旦漏掉 ``--gui``，新进程会在几秒内
    「正常退出」，而旧进程那时往往已经 quit 了，用户看到的就是「点了重启，
    程序直接没了」——正是掌机上「无法重启」的经典表现。frozen_launcher 虽然
    在启动时会补 ``--gui``，但 ``--headless`` 分支只做删除、不补，原样透传
    argv 就会漏掉它。
    """
    rest = [a for a in sys.argv[1:] if a != "--headless"]
    if "--gui" not in rest:
        rest = ["--gui"] + rest
    if getattr(sys, "frozen", False):
        # 冻结 exe：sys.executable 就是程序本体，不要拼脚本路径（否则 argparse
        # 收到多余位置参数直接退出）。
        return [sys.executable] + rest, None
    # 源码模式：sys.executable 是 python.exe，必须补 run.py 脚本路径。
    # 本文件位于 <项目根>/src/ningbingjue/ui/ → 目录上跳 3 级 = 项目根。
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    run_py = os.path.join(root, "run.py")
    if not os.path.isfile(run_py):
        run_py = os.path.join(os.getcwd(), "run.py")
    if not os.path.isfile(run_py):
        return None, ("源码模式启动但找不到 run.py（试过 " + root +
                      " 与当前目录），无法自动重启；请手动重新运行 launch.bat")
    return [sys.executable, run_py] + rest, None


def _spawn_restart_child_proc() -> "tuple[Any, Optional[str]]":
    """拉起重启子进程并**立即返回**（不等待）。返回 ``(proc, err)``。

    等待/存活验证交给 :class:`_RestartWatcher` 异步做，避免阻塞 Qt 主线程。
    """
    cmd, err = _build_restart_cmd()
    if err:
        return None, err
    env = dict(os.environ)
    # 让 run.py 单实例互斥放行：旧进程尚未退出仍持有互斥，新进程必须先起来。
    env["NINGBING_RESTART"] = "1"

    log_path = _restart_child_log_path()
    fh = None
    if log_path:
        try:
            fh = open(log_path, "a", encoding="utf-8", errors="replace")
            fh.write("\n===== restart child %s =====\ncmd=%r\ncwd=%r\n"
                     % (time.strftime("%m-%d %H:%M:%S"), cmd, os.getcwd()))
            fh.flush()
        except Exception:  # noqa: BLE001 - 日志不可用就退化，不影响拉起
            fh = None
    flags = 0
    if os.name == "nt":
        # CREATE_NEW_PROCESS_GROUP：独立进程组，旧进程退出/崩溃不牵连新进程。
        # DETACHED_PROCESS：不继承父进程的控制台。
        # CREATE_NO_WINDOW：源码模式（python.exe run.py --gui）若不显式禁窗口，
        #   重启会闪一个黑控制台——掌机上看着就像「程序崩了一下」。
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        proc = subprocess.Popen(
            cmd, env=env, cwd=os.getcwd(),
            stdin=subprocess.DEVNULL,
            stdout=(fh if fh is not None else subprocess.DEVNULL),
            stderr=(subprocess.STDOUT if fh is not None else subprocess.DEVNULL),
            creationflags=flags, close_fds=True,
        )
    except Exception as exc:  # noqa: BLE001
        # 最后退路：Popen 拉不起（CreateProcess 失败 / 被杀软拦截 / 权限问题）
        # 时退回「原地 execl 替换当前进程」——这是 Popen 版之前的原始做法，
        # 作为兜底保留（tests/test_restart.py 锁着这条契约）。
        # 语义：execl 成功则**永不返回**（进程镜像被替换）；失败必抛异常。
        # 所以能执行到下面一行，只可能是测试替身返回了。
        _devsync_log("[restart] Popen 失败，退回 execl：" + repr(exc))
        try:
            os.execl(cmd[0], *cmd)
        except Exception:  # noqa: BLE001
            return None, "拉起重启子进程失败：" + str(exc)
        return None, None  # execl 已接管，无子进程对象可验证
    finally:
        # 子进程已 dup 走句柄，父进程这边可以立刻关。
        if fh is not None:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass
    # pid 用 getattr 取：日志只是旁证，绝不能因为取不到 pid 就把整个重启流程打断。
    _devsync_log("[restart] spawned pid=" + str(getattr(proc, "pid", "?"))
                 + " cmd=" + repr(cmd))
    return proc, None


class _RestartWatcher(QObject):
    """异步确认重启子进程真的活下来：每 250ms 轮询一次，最长 8 秒。

    全程不阻塞 Qt 主线程，UI 可继续刷新「正在重启…」文案；观察窗口从 1 秒
    放宽到 8 秒，掌机冷启动再慢也不会被误判成「秒退」。
    """

    succeeded = pyqtSignal()
    failed = pyqtSignal(str)

    TICK_MS = 250
    GRACE_S = 8.0

    def __init__(self, proc: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self._proc = proc
        self._elapsed = 0.0
        self._done = False
        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._on_tick)

    def start(self) -> None:
        self._timer.start()

    def _on_tick(self) -> None:
        if self._done:
            return
        self._elapsed += self.TICK_MS / 1000.0
        rc = self._proc.poll()
        if rc is not None:
            self._finish(False, self._explain(rc))
            return
        if self._elapsed >= self.GRACE_S:
            self._finish(True, "")

    def _explain(self, rc: Optional[int]) -> str:
        msg = ("重启子进程启动后 %.1fs 就退出了（rc=%s），已保持当前实例运行。"
               % (self._elapsed, rc))
        if rc == 0:
            msg += ("\n\nrc=0 多半是「提权跳板」：子进程发现自己不是管理员，用 runas "
                    "另起一个提权副本后就正常退出，这里看到的其实是跳板的退出码。"
                    "请用 launch.bat（它已经提权）或直接双击 exe 启动。")
        msg += "\n\n完整启动输出见 logs/restart_child.log"
        return msg

    def _finish(self, ok: bool, msg: str) -> None:
        self._done = True
        try:
            self._timer.stop()
        except Exception:  # noqa: BLE001
            pass
        if ok:
            _devsync_log("[restart] 子进程存活确认（%.1fs）" % self._elapsed)
            self.succeeded.emit()
        else:
            _devsync_log("[restart] 子进程退出：" + msg.replace("\n", " "))
            self.failed.emit(msg)


def _start_async_restart(parent: Any, on_ok: Callable,
                         on_fail: Callable) -> Optional["_RestartWatcher"]:
    """GUI 统一入口：拉起重启子进程 + 异步存活验证 + 成功/失败回调。

    返回 watcher（调用方务必保存引用防 GC），拉起阶段即失败时返回 None
    并直接调用 ``on_fail``。
    """
    proc, err = _spawn_restart_child_proc()
    if err:
        on_fail(err)
        return None
    if proc is None:
        # execl 路径：进程镜像已被替换，没有子进程对象可验证，直接算接管成功。
        on_ok()
        return None
    # parent 只有确实是 QObject 才能传给 watcher（QObject 的父参不接受别的
    # 类型，传错会抛 TypeError 把整条重启链路打断）。watcher 的生命周期由
    # 调用方保存的引用保证，不依赖 Qt 父子关系。
    w = _RestartWatcher(proc, parent if isinstance(parent, QObject) else None)
    w.succeeded.connect(on_ok)
    w.failed.connect(on_fail)
    w.start()
    return w


def _spawn_restart_child() -> Optional[str]:
    """同步版：拉起并轮询等待（最长 GRACE_S 秒）。

    返回 None = 子进程存活；返回 str = 失败原因（调用方保持运行）。
    **仅保留给离线脚本 / 测试调用**——它会阻塞调用方，GUI 路径一律用
    :func:`_start_async_restart`，否则主线程会被 sleep 冻住。
    """
    proc, err = _spawn_restart_child_proc()
    if err:
        return err
    if proc is None:
        return None  # execl 已接管
    deadline = time.monotonic() + _RestartWatcher.GRACE_S
    while time.monotonic() < deadline:
        rc = proc.poll()
        if rc is not None:
            return ("重启子进程启动后立即退出（rc=" + str(rc) +
                    "），已保持当前实例运行")
        time.sleep(_RestartWatcher.TICK_MS / 1000.0)
    return None


def _quit_after_restart(app: Any) -> None:
    """重启子进程确认存活后，干净退出当前实例。

    走主窗口 ``_quit_app()``（save_all + close + quit，含 closeEvent 收尾与
    aboutToQuit 清理钩子），而不是在 Qt slot 里裸 ``sys.exit(0)`` ——
    SystemExit 从 slot 抛出会绕过 closeEvent 的正常清理路径（停线程/关子进程/
    复位降压等），正是旧实现 docstring 承诺却没做到的那一步。主窗口缺失时
    退化为 QApplication.quit() 兜底。
    """
    win = getattr(app, "_window", None)
    if win is not None and hasattr(win, "_quit_app"):
        try:
            win._quit_app()
            return
        except Exception:  # noqa: BLE001 - quit() 兜底保证一定退
            pass
    try:
        from PyQt6.QtWidgets import QApplication as _QApp
        _q = _QApp.instance()
        if _q is not None:
            _q.quit()
    except Exception:  # noqa: BLE001
        pass


class LayoutEditPanel(QWidget):
    def __init__(self, app: Any, overlay: Any) -> None:
        super().__init__()
        self.setStyleSheet(
            "QLabel { background: transparent; border: none; }"
            "QPushButton { border: none; }"
            "QPushButton:hover { border: none; }"
            "QPushButton:pressed { border: none; }"
            "QPushButton:focus { border: none; }"
            "QPushButton:checked { border: none; }"
        )
        self._app = app
        self._overlay = overlay
        # 重启观察器（QTimer 轮询）必须存住引用，局部变量被 GC 后轮询会停摆。
        self._restart_watcher: Optional["_RestartWatcher"] = None

        root = QVBoxLayout(self)
        root.setSpacing(4)
        root.setContentsMargins(8, 8, 8, 8)

        # Title row with action buttons.
        hdr_row = QHBoxLayout()
        title = QLabel("布局编辑")
        title.setStyleSheet(
            f"QLabel{{color: {_hex(get_theme_accent())};"
            f"font-size: {PAGE_TITLE_SIZE}px; font-weight: bold;}}")
        hdr_row.addWidget(title)
        # Restart button — relaunches the whole program (mirrors clicking the
        # launcher again).  Styled with a warning tint because it is destructive.
        self._restart_btn = HintButton("↻ 重启")
        self._restart_btn.setStyleSheet(
            "QPushButton { background: rgba(255,170,60,0.16); color: #ffce8a;"
            " border: none; border-radius: 6px;"
            " padding: 4px 12px; font-size: 11px; font-weight: bold;"
            " text-align: center; }"
            "QPushButton:hover { background: rgba(255,170,60,0.28); }")
        self._restart_btn.clicked.connect(self._restart)
        hdr_row.addWidget(self._restart_btn)
        hdr_row.addStretch(1)
        # Window edge-alignment toggle (left / right of screen).  Lives to the
        # LEFT of the +磁贴 / ＋分页 buttons as requested; flips AppConfig.
        # window_align, re-fits the HUD window, and persists the choice.
        self._align_btn = HintButton("贴右 ⇄")
        self._align_btn.setStyleSheet(
            "QPushButton { background: rgba(180,160,255,0.16); color: #cdbcff;"
            " border: none; border-radius: 6px;"
            " padding: 4px 8px; font-size: 11px; font-weight: bold; }"
            "QPushButton:hover { background: rgba(180,160,255,0.30); }")
        self._align_btn.clicked.connect(self._toggle_window_align)
        hdr_row.addWidget(self._align_btn)
        self._new_tile_btn = HintButton("+ 磁贴")
        self._new_tile_btn.setStyleSheet(
            "QPushButton { background: rgba(95,175,235,0.22); color: #8fd4ff;"
            " border: none; border-radius: 6px;"
            " padding: 4px 8px; font-size: 11px; font-weight: bold; }"
            "QPushButton:hover { background: rgba(95,175,235,0.38); }")
        self._new_tile_btn.clicked.connect(self._new_tile_block)
        self._add_page_btn = HintButton("＋分页")
        self._add_page_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #b0bdd0;"
            " border: none; border-radius: 6px;"
            " padding: 4px 8px; font-size: 11px; font-weight: bold; }"
            "QPushButton:hover { background: rgba(255,255,255,0.16); }")
        self._add_page_btn.clicked.connect(self._add_page_break)
        hdr_row.addWidget(self._new_tile_btn)
        hdr_row.addWidget(self._add_page_btn)
        root.addLayout(hdr_row)
        # Reflect the persisted alignment on the toggle label at open time.
        self._sync_align_btn()

        # The card list lives on a rounded dark card so the editor reads as
        # one contained surface instead of a sharp rectangle punched into the
        # page.  The QScrollArea is kept transparent so the rounded card
        # beneath it shows through; the inner widget keeps a small padding so
        # the row capsules never touch the rounded corners.
        self._list_card = QFrame()
        self._list_card.setFrameStyle(QFrame.Shape.NoFrame)
        self._list_card.setStyleSheet(
            "QFrame { background: rgba(20, 24, 33, 0.88);"
            " border: 1px solid rgba(255, 255, 255, 0.08);"
            " border-radius: 12px; }")
        card_lay = QVBoxLayout(self._list_card)
        card_lay.setContentsMargins(6, 6, 6, 6)
        card_lay.setSpacing(0)

        # 保留引用：抽屉高度动画期间要临时冻结它（见 _freeze_list）。
        scroll = self._list_scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        apply_thin_scroll(scroll, always_on=False)
        self._list_inner = QWidget()
        self._list_inner.setStyleSheet("QWidget{background:transparent;}")
        self._list_lay = QVBoxLayout(self._list_inner)
        self._list_lay.setContentsMargins(0, 0, 0, 0)
        self._list_lay.setSpacing(2)
        scroll.setWidget(self._list_inner)
        card_lay.addWidget(scroll)
        root.addWidget(self._list_card, 1)
        # 鼠标单击选中的卡片 token（即「卡片光标」）。独立于 InteractionController
        # 的焦点框——后者只在手柄激活后才显示，纯鼠标用户需要这里自己的选中高亮。
        self._sel_t: str | None = None

        # Button style editor — moved to a SEPARATE inline interface
        # (ButtonEditPanel) so it no longer clutters the layout editor.  This
        # entry button opens that interface (mirrors each functional card's
        # dedicated edit page).
        # 快捷键提示：可视化键帽（GamepadHintBar）+ 鼠标手势芯片，替代旧纯文字。
        root.addWidget(self._build_hotkey_hint())
        self._btn_style_open = HintButton("按钮风格编辑")
        self._btn_style_open.setStyleSheet(
            "QPushButton { background: rgba(255,215,120,0.16); color: #ffd778;"
            " border: none; border-radius: 6px;"
            " padding: 5px 12px 5px 22px; font-size: 11px; font-weight: bold; }"
            "QPushButton:hover { background: rgba(255,215,120,0.30); }")
        self._btn_style_open.clicked.connect(lambda _=False: self._overlay.open_button())
        root.addWidget(self._btn_style_open)

        # All theme colours are driven by the breathing-light colour (the
        # single accent knob) — there is no separate theme selector here.

        # Breathing light — customized via a dedicated sub-page (opened from
        # the footer button below), so there is no inline speed slider here.
        #
        # Footer is split into two rows so the large Save/Cancel action buttons
        # can be right-aligned without crowding the smaller auxiliary buttons.
        foot = QVBoxLayout()
        foot.setSpacing(6)
        foot.setContentsMargins(0, 0, 0, 0)

        top_foot = QHBoxLayout()
        top_foot.setSpacing(4)
        top_foot.setContentsMargins(0, 0, 0, 0)
        self._breath_btn = HintButton("呼吸灯")
        self._breath_btn.setStyleSheet(
            "QPushButton { background: rgba(255,170,200,0.16); color: #ffb3d1;"
            " border: none; border-radius: 6px;"
            " padding: 7px 14px; font-size: 11px; font-weight: bold; }"
            "QPushButton:hover { background: rgba(255,170,200,0.30); }")
        self._breath_btn.clicked.connect(
            lambda _=False: self._overlay.open_breathing())
        # Shortcut-button editor — opened from the footer row, right of the
        # breathing-light button.  This is the only entry point now that the
        # main-window header button has been removed.  Delegates to
        # EditOverlay.open_shortcut().
        self._shortcut_edit_btn = HintButton("快捷键")
        self._shortcut_edit_btn.setStyleSheet(
            "QPushButton { background: rgba(127,209,255,0.16); color: #a9d8ff;"
            " border: none; border-radius: 6px;"
            " padding: 7px 14px; font-size: 11px; font-weight: bold; }"
            "QPushButton:hover { background: rgba(127,209,255,0.30); }")
        self._shortcut_edit_btn.clicked.connect(
            lambda _=False: self._overlay.open_shortcut())
        # 「快捷键」编辑按钮：取消其手柄提示（用户明确要求不再显示 A 提示）。
        self._shortcut_edit_btn.set_hint_suppressed(True)
        self._xinput_gate_btn = HintButton("手柄隔离")
        self._xinput_gate_btn.setStyleSheet(
            "QPushButton { background: rgba(93,202,165,0.16); color: #5dcaa5;"
            " border: none; border-radius: 6px;"
            " padding: 7px 14px; font-size: 11px; font-weight: bold; }"
            "QPushButton:hover { background: rgba(93,202,165,0.30); }")
        self._xinput_gate_btn.clicked.connect(
            lambda _=False: self._overlay.open_xinput_gate())
        # 调试同步：进入掌机 ↔ 笔记本 源码/日志同步面板（开发测试用）。
        self._devsync_btn = HintButton("调试同步")
        self._devsync_btn.setStyleSheet(
            "QPushButton { background: rgba(143,208,255,0.16); color: #8fd0ff;"
            " border: none; border-radius: 6px;"
            " padding: 7px 14px; font-size: 11px; font-weight: bold; }"
            "QPushButton:hover { background: rgba(143,208,255,0.30); }")
        self._devsync_btn.clicked.connect(
            lambda _=False: self._overlay.open_dev_sync())
        # 排版效果：进入「排版效果」子页，调整卡片间距 / 外圈边距并实时套用。
        self._layout_effect_btn = HintButton("排版效果")
        self._layout_effect_btn.setStyleSheet(
            "QPushButton { background: rgba(127,209,255,0.16); color: #a9d8ff;"
            " border: none; border-radius: 6px;"
            " padding: 7px 14px; font-size: 11px; font-weight: bold; }"
            "QPushButton:hover { background: rgba(127,209,255,0.30); }")
        self._layout_effect_btn.clicked.connect(
            lambda _=False: self._overlay.open_layout_effect())
        # 主题与蒙版：底板 / 单卡 / 编辑蒙版统一编辑入口（2026-08-30 新增）。
        self._theme_mask_btn = HintButton("主题与蒙版")
        self._theme_mask_btn.setStyleSheet(
            "QPushButton { background: rgba(255,120,120,0.16); color: #ff9b9b;"
            " border: none; border-radius: 6px;"
            " padding: 7px 14px; font-size: 11px; font-weight: bold; }"
            "QPushButton:hover { background: rgba(255,120,120,0.30); }")
        self._theme_mask_btn.clicked.connect(
            lambda _=False: self._overlay.open_theme_mask())
        # 底部 6 个次级按钮不再固定平铺：屏宽不足时低优先按钮移入「更多」抽屉
        # （方案 E 响应式布局）。实际落位由 _reflow_footer() 在 resize 时计算。
        self._more_btn = HintButton("更多 ▾")
        self._more_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.10); color: #dfe6f0;"
            " border: none; border-radius: 6px;"
            " padding: 7px 14px; font-size: 11px; font-weight: bold; }"
            "QPushButton:hover { background: rgba(255,255,255,0.18); }")
        self._more_btn.clicked.connect(lambda _=False: self._toggle_drawer())
        self._more_btn.setVisible(False)
        # 抽屉容器：放不下的低优先按钮在此竖排（内嵌面板，非弹窗）。
        self._drawer = QWidget()
        self._drawer.setStyleSheet(
            "QWidget { background: rgba(255,255,255,0.04);"
            " border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; }")
        self._drawer_lay = QVBoxLayout(self._drawer)
        self._drawer_lay.setContentsMargins(6, 6, 6, 6)
        self._drawer_lay.setSpacing(4)
        # 抽屉展开/收起动画（方案 A）：动画 maximumHeight，抽屉**留在布局流里**，
        # 展开时挤压上方卡片列表区腾出空间 —— 不遮挡「保存/取消」、也不会溢出
        # 窗口底部（foot 就在面板最底部，向下浮层放不下 110-175px 的抽屉）。
        # 最小高度必须置 0，否则动画无法把抽屉压到完全收起。
        self._drawer.setMinimumHeight(0)
        self._drawer.setMaximumHeight(0)
        self._drawer.setVisible(False)
        # 动画对象必须挂在 self 上保引用：局部变量会被 GC，动画静默不跑。
        self._drawer_anim: Any = None
        # 动画目标高度（展开态），由 _drawer_target_height() 每次实时算。
        # 响应式收起状态（初始全常驻，首帧 resize 后由 _reflow_footer 重算）。
        self._top_foot = top_foot
        self._foot_gap = 4
        # 优先级（值小=重要，最晚被收起）；_footer_order 按优先级降序。
        self._footer_order = [
            self._breath_btn,        # 1 呼吸灯
            self._theme_mask_btn,    # 2 主题与蒙版
            self._shortcut_edit_btn, # 3 快捷键
            self._layout_effect_btn, # 4 排版效果
            self._xinput_gate_btn,   # 5 手柄间隔
            self._devsync_btn,       # 6 调试同步
        ]
        self._footer_confirm = {
            self._breath_btn: lambda: self._overlay.open_breathing(),
            self._shortcut_edit_btn: lambda: self._overlay.open_shortcut(),
            self._xinput_gate_btn: lambda: self._overlay.open_xinput_gate(),
            self._devsync_btn: lambda: self._overlay.open_dev_sync(),
            self._layout_effect_btn: lambda: self._overlay.open_layout_effect(),
            self._theme_mask_btn: lambda: self._overlay.open_theme_mask(),
        }
        self._visible_footer: list = list(self._footer_order)
        self._drawer_buttons: list = []
        self._drawer_open = False

        bot_foot = QHBoxLayout()
        bot_foot.setSpacing(6)
        bot_foot.setContentsMargins(0, 0, 0, 0)
        self._save_btn = HintButton("保存")
        self._cancel_btn = HintButton("取消")
        # 布局编辑页底部「保存 / 取消」做成**扁的宽矩形**（固定 104×28，
        # 宽明显大于高），与其他按钮同款圆角 / 字号 / 配色。HintButton 默认
        # 最小高度 29 + 最小宽度 56，会让它们显得偏方甚至接近正方形；这里用
        # setFixedSize 显式定宽定高。宽度 < 112 仍走顶部手柄提示胶囊
        #（Y/B 常驻），不破坏提示布局。
        _foot_btn_w, _foot_btn_h = 104, 28
        self._save_btn.setStyleSheet(
            "QPushButton { background: #82ff1e; color: #0c0f16; border: none;"
            " border-radius: 6px; padding: 3px 8px; font-size: 11px;"
            " font-weight: bold; }"
            "QPushButton:hover { background: #9dff4a; }")
        self._cancel_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.1); color: #dfe6f0;"
            " border: none; border-radius: 6px;"
            " padding: 3px 8px; font-size: 11px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.18); }")
        self._save_btn.setFixedSize(_foot_btn_w, _foot_btn_h)
        self._cancel_btn.setFixedSize(_foot_btn_w, _foot_btn_h)
        self._save_btn.clicked.connect(self._save)
        self._cancel_btn.clicked.connect(self._cancel)
        # 保存(Y)/取消(B) 手柄提示「常驻显示」：即便手柄导航未激活也亮着，
        # 方便用户随时知道可用按键（布局编辑页的核心动作按钮）。
        self._save_btn.set_gamepad_hint("Y")
        self._save_btn.set_hint_long_press(True)  # Y=保存=长按，徽章显示拖尾
        self._cancel_btn.set_gamepad_hint("B")
        self._save_btn.set_hint_always_visible(True)
        self._cancel_btn.set_hint_always_visible(True)
        bot_foot.addStretch(1)
        bot_foot.addWidget(self._save_btn)
        bot_foot.addWidget(self._cancel_btn)

        foot.addLayout(top_foot)
        foot.addWidget(self._drawer)
        foot.addLayout(bot_foot)
        root.addLayout(foot)

        self.order: list[str] = []
        self.visible: dict[str, bool] = {}
        self._page_breaks: list[int] = [0]

    def _sync_align_btn(self) -> None:
        """Reflect the persisted window-edge alignment on the toggle label."""
        try:
            cfg = self._app.config_manager.config
            left = getattr(cfg, "window_align", "right") == "left"
        except Exception:
            left = False
        self._align_btn.setText("贴左 ⇄" if left else "贴右 ⇄")

    def _toggle_window_align(self) -> None:
        """Flip the HUD window between hugging the left / right screen edge.

        Persists the choice to layout.json and immediately re-fits the window
        so the change is visible without a restart.
        """
        try:
            cfg = self._app.config_manager.config
        except Exception:
            cfg = None
        if cfg is None:
            return
        left = getattr(cfg, "window_align", "right") == "left"
        cfg.window_align = "left" if not left else "right"
        try:
            self._app.config_manager.save()
        except Exception:
            pass
        try:
            overlay = self._overlay
            if overlay is not None and hasattr(overlay, "_fit_to_screen"):
                overlay._fit_to_screen()
        except Exception:
            pass
        self._sync_align_btn()
        side = "左" if cfg.window_align == "left" else "右"
        self._toast(f"窗口已贴{side}屏", success=True)

    def _new_tile_block(self) -> None:
        """Create a COMPLETELY NEW tile-block panel with its own name/colour/tiles.

        Mirrors PS1 ``New-TileBlock`` (blocks/tile.ps1:534-559):
        1. Generates a unique name like "新区块_1" / "新区块_2" ...
        2. Creates a new ``ExtraTileBlock`` entry in ``config.extra_tile_blocks``.
        3. Appends 'tile' to ``config.cards`` if not present (first block).
        4. Calls ``Build-EditLayout`` → ``_render_rows()`` to show the new row.
        5. Selects the newly-created block in the editor.

        This is a FULL tile block (not just a single tile within the existing
        block).  Each block has its own name, theme colour, and grid of tiles
        — exactly matching the original PS1 behaviour.
        """
        from ..core.config import ExtraTileBlock
        cfg = self._app.config_manager.config
        # Generate unique block name.
        extra = list(getattr(cfg, "extra_tile_blocks", []) or [])
        used_names = {eb.block_name for eb in extra}
        # Also consider the base tile block's name.
        base_tiles = getattr(cfg, "tiles", None) or []
        if base_tiles:
            base_name = base_tiles[0].block_name if base_tiles[0].block_name else "主磁贴"
            used_names.add(base_name)
        n = len(extra) + 1
        block_name = f"新区块_{n}"
        while block_name in used_names:
            n += 1
            block_name = f"新区块_{n}"
        # Create the extra tile block with a default tile so the panel renders.
        new_eb = ExtraTileBlock(
            block_name=block_name,
            show_block_title=True,
            tiles=[Tile(name="新磁贴", launch_path="", launch_type="FILE",
                         col=0, row=0, width=1, height=1,
                         block_name=block_name,
                         show_block_title=True, show_tile_name=True)],
        )
        cfg.extra_tile_blocks = extra + [new_eb]
        # Ensure 'tile' card is visible.
        cards = list(cfg.cards) if cfg.cards else []
        if "tile" not in cards:
            cfg.cards = cards + ["tile"]
        self._app.save_all()
        # Refresh the order from config (so tile#N entries appear).
        self.order = list(self._app.full_block_order())
        self.visible = {t: True for t in self.order}
        # Rebuild editor rows + HUD panels.
        self._render_rows()
        try:
            self._app._window.rebuild()
        except Exception:
            pass
        # Toast feedback.
        try:
            block = self._app.blocks.get("tile")
            if block and hasattr(block, "show_toast"):
                block.show_toast(f"已新建区块: {block_name}", success=True)
        except Exception:
            pass
        # Update button text to show count.
        cnt = len(extra) + 1 + (1 if base_tiles else 0)
        self._new_tile_btn.setText(f"+ 磁贴({cnt})")

    def _add_page_break(self) -> None:
        """＋分页：把当前【最后一页】从中间切成两页。

        具体做法：取最后一页区间 ``[s, e)``，在其内部靠近中点处插入一个
        断点 ``k``，把最后一页一分为二（两页都至少 1 张卡）。
        由于已「不封顶」（PAGE_CAP=None），这里不再受数量上限约束——
        断点只决定分页位置，不会造出空页。
        若最后一页只有 1 个卡片，则无法再分。
        """
        self._normalize_self_breaks()
        segs = self._pages()
        if not segs:
            return
        s, e = segs[-1]
        size = e - s
        if size < 2:
            self._toast("最后一页仅有 1 个卡片，无法再分页", success=False)
            return
        # 切分点：取靠近中点的合法位置（左右各至少 1 张）。
        k = s + max(1, size // 2)
        if k <= s or k >= e:
            self._toast("最后一页无法再分页", success=False)
            return
        self._page_breaks.append(k)
        self._normalize_self_breaks()
        self._render_rows()
        cnt = len(self._pages())
        self._add_page_btn.setText(f"＋分页({cnt})")
        self._toast(f"已分页：共 {cnt} 页", success=True)

    def refresh(self) -> None:
        self.order = list(self._app.full_block_order())
        self.visible = {
            t: t not in getattr(self._app.config_manager.config, "hidden_cards", [])
            for t in self.order
        }
        # Restore page breaks from config if any.
        pbs = getattr(self._app.config_manager.config, "page_breaks", None)
        if pbs and isinstance(pbs, list) and len(pbs) > 0:
            self._page_breaks = list(pbs)
        else:
            self._page_breaks = [0]
        # 归一化：保证读入的分页合法（不封顶，仅去重 / 强制从 0 开始 /
        # 丢弃空页），并即时清掉空页断点。
        self._normalize_self_breaks()
        self._render_rows()

    # -- 分页工具：cap=PAGE_CAP（None=不封顶），页码/边界以其为准 ----
    # 纯逻辑见 ``._pagination``（不依赖 Qt，可单测）：pg_norm_breaks /
    # pg_pages / pg_page_index_of / pg_move_in_page /
    # pg_move_to_prev_page / pg_move_to_next_page / pg_prune_empty_pages。
    def _normalize_self_breaks(self) -> list[int]:
        """把 ``self._page_breaks`` 归一化为合法起始索引列表并写回：
        去重、强制从 0 开始、丢弃空页断点（保证每页至少 1 张卡）。
        cap=PAGE_CAP（None 表示不封顶，不做自动拆分）。
        """
        self._page_breaks = pg_norm_breaks(
            len(self.order), self._page_breaks, PAGE_CAP)
        self._page_breaks = pg_prune_empty_pages(
            len(self.order), self._page_breaks)
        return self._page_breaks

    def _pages(self) -> list[tuple[int, int]]:
        """返回归一化后的各页区间 ``[(start, end), ...]``（end 不含）。"""
        return pg_pages(len(self.order), self._page_breaks, PAGE_CAP)

    def _page_index_of(self, pos: int) -> Optional[tuple[int, int, int]]:
        """返回 ``pos`` 所在页的 ``(页序号, start, end)``，不在任何页返回 None。"""
        return pg_page_index_of(len(self.order), self._page_breaks,
                             PAGE_CAP, pos)

    def _toast(self, msg: str, success: bool = True) -> None:
        try:
            blk = self._app.blocks.get("tile")
            if blk and hasattr(blk, "show_toast"):
                blk.show_toast(msg, success=success)
        except Exception:
            pass

    def _render_rows(self) -> None:
        while self._list_lay.count():
            item = self._list_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        total = len(self.order)
        segs = self._pages()
        for pi, (start, end) in enumerate(segs):
            if start >= total:
                continue
            # Page separator — show for EVERY page (matching PS1 New-PageSeparator).
            # Compact height: smaller font + no vertical padding + fixed 14px row,
            # so multi-page layouts don't waste vertical space.  The page header
            # shows its card count (no upper cap since pages are 不封顶).
            cnt = end - start
            sep = QLabel(f"══════ 第 {pi + 1} 页 · {cnt} 张 ══════")
            sep.setStyleSheet("color: #6b7a90; font-size: 9px; padding: 0px 6px;"
                              " background: transparent; border: none;")
            sep.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sep.setFixedHeight(14)
            self._list_lay.addWidget(sep)
            for pos in range(start, min(end, total)):
                self._list_lay.addWidget(self._make_row(self.order[pos], pos))
        # Pack all rows tightly at the top; a trailing stretch absorbs any
        # leftover vertical space so items are never stretched / evenly
        # distributed to fill the scroll viewport.  Empty space at the bottom
        # is intentional ("留空就留空").
        self._restyle_rows()
        self._list_lay.addStretch(1)

    def _restyle_rows(self) -> None:
        """重绘每行的选中/隐藏样式。

        选中行（``self._sel_t``）画一圈主题色边框 = 鼠标用户的「卡片光标」，
        与 InteractionController 的焦点框独立（后者仅在手柄激活后显示）。"""
        cfg = self._app.config_manager.config
        fc = getattr(cfg, "focus_color", "e6ff1e") or "e6ff1e"
        if not str(fc).startswith("#"):
            fc = "#" + str(fc)
        for i in range(self._list_lay.count()):
            card = self._list_lay.itemAt(i).widget()
            if card is None or not hasattr(card, "_type"):
                continue
            t = card._type
            visible = self.visible.get(t, True)
            sel = (t == self._sel_t)
            if visible:
                bg = "rgba(38, 44, 57, 0.96)" if sel else "rgba(28, 33, 42, 0.92)"
            else:
                bg = "rgba(13, 16, 22, 0.5)"
            # ① 单光标：选中行不再画主题色边框（避免与 _FocusRect 双重光标），
            # 仅用背景明暗区分鼠标选中行；唯一光标统一由 InteractionController._FocusRect 画。
            card.setStyleSheet(
                f"QFrame {{ background: {bg}; border-radius: 12px; border: none; }}")

    def _set_sel(self, t: str | None) -> None:
        """鼠标/键盘导航清选中（① 单光标用）：传 None 清掉整行选中高亮，
        传 token 选中该行。只改背景明暗（边框由 _FocusRect 统一画）。"""
        if self._sel_t != t:
            self._sel_t = t
            self._restyle_rows()

    def _select_row(self, t: str) -> None:
        """鼠标单击整行（非按钮空白）= 选中该行（点亮卡片光标）；若当前在卡片
        内层则先退回外层，使「单光标 + 两层」一致。焦点桥接给手柄控制器，使 Y
        短按=编辑项 / X 长按=隐藏 也能瞄准这张卡。"""
        self._set_sel(t)
        ctrl = getattr(self._overlay, "_controller", None)
        if ctrl is None:
            return
        try:
            if getattr(ctrl, "_edit_inner", False):
                ctrl.exit_card_inner()   # 退外层：清 _edit_inner 并聚焦整行 col 0
            else:
                ctrl.focus_card(t)        # 外层选中整行
            ctrl.set_input_mode("mouse")  # ③ 设备互斥：鼠标操作切到鼠标提示
        except Exception:
            pass

    def _build_hotkey_hint(self) -> QWidget:
        """底部快捷键提示：设备互斥——鼠标/键盘操作时只显示鼠标芯片，
        手柄操作时只显示手柄键帽（③），互不显示、节省竖向空间。

        手柄行长按标记用 ④-A 胶囊内圈圆点（常驻说明条）；鼠标行用左键绿 /
        右键青色点图标（③ 直接回答「能否区分左右键」）。"""
        box = QFrame()
        box.setFrameStyle(QFrame.Shape.NoFrame)
        box.setStyleSheet("QFrame{background:transparent;border:none;}")
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 2, 0, 2)
        v.setSpacing(5)

        # 手柄行：彩色键帽（④-A 常驻用内圈圆点表示长按）
        gp_row = QHBoxLayout()
        gp_row.setSpacing(6)
        gp_row.setContentsMargins(0, 0, 0, 0)
        gp_lbl = QLabel("🎮 手柄")
        gp_lbl.setStyleSheet("color:#9fb0c8;font-size:11px;font-weight:bold;")
        gp_row.addWidget(gp_lbl)
        gp_bar = GamepadHintBar([
            ("Y", "编辑项"),
            ("Y", "保存", True),
            ("X", "隐藏", True),
        ])
        gp_row.addWidget(gp_bar)
        gp_row.addStretch(1)
        hold_gp = QWidget()
        hold_gp.setLayout(gp_row)
        hold_gp.setStyleSheet("QWidget{background:transparent;}")

        # 鼠标行：左键绿 / 右键青 图标芯片
        ms_row = QHBoxLayout()
        ms_row.setSpacing(6)
        ms_row.setContentsMargins(0, 0, 0, 0)
        ms_lbl = QLabel("🖱 鼠标")
        ms_lbl.setStyleSheet("color:#9fb0c8;font-size:11px;font-weight:bold;")
        ms_row.addWidget(ms_lbl)
        ms_row.addWidget(self._mouse_chip("左键双击", "编辑项", "#82ff1e"))
        ms_row.addWidget(self._mouse_chip("右键双击", "隐藏卡片", "#5dcaa5"))
        ms_row.addStretch(1)
        hold_ms = QWidget()
        hold_ms.setLayout(ms_row)
        hold_ms.setStyleSheet("QWidget{background:transparent;}")

        v.addWidget(hold_gp)
        v.addWidget(hold_ms)
        self._hint_gp = hold_gp
        self._hint_ms = hold_ms
        hold_gp.hide()   # 默认鼠标模式，手柄行隐藏
        return box

    def _hint_set_input_mode(self, mode: str) -> None:
        """③ 设备互斥：切换底部提示行的可见性（mouse=鼠标芯片 / gamepad=手柄键帽）。"""
        gp = getattr(self, "_hint_gp", None)
        ms = getattr(self, "_hint_ms", None)
        if gp is None or ms is None:
            return
        if mode == "gamepad":
            gp.show()
            ms.hide()
        else:
            gp.hide()
            ms.show()

    def _mouse_chip(self, gesture: str, action: str, dot: str) -> QLabel:
        # ③：左键绿 / 右键青色点，一眼区分左右键，比纯文字直观且不占空间。
        c = QLabel(f'<span style="color:{dot}">●</span> {gesture} → {action}')
        c.setStyleSheet(
            "QLabel{background:rgba(255,255,255,0.07);color:#cdd8e8;"
            "border:1px solid rgba(255,255,255,0.14);border-radius:6px;"
            "padding:3px 8px;font-size:10px;}")
        c.setFixedHeight(20)
        return c

    def _make_row(self, t: str, pos: int) -> QFrame:
        # Resolve tile#N → actual block name.
        base_type = t.split("#")[0] if "#" in t else t
        block = self._app.registry.get(base_type)
        extra_idx: int | None = None
        if "#" in t and base_type == "tile":
            extra_idx = int(t.split("#")[1])
        # Display name: for extra tile blocks, use the ExtraTileBlock's name.
        if extra_idx is not None:
            extra_blocks = getattr(self._app.config_manager.config, "extra_tile_blocks", []) or []
            if extra_idx < len(extra_blocks):
                display_name = extra_blocks[extra_idx].block_name
            else:
                display_name = t
        else:
            display_name = block.name if block else t
        card = QFrame()
        card.setFrameStyle(QFrame.Shape.NoFrame)
        card.setFixedHeight(30)
        # 该行卡片是否被隐藏：隐藏行整体变暗（背景更透、标题转灰），但在编辑
        # 页仍可见、仍可被手柄聚焦（聚焦后长按 X 可恢复显示）。
        _visible = self.visible.get(t, True)
        # Rounded dark row background.  The corners ARE clipped by the
        # stylesheet border-radius (no extra attribute needed), but the fill is
        # only a few /255 brighter than the tray, so the old 8px radius + 6%-white
        # border was visually invisible (looked like a sharp rectangle).  Bump the
        # radius to 12px, the border to 14%-white, and lift the fill so the
        # rounded outline is actually perceptible against the tray.
        # 2026-08-29: 取消 1px 浅白外框（用户明确要求「去掉外框」）。保留 12px 圆角
        # 与 rgba(28,33,42,0.92) 深背景，所以胶囊形状不变，只是没有描边了。
        # 影响所有 row（tasklist/freeze/tile/procmon/power/rtss/自定义块统一脱框）。
        # 隐藏行：背景压到近乎透明 + 更暗，强化「整行变暗」的视觉反馈。
        if _visible:
            card.setStyleSheet(
                "QFrame { background: rgba(28, 33, 42, 0.92); border-radius: 12px;"
                " border: none; }")
        else:
            card.setStyleSheet(
                "QFrame { background: rgba(13, 16, 22, 0.5); border-radius: 12px;"
                " border: none; }")
        lay = QHBoxLayout(card)
        lay.setContentsMargins(6, 1, 6, 1)
        lay.setSpacing(3)
        # 卡片列表标题颜色：服从每张卡自己设定的卡片色（与主页一致），
        # 而非统一呼吸灯色。隐藏行：标题转灰，强化变暗反馈。
        cfg = self._app.config_manager.config
        csv = cfg.card_colors.get(t) if cfg else None
        if csv:
            accent = _hex(_parse_color(csv))
        elif base_type == "tile":
            accent = _hex(get_theme_accent())
        else:
            accent = _hex(block.color if block else None)
        name = QLabel(_card_mark_name(display_name))
        _name_color = accent if _visible else "#5a6478"
        name.setStyleSheet(f"color: {_name_color}; font-weight: bold; font-size: 11px;")
        if not _visible:
            name.setToolTip("已隐藏：主页面不显示；聚焦本行后长按 X 可恢复显示")
        lay.addWidget(name, 1)

        # 行内按钮排布（按需求固定顺序）：
        #   页内上移(↑) · 页内下移(↓) · ▲移动到上一页 · ▼移动到下一页
        # 「编辑项」按钮已移除（改用 手柄 Y 短按 / 鼠标左键双击 进入编辑项）。
        # 先算一下该卡片所在的页，用于禁用不可用的跨页按钮。
        segs = self._pages()
        seg = self._page_index_of(pos)
        pi = seg[0] if seg else 0
        page_count = len(segs)

        up = QPushButton("↑")
        down = QPushButton("↓")
        prev_page = QPushButton("▲")   # 三角形：挪到上一页
        next_page = QPushButton("▼")   # 三角形：挪到下一页
        for b in (up, down):
            b.setFixedSize(26, 26)
            b.setStyleSheet(
                "QPushButton { background: rgba(255,255,255,0.06); color: #b0bdd0;"
                " border: none; border-radius: 4px;"
                " font-size: 11px; padding: 0px; }"
                "QPushButton:hover { background: rgba(255,255,255,0.14); }")
        for b in (prev_page, next_page):
            b.setFixedSize(26, 26)
            b.setStyleSheet(
                "QPushButton { background: rgba(255,215,120,0.12); color: #ffd778;"
                " border: none; border-radius: 4px;"
                " font-size: 11px; padding: 0px; }"
                "QPushButton:hover { background: rgba(255,215,120,0.24); }"
                "QPushButton:disabled { background: rgba(255,255,255,0.03);"
                " color: #4a5468; border: none; }")
        up.setToolTip("页内上移：仅在本页内与前一个卡片交换位置")
        down.setToolTip("页内下移：仅在本页内与后一个卡片交换位置")
        prev_page.setToolTip("移动到上一页：把该卡片挪到上一页末尾")
        next_page.setToolTip("移动到下一页：把该卡片挪到下一页开头")
        # 没有上一页 → 禁用「移动到上一页」（不封顶，不再有“上一页已满”）
        if pi <= 0:
            prev_page.setEnabled(False)
        # 没有下一页 → 禁用「移动到下一页」
        if pi >= page_count - 1:
            next_page.setEnabled(False)
        # ②.5：鼠标点行内按钮 = 进入卡片内层（焦点移到该卡位置按钮）再执行移动，
        # 使随后手柄方向键在内层（↑/↓/▲/▼）导航，与「点击进入」一致。
        up.clicked.connect(lambda _=False, p=pos, tt=t: self._enter_inner_then(tt, p, -1, 1))
        down.clicked.connect(lambda _=False, p=pos, tt=t: self._enter_inner_then(tt, p, 1, 2))
        prev_page.clicked.connect(lambda _=False, p=pos, tt=t: self._enter_inner_then(tt, p, "prev", 3))
        next_page.clicked.connect(lambda _=False, p=pos, tt=t: self._enter_inner_then(tt, p, "next", 4))
        lay.addWidget(up)
        lay.addWidget(down)
        lay.addWidget(prev_page)
        lay.addWidget(next_page)
        # remember widgets for nav
        card._up = up
        card._down = down
        card._prev = prev_page
        card._next = next_page
        card._type = t
        card._pos = pos
        card._vis = None  # hide-toggle removed; keep attr for nav_items compat
        card._edit = None  # 行内「编辑项」按钮已移除；置 None 使 nav_items 跳过该列
        # 鼠标双击进入编辑项 / 隐藏卡片：左键双击=编辑项、右键双击=隐藏。
        # 整行（含子按钮）双击都归到该行，统一由本面板的 eventFilter 接管。
        card.installEventFilter(self)
        return card

    def eventFilter(self, obj, ev) -> bool:
        """布局编辑页行级鼠标手势。

        - 左/右键**单击**（MouseButtonPress）：选中整行（点亮卡片光标），
          不吞事件，行内按钮的 clicked 仍照常生效。
        - 左键**双击**：进入编辑项；右键**双击**：隐藏/恢复卡片。命中后返回
          True 吞掉事件，避免子按钮把同一次双击当成两次单击（如误触 ↑/↓ 移动）。
        """
        if ev.type() == QEvent.Type.MouseButtonPress:
            # 鼠标单击 = 选中该行（卡片光标）。任何按钮（左/右）都算。
            btn = ev.button()
            if btn in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
                card = obj
                while card is not None and not hasattr(card, "_type"):
                    card = card.parent()
                tok = getattr(card, "_type", None) if card is not None else None
                if tok:
                    self._select_row(tok)
                    # 返回 False：不吞掉事件，让行内 ↑/↓/▲/▼ 按钮的 clicked 仍触发。
                    return False
        if ev.type() == QEvent.Type.MouseButtonDblClick:
            card = obj
            while card is not None and not hasattr(card, "_type"):
                card = card.parent()
            tok = getattr(card, "_type", None) if card is not None else None
            if tok:
                if ev.button() == Qt.MouseButton.LeftButton:
                    self._edit_items(tok)
                    return True
                if ev.button() == Qt.MouseButton.RightButton:
                    self._toggle(tok)
                    return True
        return super().eventFilter(obj, ev)

    def _refresh_nav(self) -> None:
        """Re-point the gamepad/keyboard navigation cursor after a reorder so
        it follows the moved card (see InteractionController.refresh's edit-mode
        branch).  The controller already rebuilds nav items on a 1 s timer, but
        calling it right here snaps the cursor to the moved card immediately
        instead of leaving it on the wrong card for up to a second.  No-op if
        the controller isn't wired yet."""
        ctrl = getattr(self._overlay, "_controller", None)
        if ctrl is not None:
            try:
                ctrl.refresh()
            except Exception:
                pass

    def _hide_focus_rect(self) -> None:
        """重建行之前收起焦点框，避免新行几何未算完时焦点框残留在旧位置或左上角。
        转调 controller 的归零版（隐藏 + 几何 0 + 清 hint），避免焦点框保留旧尺寸
        在退出编辑器/切页后画成错位长条。"""
        ctrl = getattr(self._overlay, "_controller", None)
        if ctrl is None:
            return
        fn = getattr(ctrl, "_hide_focus_rect", None)
        if callable(fn):
            try:
                fn()
                return
            except Exception:
                pass
        rect = getattr(ctrl, "_rect", None)
        if rect is not None:
            try:
                rect.hide()
            except Exception:
                pass

    def _relayout_cursor(self) -> None:
        """重建行之后延迟重摆焦点框：先收起，等下一轮事件循环把新行几何算完，
        再让 refresh() 按 card_token 把焦点框摆到被移动卡片的按钮上（光标跟着卡片走）。
        必须延迟——同步摆时新行几何为 0，焦点框会被摆到 (0,0) 左上角，造成
        「飞到左上角→再等约 1 秒被同步计时器摆回按钮」的断裂感。"""
        self._hide_focus_rect()
        QTimer.singleShot(0, self._refresh_nav)

    def _scroll_to_card(self, t: str) -> None:
        """跨页移动后，把被移动卡片滚入滚动视口，使跟随它的焦点框真正可见。

        卡片列表位于一个 QScrollArea 内，其 widget 即 ``self._list_inner``，
        故 ``self._list_inner.parentWidget()`` 就是该滚动区。由于 ``_render_rows``
        会销毁并重建所有行，几何信息要到下一轮事件循环才稳定，因此延后到
        ``QTimer.singleShot(0)`` 再滚动；滚动结束后再 ``_refresh_nav()`` 重摆
        焦点框，确保它落在滚动后可见的那张卡按钮上（而非滚动前的旧坐标）。"""
        inner = getattr(self, "_list_inner", None)
        if inner is None:
            return
        area = inner.parentWidget()
        if not isinstance(area, QScrollArea):
            return

        def _do() -> None:
            for i in range(self._list_lay.count()):
                card = self._list_lay.itemAt(i).widget()
                if card is not None and getattr(card, "_type", None) == t:
                    area.ensureWidgetVisible(card)
                    break
            # 滚动后再重摆焦点框：_FocusRect 用 widget.mapTo(host) 读实时几何，
            # 必须在滚动结束（视口偏移更新）后重算，否则框会停在滚动前的旧位置。
            self._refresh_nav()

        QTimer.singleShot(0, _do)

    def _enter_inner(self, t: str, col: int = 1) -> None:
        """鼠标点行内按钮 = 进入卡片内层（焦点移到该卡位置按钮），使随后手柄
        导航在内层（↑/↓/▲/▼）工作。仅桥接 interaction.enter_card_inner。"""
        ctrl = getattr(self._overlay, "_controller", None)
        if ctrl is not None and hasattr(ctrl, "enter_card_inner"):
            try:
                ctrl.enter_card_inner(t, col)
            except Exception:
                pass

    def _enter_inner_then(self, t: str, p: int, delta, col: int = 1) -> None:
        """鼠标/手柄点行内按钮：先进入内层（焦点落到**被点按钮**所在列），
        再执行该按钮的移动动作。col 必须与被点按钮一致（↑=1 / ↓=2 / ▲=3 / ▼=4），
        否则重建后 refresh() 按 (card_token, col) 重摆焦点会落到错误按钮——
        旧实现恒传 col=1（↑），导致点 ↓ 后光标跳回 ↑（卡片下移、光标却停在上移键）。"""
        self._enter_inner(t, col)
        if delta == "prev":
            self._move_to_prev_page(p)
        elif delta == "next":
            self._move_to_next_page(p)
        else:
            self._move(p, delta)

    def _move(self, pos: int, delta: int) -> None:
        """页内上移 / 页内下移：仅在本页内与相邻卡片交换位置，
        不允许跨过本页边界（要跨页请用 ▲/▼ 按钮）。"""
        order, breaks = pg_move_in_page(
            self.order, self._page_breaks, PAGE_CAP, pos, delta)
        if order is self.order:
            return  # 被页边界挡住，未改动
        self.order = order
        self._render_rows()
        self._relayout_cursor()

    def _move_to_prev_page(self, pos: int) -> None:
        """▲ 移动到上一页：把该卡片挪到上一页末尾。

        前提：存在上一页。不封顶（PAGE_CAP=None）后不再有容量上限限制，
        所以唯一失败原因是「没有上一页」。"""
        t = self.order[pos]  # 被移动卡片的稳定 token；跨页后用它把视口滚到该卡
        res = pg_move_to_prev_page(
            self.order, self._page_breaks, PAGE_CAP, pos)
        if res is None:
            self._toast("已在第 1 页，无法再上移", success=False)
            return
        self.order, self._page_breaks = res
        self._normalize_self_breaks()   # 即时清掉可能产生的空页断点
        self._render_rows()
        self._hide_focus_rect()
        self._scroll_to_card(t)
        # 用重排后的真实位置查页号（pos 是移动前的旧位置，会报旧页号）。
        new_pos = self.order.index(t)
        seg = self._page_index_of(new_pos)
        pi = seg[0] if seg else 1
        self._toast(f"已移动到第 {pi + 1} 页")

    def _move_to_next_page(self, pos: int) -> None:
        """▼ 移动到下一页：把该卡片挪到下一页开头。

        前提：存在下一页。不封顶（PAGE_CAP=None）后不再有容量上限限制，
        所以唯一失败原因是「没有下一页」。"""
        t = self.order[pos]  # 被移动卡片的稳定 token；跨页后用它把视口滚到该卡
        res = pg_move_to_next_page(
            self.order, self._page_breaks, PAGE_CAP, pos)
        if res is None:
            self._toast("已在最后一页，无法再下移", success=False)
            return
        self.order, self._page_breaks = res
        self._normalize_self_breaks()   # 即时清掉可能产生的空页断点
        self._render_rows()
        self._hide_focus_rect()
        self._scroll_to_card(t)
        # 用重排后的真实位置查页号（pos 是移动前的旧位置，会报旧页号）。
        new_pos = self.order.index(t)
        seg = self._page_index_of(new_pos)
        pi = seg[0] if seg else 0
        self._toast(f"已移动到第 {pi + 1} 页")

    def _toggle(self, t: str) -> None:
        self.visible[t] = not self.visible.get(t, True)
        self._render_rows()

    def _edit_items(self, t: str) -> None:
        base = t.split("#")[0] if "#" in t else t
        if base == "procmon":
            self._overlay.open_procmon()
        elif base == "freeze":
            self._overlay.open_freeze()
        elif base == "power":
            self._overlay.open_power(t)
        elif base == "undervolt":
            self._overlay.open_undervolt(t)
        elif base == "perfmon":
            self._overlay.open_perfmon(t)
        elif base == "smt":
            self._overlay.open_smt(t)
        elif base == "fan":
            self._overlay.open_fan(t)
        elif base == "rtss":
            self._overlay.open_rtss(t)
        elif base == "ls":
            self._overlay.open_ls(t)
        elif base == "powerplan":
            self._overlay.open_powerplan(t)
        elif base == "tile":
            if "#" in t:
                # Open the editor for the matching additional tile block.
                parts = t.split("#")
                if len(parts) == 2 and parts[1].isdigit():
                    idx = int(parts[1])
                    extra_blocks = getattr(
                        self._app.config_manager.config, "extra_tile_blocks", []) or []
                    if 0 <= idx < len(extra_blocks):
                        self._overlay.open_tile(extra_blocks[idx])
            else:
                self._overlay.open_tile(None)
        else:
            # Every other functional card opens the generic 卡片颜色 editor.
            self._overlay.open_card_color(t)

    def _save(self) -> None:
        theme = None
        # Button style is now edited in its own sub-page (ButtonEditPanel)
        # and persisted independently — no longer part of the layout save.
        # page_breaks are the *expanded-order* indices the editor works in;
        # apply_layout normalises + de-duplicates them before storing so the
        # HUD's render_pages() stays consistent with what the user sees here.
        self._app.apply_layout(self.order, self.visible, theme,
                               page_breaks=list(self._page_breaks))
        self._app.save_all()
        self._overlay.close_requested.emit()

    def _cancel(self) -> None:
        self._overlay.close_requested.emit()

    def _restart(self) -> None:
        """点击「↻ 重启」真正重启凝冰结魄。

        旧实现只弹 toast 提示「请手动重启」，等于按钮点了没反应（且 toast 在
        某些状态下会被静默吞掉）。现流程（2026-08-30 修，与 DevSync 共用）：

        1. 先 ``save_all()`` 落盘当前布局，避免重启把正在编辑的布局弄丢；
        2. 通过 ``_start_async_restart()`` 拉起新进程（frozen exe 下
           ``sys.executable`` 即本体；源码模式补 run.py 绝对路径——旧实现
           丢掉 run.py 导致 ``python --gui`` 秒退 + 旧进程自杀 = 点重启整个
           程序消失），并用 QTimer 轮询（最长 8s）异步验证子进程存活，
           **不阻塞主线程**（旧版在主线程 sleep 1s，既冻 UI 又容易误判）；
        3. 子进程存活后走 ``_quit_after_restart()`` → 主窗口 ``_quit_app()``
           干净退出（save_all + close + quit + aboutToQuit 收尾），旧进程
           释放 ryzenadj 等互斥/句柄，新进程正常接管，无崩溃-重启竞态；
        4. 任一步失败 → 弹窗说明原因，当前实例**保持运行**，绝不再出现
           「程序关了却没重启起来」。
        """
        if getattr(self, "_restart_watcher", None) is not None:
            return  # 已在重启流程中，防连点
        # 1) 先落盘，避免重启丢失正在编辑的布局。
        try:
            self._app.save_all()
        except Exception:  # noqa: BLE001 - 退出阶段不应因保存失败阻断重启
            pass
        # 收起编辑面板：新实例起来后不该还停留在编辑态（tests/test_restart.py
        # 的 test_restart_persists_and_hides_editor 锁着这条契约）。
        try:
            self._overlay.close_requested.emit()
        except Exception:  # noqa: BLE001
            pass

        # 2) 拉起新进程并异步验证存活；失败保持当前实例运行。
        try:
            self._restart_btn.setEnabled(False)
            self._restart_btn.setText("重启中…")
        except Exception:  # noqa: BLE001
            pass
        self._restart_watcher = _start_async_restart(
            self,
            on_ok=self._on_restart_ok,
            on_fail=self._on_restart_failed,
        )

    def _on_restart_ok(self) -> None:
        """子进程已确认接管 → 干净退出当前实例。"""
        _quit_after_restart(self._app)

    def _on_restart_failed(self, msg: str) -> None:
        self._restart_watcher = None
        try:
            self._restart_btn.setEnabled(True)
            self._restart_btn.setText("↻ 重启")
        except Exception:  # noqa: BLE001
            pass
        try:
            QMessageBox.warning(self, "重启失败", msg)
        except Exception:  # noqa: BLE001
            pass




# --------------------------------------------------------------------------
# In-window overlay that hosts the three sub-modes
# --------------------------------------------------------------------------

    # -- nav ------------------------------------------------------------
    def nav_items(self) -> list[NavItem]:
        items: list[NavItem] = []
        # 页眉横幅（row 0）：4 个全局动作按钮，按视觉左右排成 cols 0..3。
        # 它们不带 card_token，属「横幅按钮行」——外层模式允许左右选按钮
        # （见 interaction._apply_grid 的 non-card 横幅特例）。
        items.append(NavItem(self._restart_btn, "action",
                             confirm=self._restart, hint="A", row=0, col=0))
        items.append(NavItem(self._align_btn, "action",
                             confirm=self._toggle_window_align, hint="A",
                             row=0, col=1))
        items.append(NavItem(self._new_tile_btn, "action",
                             confirm=self._new_tile_block, hint="A", row=0, col=2))
        items.append(NavItem(self._add_page_btn, "action",
                             confirm=self._add_page_break, hint="A", row=0, col=3))
        # 每张卡片行（row 1..N）：col0=整行聚焦（A/Enter 进内层），
        # col1..4=↑/↓/▲/▼ 四个位置按钮。整行项带 card_token 以驱动两层导航
        # 与「光标随卡走」的 re-resolution（interaction.refresh 按 card_token 重摆）。
        r = 1
        for i in range(self._list_lay.count()):
            it = self._list_lay.itemAt(i)
            if it is None:
                continue
            w = it.widget()
            if w is None or not hasattr(w, "_type"):
                continue
            t = w._type
            p = w._pos
            items.append(NavItem(w, "action", card_token=t, row=r, col=0))
            # 四个位置按钮也必须带 card_token：enter_card_inner(t, col) 靠
            # 「token+col」双键查找（漏传 = A 进内层找不到目标、焦点原地不动
            # 却置 _edit_inner=True，后续左右变成内层选按钮——即「回滚」主因）；
            # refresh 的 (token,col) 重摆与 Y=编辑项 / X长按=隐藏 同样依赖它。
            items.append(NavItem(
                w._up, "action", card_token=t, row=r, col=1,
                confirm=lambda _b=w._up, _t=t, _p=p, _d=-1:
                    self._enter_inner_then(_t, _p, _d, 1)))
            items.append(NavItem(
                w._down, "action", card_token=t, row=r, col=2,
                confirm=lambda _b=w._down, _t=t, _p=p, _d=1:
                    self._enter_inner_then(_t, _p, _d, 2)))
            items.append(NavItem(
                w._prev, "action", card_token=t, row=r, col=3,
                confirm=lambda _b=w._prev, _t=t, _p=p, _d="prev":
                    self._enter_inner_then(_t, _p, _d, 3)))
            items.append(NavItem(
                w._next, "action", card_token=t, row=r, col=4,
                confirm=lambda _b=w._next, _t=t, _p=p, _d="next":
                    self._enter_inner_then(_t, _p, _d, 4)))
            r += 1
        # 按钮风格编辑：独占一行（row r），col0。
        items.append(NavItem(
            self._btn_style_open, "action",
            confirm=lambda: self._overlay.open_button(), hint="A",
            row=r, col=0))
        r += 1
        # 页脚横幅（row r）：响应式——仅常驻可见按钮进导航；放不下的低优先
        # 按钮移入「更多」抽屉（见 _reflow_footer / _toggle_drawer）。
        col = 0
        for _b in self._visible_footer:
            items.append(NavItem(_b, "action",
                                 confirm=self._footer_confirm[_b],
                                 hint="A", row=r, col=col))
            col += 1
        if self._drawer_buttons:
            items.append(NavItem(self._more_btn, "action",
                                 confirm=self._toggle_drawer, hint="A",
                                 row=r, col=col))
            col += 1
        # 抽屉展开时，其内部按钮作为独立一行（r+1），方向键上下可进出。
        if self._drawer_open and self._drawer_buttons:
            _drow = r + 1
            for _j, _b in enumerate(self._drawer_buttons):
                items.append(NavItem(_b, "action",
                                     confirm=self._footer_confirm[_b],
                                     hint="A", row=_drow, col=_j))
            _srow = r + 2
        else:
            _srow = r + 1
        # 保存 / 取消（_srow），Y/B 常驻。
        items.append(NavItem(self._save_btn, "action",
                             confirm=self._save, hint="Y", row=_srow, col=0))
        items.append(NavItem(self._cancel_btn, "action",
                             confirm=self._cancel, hint="B", row=_srow, col=1))
        return items

    # -- 响应式页脚（方案 E：常驻个数随宽度，放不下→更多抽屉） ----------
    def _reflow_footer(self) -> None:
        """按当前屏宽贪心收起 6 个次级按钮：放得下的高优先按钮常驻，
        放不下的低优先按钮移入抽屉。须等布局有宽度后再测（width>0）。"""
        if not hasattr(self, "_footer_order"):
            return
        if self.width() <= 0:
            return
        # 重排会增删抽屉里的按钮，动画中途被打断会让高度与内容不符 → 先停掉
        # 动画（不 snap：末尾的 _apply_drawer_visual() 会把高度设为终态）。
        self._stop_drawer_anim()
        avail = self.width() - 16  # root 左右边距各 8
        gap = self._foot_gap
        more_w = self._more_btn.sizeHint().width()
        widths = {b: b.sizeHint().width() for b in self._footer_order}
        cand = list(self._footer_order)  # 优先级降序
        # 从最低优先逐个移除，直到整行（含「更多」）放得下，至少留 1 个常驻。
        while len(cand) > 1:
            need_more = len(cand) < len(self._footer_order)
            total = sum(widths[b] for b in cand) + gap * max(0, len(cand) - 1)
            if need_more:
                total += gap + more_w
            if total <= avail:
                break
            cand.pop()  # 去掉最低优先
        # 落回布局：先全部清出两个容器，再按新状态归位。
        for b in self._footer_order:
            self._top_foot.removeWidget(b)
            self._drawer_lay.removeWidget(b)
        self._top_foot.removeWidget(self._more_btn)
        while self._top_foot.count():
            self._top_foot.takeAt(0)
        for b in cand:
            self._top_foot.addWidget(b)
        if len(cand) < len(self._footer_order):
            self._top_foot.addWidget(self._more_btn)
        self._top_foot.addStretch(1)
        self._drawer_buttons = [b for b in self._footer_order if b not in cand]
        for b in self._drawer_buttons:
            self._drawer_lay.addWidget(b)
        self._visible_footer = cand
        self._apply_drawer_visual()

    def _apply_drawer_visual(self) -> None:
        """依据当前状态切换「更多」与抽屉容器的可见性 / 高度终态。

        用于**非动画**路径（页脚重排等）：直接把高度设为终态。动画路径走
        :meth:`_animate_drawer`，不要在这里设高度，否则会与动画抢值。"""
        has = bool(self._drawer_buttons)
        self._more_btn.setVisible(has)
        self._drawer.setVisible(has and self._drawer_open)
        try:
            self._drawer.setMaximumHeight(
                self._drawer_target_height() if (has and self._drawer_open) else 0)
        except Exception:
            pass

    def _toggle_drawer(self) -> None:
        """鼠标点击 / 手柄 A 在「更多」上：带高度动画地展开或收起抽屉。"""
        if not self._drawer_buttons:
            return
        self._drawer_open = not self._drawer_open
        # 收起前把焦点移回「更多」按钮：抽屉按钮的 NavItem 不带 card_token，
        # 收起后 refresh() 无法按 (card_token, col) 重新解析它们，焦点会落到
        # 一个失效的扁平索引上（表现为光标乱跳）。先归位到「更多」即可。
        if not self._drawer_open:
            ctrl = getattr(self._overlay, "_controller", None)
            if ctrl is not None and hasattr(ctrl, "_items"):
                try:
                    for _i, _it in enumerate(ctrl._items):
                        if _it.widget is self._more_btn:
                            ctrl.set_focus(_i)
                            break
                except Exception:
                    pass
        # 可见性：展开要**立刻**显示（动画从 0 高度撑开，得先看得见）；
        # 收起则**不能立刻隐藏**——否则 widget 瞬间消失，动画虽然跑了却看不
        # 见，表现为「只有展开有动画、收起是瞬间消失」。收起的隐藏交由
        # _settle_drawer_after_anim() 在高度压到 0 之后执行。
        if self._drawer_open:
            self._drawer.setVisible(True)
        self._animate_drawer(self._drawer_open)

    # -- 抽屉展开 / 收起动画（方案 A：动画 maximumHeight，留在布局流） ------
    _DRAWER_ANIM_MS = 170

    def _drawer_target_height(self) -> int:
        """展开态应有的高度：按钮竖排累加 + 间距 + 内边距。

        手动算而**不用** ``sizeHint()``：抽屉此刻可能被压到 maximumHeight=0 或
        处于隐藏态，布局尚未激活时 sizeHint 会返回不可靠的值，据此做的动画会
        算出错误高度。按钮自身的 sizeHint 只依赖字体度量，与可见性无关，可靠。
        """
        try:
            lay = self._drawer_lay
            m = lay.contentsMargins()
            h = m.top() + m.bottom()
            if not self._drawer_buttons:
                return max(0, h)
            for b in self._drawer_buttons:
                h += max(0, b.sizeHint().height())
            h += lay.spacing() * max(0, len(self._drawer_buttons) - 1)
            return max(0, h)
        except Exception:
            return 0

    def _freeze_list(self, freeze: bool) -> None:
        """抽屉动画期间冻结卡片列表滚动区，避免每帧重排卡片行。

        抽屉高度动画每帧都会让 ``_list_card`` 变矮，而它内部的 QScrollArea
        开着 ``widgetResizable=True`` —— 视口高度一变，Qt 就把内容 widget 重新
        resize 并**重排所有卡片行**，这正是「略微掉帧」的来源。动画期间临时
        关掉 widgetResizable，内容保持原尺寸不再重排，动画结束再恢复。

        只改高度、不改宽度，因此不会出现宽度方向的不适配。若上机发现列表区
        有跳动，把 ``_animate_drawer`` 里的 ``self._freeze_list(True)`` 注释掉
        即可回退（功能不受影响，仅回到掉帧状态）。
        """
        scroll = getattr(self, "_list_scroll", None)
        if scroll is None:
            return
        try:
            scroll.setWidgetResizable(not freeze)
            if not freeze:
                inner = getattr(self, "_list_inner", None)
                if inner is not None:
                    inner.updateGeometry()
        except Exception:
            pass

    def _stop_drawer_anim(self, snap: bool = False) -> None:
        """停止抽屉动画。

        ``snap=True`` 时把抽屉高度直接跳到当前状态的终态（展开=目标高 /
        收起=0）。用于 resize、页脚重排、退出编辑器等**中断**场景，避免抽屉
        卡在动画中途的高度——这与焦点框残留是同一类问题。
        """
        anim = getattr(self, "_drawer_anim", None)
        if anim is not None:
            try:
                anim.stop()
            except Exception:
                pass
            self._drawer_anim = None
        # 中断动画时务必解冻列表，否则卡片行会一直不跟随视口宽度变化。
        self._freeze_list(False)
        if snap:
            try:
                self._drawer.setMaximumHeight(
                    self._drawer_target_height() if self._drawer_open else 0)
                # 收起态被打断时补一次隐藏，否则抽屉会停在「可见但高度 0」的
                # 中间态，下次展开前一直占着一个空 widget。
                if not self._drawer_open:
                    self._drawer.setVisible(False)
            except Exception:
                pass

    def _settle_drawer_after_anim(self) -> None:
        """动画结束（或动画不可用）：固定终态高度 + 重摆焦点框。

        焦点框的重摆必须放在这里而不是动画前——动画期间抽屉按钮几何在变，
        焦点框若落在抽屉里会跟着抖；先归零隐藏、动画结束再摆一次即可。"""
        self._drawer_anim = None
        # 解冻卡片列表，让内容重新跟随视口（必须在下面 refresh() 之前，
        # 否则列表区还停在冻结尺寸，焦点框会按旧几何摆放）。
        self._freeze_list(False)
        try:
            self._drawer.setMaximumHeight(
                self._drawer_target_height() if self._drawer_open else 0)
            if not self._drawer_open:
                self._drawer.setVisible(False)
        except Exception:
            pass
        ctrl = getattr(self._overlay, "_controller", None)
        if ctrl is None:
            return
        try:
            ctrl.refresh()
            if self._drawer_open and self._drawer_buttons:
                # 展开后把焦点移入抽屉第一个按钮
                for _i, _it in enumerate(ctrl._items):
                    if _it.widget is self._drawer_buttons[0]:
                        ctrl.set_focus(_i)
                        break
        except Exception:
            pass

    def _animate_drawer(self, opening: bool) -> None:
        """用 QPropertyAnimation 动画抽屉高度（当前值 ↔ 目标高 / 0）。

        抽屉**留在布局流里**：高度变化会挤压上方卡片列表区（stretch=1）腾出
        空间，因此不遮挡「保存 / 取消」、也不溢出窗口底部。代价是每帧触发
        foot + root 重新布局，故时长压在 170ms（约 10 帧）。
        """
        target = self._drawer_target_height()
        if target <= 0:
            # 拿不到高度（尚未布局 / 无按钮）→ 退回瞬间显隐，避免动画用错值。
            self._settle_drawer_after_anim()
            return
        self._stop_drawer_anim()
        try:
            cur = max(0, int(self._drawer.maximumHeight()))
        except Exception:
            cur = 0
        # 焦点框在动画期间**保持可见**，不再隐藏：
        # 此刻焦点停在「更多」按钮上，它位于 top_foot —— 而 foot 的顺序是
        # top_foot / drawer / bot_foot，抽屉高度变化只推动**下方**的 bot_foot，
        # top_foot 位置纹丝不动，所以焦点框既不会抖、也不需要重摆。
        # 之前这里会先归零隐藏、等动画结束再摆，中间 170ms 的空档正是用户看到
        # 的「光标延迟 / 闪一下」。收起路径同理（焦点已先归位到「更多」按钮）。
        # 冻结卡片列表：抽屉每帧变高会让 _list_card 变矮，其 QScrollArea
        # (widgetResizable=True) 会重排所有卡片行 —— 这是掉帧的主因。
        self._freeze_list(True)
        try:
            anim = QPropertyAnimation(self._drawer, b"maximumHeight", self)
            anim.setDuration(self._DRAWER_ANIM_MS)
            # PyQt6 要求完整枚举限定：必须 QEasingCurve.Type.OutCubic，
            # 简写 OutCubic 会抛 AttributeError 并被下方 except 吞掉 →
            # 动画静默退化成瞬间显隐（与 page_transition/faq_popup 保持一致）。
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.setStartValue(cur)
            anim.setEndValue(target if opening else 0)
            # 必须保引用：局部变量动画对象会被 GC，动画静默不跑。
            self._drawer_anim = anim
            anim.finished.connect(self._settle_drawer_after_anim)
            anim.start()
        except Exception:
            # 动画不可用 / 异常 → 退回瞬间显隐，功能不受影响。
            self._settle_drawer_after_anim()

    def resizeEvent(self, ev: Any) -> None:  # noqa: N802 - Qt camelCase
        super().resizeEvent(ev)
        # resize 会重算页脚；动画若在中途被打断，抽屉高度会与布局错乱 →
        # 立即跳到终态（snap）再重排。
        self._stop_drawer_anim(snap=True)
        self._reflow_footer()

    def hideEvent(self, ev: Any) -> None:  # noqa: N802 - Qt camelCase
        # 退出编辑器 / 切到别的编辑页时停掉抽屉动画并跳到终态，避免下次进来
        # 抽屉卡在动画中途的高度（与焦点框残留是同一类问题）。
        self._stop_drawer_anim(snap=True)
        super().hideEvent(ev)

class _EditCard(QFrame):
    """A titled rounded card used inside the editors (mirrors the original's
    stacked RoundedPanel cards)."""

    def __init__(self, title: str, parent: QWidget | None = None,
                 on_title_click: Any = None,
                 title_color: str | None = None,
                 title_size: int = 13) -> None:
        super().__init__(parent)
        # Subtle neutral border that groups title + content without using the
        # bright theme accent.  Title is visually inside the card.
        self.setStyleSheet(
            "QFrame { background: rgba(255,255,255,0.02); border-radius: 10px;"
            " border: 1px solid rgba(255,255,255,0.08); }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(6)
        if on_title_click is not None:
            lbl = ClickableTitle(title, color=title_color, size=title_size)
            lbl.clicked.connect(lambda _=False: on_title_click())
        else:
            lbl = QLabel(title)
            col = title_color or "#7fd1ff"
            lbl.setStyleSheet(
                f"QLabel{{color: {col}; font-weight: bold; font-size: {title_size}px;"
                " background: transparent; border: none;}}")
        lay.addWidget(lbl)
        self._content = QWidget()
        self._content.setStyleSheet("background: transparent; border: none;")
        self._clay = QVBoxLayout(self._content)
        self._clay.setContentsMargins(0, 0, 0, 0)
        self._clay.setSpacing(6)
        lay.addWidget(self._content)

    def content(self) -> QVBoxLayout:
        return self._clay


# --------------------------------------------------------------------------
# Breathing-light customizer (port of the "呼吸灯自定义" sub-page)
# --------------------------------------------------------------------------
class _BreathHeadCanvas(QWidget):
    """呼吸灯整合预览的纯绘制画布。

    左→右三段：模拟 HUD 窗口 ｜ 焦点瞬态波形 ｜ 常态呼吸波形。
    两张波形都用**绝对时间轴**（见 :class:`_BreathHeadPreview` 的说明）。
    """

    def __init__(self, owner: "_BreathHeadPreview") -> None:
        super().__init__(owner)
        self._owner = owner
        self.setMinimumHeight(92)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

    # -- 布局常量 ----------------------------------------------------
    _WIN_W = 118      # 左侧模拟窗口宽度
    _GAP = 12         # 段间距

    def paintEvent(self, ev: Any) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            p.end()
            return
        o = self._owner
        self._draw_mini_win(p, 0, 0, self._WIN_W, h, o)
        wave_x = self._WIN_W + self._GAP
        wave_w = w - wave_x - self._GAP
        if wave_w >= 120:
            half = (wave_w - 10) // 2
            self._draw_transient(p, wave_x, 0, half, h, o)
            self._draw_breath(p, wave_x + half + 10, 0, half, h, o)
        p.end()

    # -- 左：模拟 HUD 窗口（失焦黑罩 + 三层呼吸环）------------------
    @staticmethod
    def _draw_mini_win(p: QPainter, x: int, y: int, w: int, h: int,
                       o: "_BreathHeadPreview") -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(10, 13, 18))
        p.drawRoundedRect(x, y, w, h, 12, 12)
        m = 10
        gap = 4
        cw = (w - 2 * m - 2 * gap) // 3
        ch = (h - 2 * m - gap) // 2
        if cw > 2 and ch > 2:
            for r_i in range(2):
                for c_i in range(3):
                    cx = x + m + c_i * (cw + gap)
                    cy = y + m + r_i * (ch + gap)
                    p.setBrush(QColor(20, 26, 36))
                    p.drawRoundedRect(cx, cy, cw, ch, 5, 5)
                    # 卡片内的强调色小块 -> 直观体现「跟随主题色」
                    p.setBrush(QColor(o._color.red(), o._color.green(),
                                      o._color.blue(), 150))
                    p.drawRoundedRect(cx + 4, cy + 4, max(6, cw // 3), 3, 1, 1)
        if o._scrim > 0.001:                       # 失焦黑罩
            p.setBrush(QColor(0, 0, 0, int(255 * o._scrim)))
            p.drawRoundedRect(x, y, w, h, 12, 12)
        if o._alpha > 0.001:                       # 三层呼吸环
            r, g, b = o._color.red(), o._color.green(), o._color.blue()
            for off, af, wd in ((2.0, 0.85, 2.0), (4.5, 0.55, 2.0),
                                (7.0, 0.30, 2.0)):
                pen = QPen(QColor(r, g, b, int(255 * af * o._alpha)))
                pen.setWidthF(wd)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(
                    QRectF(x + off, y + off, w - 2 * off, h - 2 * off),
                    10.0 - off, 10.0 - off)

    # -- 波形公共外框 ------------------------------------------------
    @staticmethod
    def _wave_frame(p: QPainter, x: int, y: int, w: int, h: int,
                    title: str) -> tuple:
        """画标题 + 上下基线，返回 ``(top, bot)`` 绘图区纵向范围。"""
        f = QFont("Microsoft YaHei")   # 显式指定中文字体 fallback
        f.setPixelSize(9)              # 显式像素字号：绝不依赖继承字体（HUD 铁律）
        p.setFont(f)
        p.setPen(QColor(174, 185, 201))
        p.drawText(x, y + 9, title)
        top = y + 15
        bot = y + h - 11
        p.setPen(QPen(QColor(255, 255, 255, 30), 1))
        p.drawLine(x, int(bot), x + w, int(bot))
        return top, bot

    # -- 中：焦点瞬态（冲亮 + 停留 + 黑罩淡出）----------------------
    @classmethod
    def _draw_transient(cls, p: QPainter, x: int, y: int, w: int, h: int,
                        o: "_BreathHeadPreview") -> None:
        span = int(o._transient_span())
        top, bot = cls._wave_frame(
            p, x, y, w, h, f"焦点瞬态 0-{span} ms")
        span_f = float(span)

        def px(ms: float) -> float:
            return x + (ms / span_f) * w

        def py(v: float) -> float:
            return bot - v * (bot - top)

        grid = QPen(QColor(255, 255, 255, 26), 1)
        p.setPen(grid)
        # 按 span 自适应网格间隔：默认 600ms 用 200ms 一格，长轴用 500ms
        step = 200 if span <= 1000 else 500
        for ms in range(step, span + 1, step):
            gx = int(px(ms))
            p.drawLine(gx, int(top), gx, int(bot))
        # 黑罩淡出（虚线，与灯冲亮同时开始）
        dash = QPen(QColor(139, 151, 170, 210), 1.6)
        dash.setStyle(Qt.PenStyle.DashLine)
        p.setPen(dash)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(int(px(0)), int(py(_BreathHeadPreview.SCRIM_MAX)),
                   int(px(o._scrim_ms)), int(py(0.0)))
        # 灯：0 -> peak(冲亮) -> 保持(停留)
        solid = QPen(o._color, 2.2)
        p.setPen(solid)
        p.drawLine(int(px(0)), int(py(0.0)),
                   int(px(o._flash_ms)), int(py(o._peak)))
        p.drawLine(int(px(o._flash_ms)), int(py(o._peak)),
                   int(px(o._flash_ms + o._hold_ms)), int(py(o._peak)))

    # -- 右：常态呼吸（周期 + 谷值）----------------------------------
    @classmethod
    def _draw_breath(cls, p: QPainter, x: int, y: int, w: int, h: int,
                     o: "_BreathHeadPreview") -> None:
        top, bot = cls._wave_frame(p, x, y, w, h, "常态呼吸 0-8.0 s")
        span = _BreathHeadPreview.BREATH_SPAN_S

        def px(t: float) -> float:
            return x + (t / span) * w

        def py(v: float) -> float:
            return bot - v * (bot - top)

        grid = QPen(QColor(255, 255, 255, 26), 1)
        p.setPen(grid)
        for s in range(1, 9):                            # 每 1s 一条
            gx = int(px(s))
            p.drawLine(gx, int(top), gx, int(bot))
        # 前 1 个周期用淡色带标出：周期一变，这条带立即伸缩
        band_w = max(1.0, (o._period / span) * w)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(o._color.red(), o._color.green(),
                          o._color.blue(), 26))
        p.drawRect(int(px(0)), int(top), int(band_w), int(bot - top))
        # 谷值参考线
        dash = QPen(QColor(o._color.red(), o._color.green(),
                           o._color.blue(), 120), 1)
        dash.setStyle(Qt.PenStyle.DashLine)
        p.setPen(dash)
        p.drawLine(int(px(0)), int(py(o._floor)),
                   int(px(span)), int(py(o._floor)))
        # 曲线：绝对时间轴 0..8s（周期越长波形越舒展，不是被归一化）
        fl = min(o._floor, o._peak)
        path = QPainterPath()
        steps = 200
        for i in range(steps + 1):
            t = (i / steps) * span
            v = fl + (o._peak - fl) * (0.5 + 0.5 * math.cos(
                2.0 * math.pi * t / max(0.1, o._period)))
            if i == 0:
                path.moveTo(px(t), py(v))
            else:
                path.lineTo(px(t), py(v))
        p.setPen(QPen(o._color, 2.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)


class _BreathHeadPreview(QFrame):
    """呼吸灯整合预览 —— 2026-08-30 重构，固定在编辑页顶部。

    取代旧的 :class:`_BreathPreview` 纯色渐变条（与任何参数都不联动，
    纯装饰）。本组件挂在 ``root`` 布局、**位于 QScrollArea 之前**（见
    :meth:`BreathingEditPanel.__init__`），因此**永远停在页面顶部**。

    **波形必须用绝对时间轴**：瞬态轴按当前 ``flash + hold`` / ``scrim``
    动态取（默认约 600 ms，避免 500 ms 事件画在 2000 ms 长空轴上；最大
    不超过 2000 ms），呼吸固定 0-8.0 s（= 周期滑块 80/10 的满量程）。
    这样参数一变，是曲线被拉长/压扁。若反过来让坐标轴跟着参数缩放，曲线
    会被"归一化"，任何周期画出来的形状都一模一样（第一版踩过的坑）。
    """

    #: 焦点瞬态波形的最大时间轴（毫秒）= 三个滑块满量程之和
    TRANS_SPAN_MAX_MS = 2000
    #: 常态呼吸波形的固定时间轴（秒）= 周期滑块满量程 80 / 10
    BREATH_SPAN_S = 8.0
    #: 失焦黑罩深度（0..1），与真实 HUD 的失焦渐隐保持一致
    SCRIM_MAX = 0.45

    def _transient_span(self) -> float:
        """焦点瞬态时间轴：按当前 flash/hold/scrim 动态取。

        默认事件约 500 ms，若固定 2000 ms 会右侧空一大截；动态取后默认约
        600 ms，紧凑同时保留少量余量。上限锁在 ``TRANS_SPAN_MAX_MS``，
        防止三项都拉满时轴被无限撑长。
        """
        return min(
            self.TRANS_SPAN_MAX_MS,
            max(600.0,
                float(self._flash_ms + self._hold_ms + 100),
                float(self._scrim_ms + 100)))

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(146)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            "QFrame { background: #0e1117;"
            " border: 1px solid rgba(255,255,255,0.08);"
            " border-radius: 10px; }")
        self._color = QColor(127, 209, 255)
        self._alpha = 0.0        # 当前辉光 0..1
        self._scrim = 0.0        # 当前黑罩 0..1
        self._floor = 0.1
        self._peak = 1.0
        self._period = 2.4
        self._flash_ms = 180
        self._hold_ms = 120
        self._scrim_ms = 200
        self._enabled = True     # False => 常亮在峰值，不再脉动
        # 动画状态机：dark | flash | hold | breathe
        self._mode = "breathe"
        self._mode_start = time.monotonic()
        self._t0 = time.monotonic()
        self._phase = math.pi / 2.0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)
        self._canvas = _BreathHeadCanvas(self)
        lay.addWidget(self._canvas, 1)
        row = QHBoxLayout()
        row.setSpacing(8)
        self._gain_btn = QPushButton("演示 获得焦点")
        self._lose_btn = QPushButton("演示 失去焦点")
        for b in (self._gain_btn, self._lose_btn):
            b.setStyleSheet(
                "QPushButton { background: rgba(255,255,255,0.08);"
                " color: #dfe6f0; border: none; border-radius: 6px;"
                " padding: 5px 10px; font-size: 11px; }"
                "QPushButton:hover { background: rgba(255,255,255,0.16); }")
            row.addWidget(b)
        self._gain_btn.clicked.connect(self.demo_gain)
        self._lose_btn.clicked.connect(self.demo_lose)
        row.addStretch(1)
        lay.addLayout(row)

        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # -- 对外 API -----------------------------------------------------
    def set_color(self, col: QColor) -> None:
        self._color = QColor(col)
        self._canvas.update()

    def set_tuning(self, floor: float, peak: float, period: float,
                   flash_ms: int, hold_ms: int, scrim_ms: int) -> None:
        """接收全部 6 个波形参数（谷值/峰值/周期/冲亮/停留/黑罩）。"""
        self._floor = max(0.0, min(1.0, float(floor)))
        self._peak = max(0.0, min(1.0, float(peak)))
        self._period = max(0.1, float(period))
        self._flash_ms = max(0, int(flash_ms))
        self._hold_ms = max(0, int(hold_ms))
        self._scrim_ms = max(0, int(scrim_ms))
        self._canvas.update()

    def set_enabled(self, on: bool) -> None:
        self._enabled = bool(on)
        self._canvas.update()

    def nav_buttons(self) -> tuple:
        """供 BreathingEditPanel.nav_items 注册手柄/键盘导航。"""
        return self._gain_btn, self._lose_btn

    def demo_gain(self) -> None:
        """播放：黑罩按 scrim 淡出，灯同时按 flash 冲到峰值。"""
        self._mode = "flash"
        self._mode_start = time.monotonic()
        self._canvas.update()

    def demo_lose(self) -> None:
        """灯熄灭、黑罩渐显（模拟 HUD 失焦）。"""
        self._mode = "dark"
        self._mode_start = time.monotonic()
        self._canvas.update()

    # -- 内部：动画状态机 ---------------------------------------------
    def _tick(self) -> None:
        now = time.monotonic()
        dt_s = now - self._mode_start
        if not self._enabled:
            # 关闭脉动 => 常亮在峰值（与开关卡片的说明文案一致）
            self._alpha = self._peak
            self._scrim = 0.0
            self._canvas.update()
            return
        if self._mode == "dark":                    # 失焦：灯灭 + 黑罩渐显
            k = min(1.0, dt_s / 0.30)
            self._alpha = max(0.0, self._alpha * (1.0 - k))
            self._scrim = self.SCRIM_MAX * k
            self._canvas.update()
            return
        if self._mode in ("flash", "hold"):         # 冲亮 + 停留
            k = (1.0 if self._flash_ms <= 0
                 else min(1.0, dt_s * 1000.0 / self._flash_ms))
            self._alpha = self._peak * k
            self._scrim = self.SCRIM_MAX * max(
                0.0, 1.0 - (dt_s * 1000.0 / max(1, self._scrim_ms)))
            if k >= 1.0 and self._mode == "flash":
                self._mode = "hold"
                self._mode_start = now
            elif self._mode == "hold" and dt_s * 1000.0 >= self._hold_ms:
                # 交棒给常态呼吸，从正弦峰值(pi/2)起步，接着往下脉动
                self._phase = math.pi / 2.0
                self._t0 = now
                self._mode = "breathe"
            self._canvas.update()
            return
        el = now - self._t0                          # 常态呼吸
        self._phase = (math.pi / 2.0
                       + 2.0 * math.pi * (el / self._period)) % (2.0 * math.pi)
        fl = min(self._floor, self._peak)
        self._alpha = fl + (self._peak - fl) * (0.5 + 0.5 * math.sin(self._phase))
        self._scrim = 0.0
        self._canvas.update()



class _TrayPreview(QFrame):
    """Live preview of the card tray.

    Shows the *real* tray background colour at the chosen opacity over a single
    neutral panel, so the hue/brightness you judge is the actual tray colour —
    no second dark panel behind it (the old ``#11151c`` double-overlay darkened
    the hue), and **no fake "desktop" rainbow texture** (the previous preview
    painted a multicolour gradient that had nothing to do with the real tray
    colour).  A subtle single-hue (theme-accent) breathing glow is blurred in at
    the top to mirror how the live HUD frosts the breathing light behind the
    tray; it uses the real accent colour, never a stand-in.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = QColor(14, 17, 23)
        self._opacity = 86
        self._border = True
        self._blur = 18
        self.setMinimumHeight(48)
        # Transparent: the painted content IS the full preview (see paintEvent).
        self.setStyleSheet(
            "QFrame { background: transparent;"
            " border: 1px solid rgba(255,255,255,0.08);"
            " border-radius: 8px; }")
        self._glow_tex: QPixmap | None = None
        self._glow_blurred: QPixmap | None = None
        self._glow_blurred_for = -1

    # -- a soft single-hue breathing glow (the real accent colour), so the
    #    gaussian blur is visibly previewed without any fake rainbow texture --
    def _ensure_glow(self) -> QPixmap:
        if self._glow_tex is not None and not self._glow_tex.isNull():
            return self._glow_tex
        tw, th = 256, 96
        dpr = max(1.0, float(self.devicePixelRatio()))
        pm = QPixmap(int(round(tw * dpr)), int(round(th * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        qp = QPainter(pm)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        ac = get_theme_accent()
        gr = getattr(ac, "r", 127)
        gg = getattr(ac, "g", 209)
        gb = getattr(ac, "b", 255)
        # a soft horizontal band of the accent glow across the top
        grad = QLinearGradient(0, 0, 0, th)
        grad.setColorAt(0.0, QColor(gr, gg, gb, 150))
        grad.setColorAt(0.5, QColor(gr, gg, gb, 60))
        grad.setColorAt(1.0, QColor(gr, gg, gb, 0))
        qp.setPen(Qt.PenStyle.NoPen)
        qp.setBrush(grad)
        qp.drawRect(0, 0, tw, th)
        qp.end()
        self._glow_tex = pm
        return pm

    def set_params(self, color: QColor, opacity: int, border: bool,
                   blur: int = 18) -> None:
        self._color = color
        self._opacity = opacity
        self._border = border
        self._blur = max(0, min(50, int(blur)))
        self.update()

    def paintEvent(self, _ev: Any) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        r = 10
        path = QPainterPath()
        path.addRoundedRect(0, 0, w, h, r, r)
        p.save()
        p.setClipPath(path)
        # 1) neutral panel — the ONLY backdrop (no second dark overlay).
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(30, 33, 40))
        p.drawRoundedRect(0, 0, w, h, r, r)
        # 2) frosted breathing glow (real accent colour, blurred by the slider)
        glow = self._ensure_glow()
        if self._glow_blurred is None or self._glow_blurred_for != self._blur:
            self._glow_blurred = blur_pixmap(glow, self._blur)
            self._glow_blurred_for = self._blur
        bg = self._glow_blurred if (self._glow_blurred is not None
                                    and not self._glow_blurred.isNull()) else glow
        if bg is not None and not bg.isNull():
            p.drawPixmap(0, 0, w, h, bg)
        # 3) the REAL tray colour @ opacity — this is what you judge.
        a = max(0, min(255, int(round(self._opacity / 100.0 * 255))))
        p.setBrush(QColor(self._color.red(), self._color.green(),
                          self._color.blue(), a))
        p.drawRoundedRect(0, 0, w, h, r, r)
        p.restore()
        # 4) optional 1px border
        if self._border:
            p.setPen(QPen(QColor(255, 255, 255, 40), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(1, 1, w - 2, h - 2, r, r)
        p.end()


class BreathingEditPanel(QWidget):
    """呼吸灯自定义页（2026-08-30 整合版，视觉顺序 = 导航顺序）。

    8 项呼吸灯参数（开关 / 周期 / 颜色 / 最大亮度 / 谷值 / 冲亮 / 停留 /
    黑罩淡出）整合为「基础」+「波形与节奏」两张分组卡片，避免逐项一张
    卡片的松散布局。顶部固定 :class:`_BreathHeadPreview` 整合预览（模拟
    HUD 窗口 + 焦点瞬态波形 + 常态呼吸波形 + 演示按钮），位于 QScrollArea
    **之前**，永远不随内容滚动。卡片区背景（tray）编辑已移到蒙版页
    ThemeMaskPanel（唯一入口），本页只管呼吸灯本身。
    """

    # 12 色色板（与演示 html 一致；含暖白/钢灰两档低饱和色）。
    _PRESETS = [
        ("冰青", "127,209,255"), ("深空蓝", "55,138,221"), ("靛蓝", "99,102,241"),
        ("紫罗兰", "167,139,250"), ("樱粉", "244,114,182"), ("玫瑰红", "244,63,94"),
        ("琥珀橙", "239,159,39"), ("翡翠绿", "74,222,128"), ("青柠", "163,230,53"),
        ("薄荷", "45,212,191"), ("暖白", "232,228,217"), ("钢灰", "148,163,184"),
    ]

    def __init__(self, app: Any, overlay: Any) -> None:
        super().__init__()
        self.setStyleSheet(
            "QLabel { background: transparent; border: none; }"
            "QPushButton { border: none; }"
            "QPushButton:hover { border: none; }"
            "QPushButton:pressed { border: none; }"
            "QPushButton:focus { border: none; }"
            "QPushButton:checked { border: none; }"
        )
        self._app = app
        self._overlay = overlay
        self._period_s = 2.4
        self._max = 1.0
        self._color_hex = ""  # "" => follow theme accent
        self._swatches: list[QFrame] = []
        self._enabled = True          # 呼吸脉动总开关（关 = 常亮峰值）
        self._floor = 0.1             # 呼吸最暗值
        self._flash_ms = 180          # 获得焦点冲到峰值时长
        self._hold_ms = 120           # 峰值停留时长
        self._scrim_ms = 200          # 失焦黑罩淡出时长
        # 卡片区背景（tray）编辑已移到蒙版页 ThemeMaskPanel，本页不再持有。

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # ---- header ----------------------------------------------------
        header = QHBoxLayout()
        header.setSpacing(8)
        back = QPushButton("← 返回")
        back.setFixedHeight(24)
        back.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #cfe6ff;"
            " border: none; border-radius: 5px;"
            " padding: 2px 10px; font-size: 11px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.16); }")
        back.clicked.connect(lambda _=False: self._overlay.open_layout())
        self._back_btn = back
        header.addWidget(back)
        title = ClickableTitle("呼吸灯自定义",
                                color=_hex(get_theme_accent()),
                                size=PAGE_TITLE_SIZE)
        title.clicked.connect(lambda _=False: self._open_faq())
        header.addWidget(title)
        header.addStretch(1)
        self._status = QLabel("")
        self._status.setStyleSheet("color: #8b97aa; font-size: 10px;")
        header.addWidget(self._status)
        root.addLayout(header)

        # ---- live preview (pinned ABOVE the scroll area) ---------------
        # 关键：addWidget 到 **root**，QScrollArea 在它之后才创建，
        # 所以预览天然不参与滚动、永远停在页面顶部。
        pv = _BreathHeadPreview(self)
        root.addWidget(pv)
        self._preview = pv

        # ---- scrollable card area --------------------------------------
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { background: rgba(255,255,255,0.05);"
            " width: 6px; border-radius: 3px; }"
            "QScrollBar::handle:vertical { background: rgba(127,209,255,0.35);"
            " border-radius: 3px; min-height: 30px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {"
            " height: 0px; }")
        scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        content = QWidget()
        content.setStyleSheet("background: transparent; border: none;")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(10)
        scroll.setWidget(content)
        root.addWidget(scroll)

        # ---- 分组卡片：8 项呼吸灯参数整合成两张（基础 / 波形与节奏）----
        base = _EditCard("基础", self)
        bf = base.content()

        self._enabled_btn = QPushButton("呼吸灯：开")
        self._enabled_btn.setCheckable(True)
        self._enabled_btn.setChecked(True)
        self._enabled_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 6px;"
            " padding: 5px 10px; font-size: 11px; }"
            "QPushButton:checked { background: rgba(127,209,255,0.28);"
            " color: #bfe8ff; border: none; }")
        self._enabled_btn.clicked.connect(self._on_enabled)
        bf.addWidget(self._enabled_btn)
        _desc_on = QLabel("关闭后呼吸灯保持常亮（不再脉动），颜色 / 间隔 / 亮度设置照常生效")
        _desc_on.setWordWrap(True)
        _desc_on.setStyleSheet("color: #aeb9c9; font-size: 10px;")
        bf.addWidget(_desc_on)

        _t_col = QLabel("呼吸灯颜色")
        _t_col.setStyleSheet("color: #dfe6f0; font-size: 11px;")
        bf.addWidget(_t_col)
        sw_wrap = QWidget()
        self._sw_row = FlowLayout(sw_wrap, margin=0, spacing=5)
        self._swatches = []
        for label, rgb in self._PRESETS:
            sw = QFrame(sw_wrap)
            sw.setFixedSize(30, 24)
            sw._hex = self._rgb_to_hex(rgb)
            sw._label = label
            sw.setStyleSheet(self._sw_style(False, sw._hex))
            sw.setCursor(Qt.CursorShape.PointingHandCursor)
            sw.setToolTip(label)
            sw.mousePressEvent = lambda _e, hx=sw._hex: self._pick_color(hx)  # type: ignore[assignment]
            self._sw_row.addWidget(sw)
            self._swatches.append(sw)
        bf.addWidget(sw_wrap)

        brow = QHBoxLayout()
        self._custom_btn = QPushButton("自定义颜色…")
        self._custom_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 6px;"
            " padding: 5px 10px; font-size: 11px; }")
        self._custom_btn.clicked.connect(self._pick_custom)
        self._follow_btn = QPushButton("跟随主题")
        self._follow_btn.setCheckable(True)
        self._follow_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 6px;"
            " padding: 5px 10px; font-size: 11px; }"
            "QPushButton:checked { background: rgba(255,170,200,0.28);"
            " color: #ffb3d1; border: none; }")
        self._follow_btn.clicked.connect(self._follow_theme)
        brow.addWidget(self._custom_btn)
        brow.addWidget(self._follow_btn)
        brow.addStretch(1)
        bf.addLayout(brow)
        _desc_color = QLabel("留空则使用主题强调色（与之前一致）")
        _desc_color.setWordWrap(True)
        _desc_color.setStyleSheet("color: #8b97aa; font-size: 10px;")
        bf.addWidget(_desc_color)
        content_layout.addWidget(base)

        # ---- 波形与节奏：6 个时间 / 亮度参数 ---------------------------
        wave = _EditCard("波形与节奏", self)
        wf = wave.content()
        self._period_slider, self._period_val = self._add_slider_row(
            wf, "呼吸时间间隔", "一个完整呼吸（最暗→最亮→最暗）的时长",
            5, 80, 24, "2.4 s", self._on_period)
        self._max_slider, self._max_val = self._add_slider_row(
            wf, "最大亮度", "峰值辉光强度（与谷值共同决定呼吸落差）",
            25, 100, 100, "100%", self._on_max)
        self._floor_slider, self._floor_val = self._add_slider_row(
            wf, "呼吸谷值（最暗）", "呼吸最暗时的亮度；越接近峰值，脉动越弱",
            0, 100, 10, "0.10", self._on_floor)
        self._flash_slider, self._flash_val = self._add_slider_row(
            wf, "冲到最亮用时", "获得焦点时，灯从熄灭升到峰值所需时间",
            60, 600, 180, "180 ms", self._on_flash)
        self._hold_slider, self._hold_val = self._add_slider_row(
            wf, "最亮处停留", "冲到最亮后保持多久再开始呼吸",
            0, 600, 120, "120 ms", self._on_hold)
        self._scrim_slider, self._scrim_val = self._add_slider_row(
            wf, "黑罩淡出用时", "获得焦点时，失焦黑罩淡出的时长（与灯冲亮同时开始）",
            0, 800, 200, "200 ms", self._on_scrim)
        content_layout.addWidget(wave)


        # ---- footer ----------------------------------------------------
        foot = QHBoxLayout()
        foot.setSpacing(6)
        self._save_btn = QPushButton("保存")
        self._cancel_btn = QPushButton("取消")
        self._save_btn.setStyleSheet(
            "QPushButton { background: #82ff1e; color: #0c0f16; border: none;"
            " border-radius: 7px; padding: 7px 18px; font-weight: bold; }"
            "QPushButton:hover { background: #9dff4a; }")
        self._cancel_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.1); color: #dfe6f0;"
            " border: none; border-radius: 7px;"
            " padding: 7px 18px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.18); }")
        self._save_btn.clicked.connect(self._save)
        self._cancel_btn.clicked.connect(self._cancel)
        foot.addStretch(1)
        foot.addWidget(self._save_btn)
        foot.addWidget(self._cancel_btn)
        root.addLayout(foot)
        # 预览动画由 _BreathHeadPreview 自带的 QTimer 驱动，不再需要
        # BreathingController（它只驱动旧的纯色渐变条）。

    # -- 疑惑解答（点击顶部「呼吸灯自定义」标题打开）----------------
    def _open_faq(self) -> None:
        faq = self._overlay.faq()
        if faq is not None:
            faq.open_for("breathing_edit", "鼠标 标题")

    # -- helpers -------------------------------------------------------
    @staticmethod
    def _rgb_to_hex(rgb: str) -> str:
        parts = [int(x) for x in rgb.split(",")]
        return f"{parts[0]:02x}{parts[1]:02x}{parts[2]:02x}"

    def _sw_style(self, selected: bool, hex_color: str) -> str:
        return (f"QFrame {{ background-color: #{hex_color}; border: none;"
                f" border-radius: 5px; }}")

    def _accent_fallback(self) -> QColor:
        win = getattr(self._app, "_window", None)
        acc = getattr(win, "_accent", None)
        if isinstance(acc, QColor):
            return acc
        return QColor(95, 175, 235)

    def _apply_preview_color(self) -> None:
        # 顶部整合预览（模拟窗口 + 两张波形）用的是同一套颜色。
        col = None
        if self._color_hex:
            try:
                col = QColor("#" + self._color_hex)
            except Exception:
                col = None
        if col is None:
            col = self._accent_fallback()
        self._preview.set_color(col)

    def _push_live(self) -> None:
        """保存前实时把当前呼吸灯设置推给真实 HUD（仅预览，不落盘）。"""
        try:
            win = getattr(self._app, "_window", None)
            if win is not None:
                win.apply_breath(self._period_s, self._max, self._color_hex,
                                 floor=self._floor,
                                 flash_ms=self._flash_ms,
                                 hold_ms=self._hold_ms,
                                 scrim_ms=self._scrim_ms)
            self._overlay._apply_window_border()
        except Exception:
            pass

    def _highlight(self, hexv: str) -> None:
        for sw in self._swatches:
            sel = (getattr(sw, "_hex", "") == hexv)
            sw.setStyleSheet(self._sw_style(sel, sw._hex))

    # -- control handlers ----------------------------------------------
    def _on_period(self, val: int) -> None:
        self._period_s = val / 10.0
        self._period_val.setText(f"{self._period_s:.1f} s")
        # 旧代码漏了这一行：改周期时预览不会跟着变（波形图始终按旧周期画）。
        self._sync_sim()

    def _on_max(self, val: int) -> None:
        self._max = val / 100.0
        self._max_val.setText(f"{val}%")
        self._sync_sim()

    def _on_floor(self, val: int) -> None:
        self._floor = val / 100.0
        self._floor_val.setText(f"{self._floor:.2f}")
        self._sync_sim()

    def _on_flash(self, val: int) -> None:
        self._flash_ms = int(val)
        self._flash_val.setText(f"{val} ms")
        self._sync_sim()

    def _on_hold(self, val: int) -> None:
        self._hold_ms = int(val)
        self._hold_val.setText(f"{val} ms")
        self._sync_sim()

    def _on_scrim(self, val: int) -> None:
        self._scrim_ms = int(val)
        self._scrim_val.setText(f"{val} ms")
        self._sync_sim()

    def _sync_sim(self) -> None:
        """把当前 6 个波形参数推给顶部整合预览（谷值/峰值/周期/冲亮/停留/黑罩）。"""
        pv = getattr(self, "_preview", None)
        if pv is None:
            return
        pv.set_tuning(self._floor, self._max, self._period_s,
                      self._flash_ms, self._hold_ms, self._scrim_ms)

    def _on_enabled(self, checked: bool) -> None:
        self._enabled = bool(checked)
        self._enabled_btn.setText("呼吸灯：" + ("开" if self._enabled else "关"))
        try:
            self._preview.set_enabled(self._enabled)
        except Exception:
            pass
        try:
            win = getattr(self._app, "_window", None)
            if win is not None:
                win.set_breath_enabled(self._enabled)
        except Exception:
            pass

    def _add_slider_row(self, layout: Any, title: str, desc: str,
                        lo: int, hi: int, val: int, text: str,
                        handler: Any) -> tuple:
        """在分组卡片**内部**追加「小标题 + 滑块 + 数值」紧凑行（不新建卡片）。"""
        t = QLabel(title)
        t.setStyleSheet("color: #dfe6f0; font-size: 11px;")
        layout.addWidget(t)
        row = QHBoxLayout()
        row.setSpacing(8)
        slider = _NoWheelSlider(Qt.Orientation.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(val)
        slider.setStyleSheet(
            "QSlider { border: none; background: transparent; }"
            "QSlider::groove:horizontal { border: none; height: 6px;"
            " background: rgba(255,255,255,0.12); border-radius: 3px; }"
            "QSlider::handle:horizontal { border: none; background: #7fd1ff;"
            " width: 12px; height: 12px; margin: -3px 0; border-radius: 6px; }"
            "QSlider::sub-page:horizontal { background: #7fd1ff; border-radius: 3px; }")
        lab = QLabel(text)
        lab.setStyleSheet("color: #7fd1ff; font-size: 11px;")
        lab.setFixedWidth(56)
        lab.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        slider.valueChanged.connect(handler)
        row.addWidget(slider, 1)
        row.addWidget(lab)
        layout.addLayout(row)
        if desc:
            d = QLabel(desc)
            d.setWordWrap(True)
            d.setStyleSheet("color: #8b97aa; font-size: 10px;")
            layout.addWidget(d)
        return slider, lab

    def _pick_color(self, hexv: str) -> None:
        self._color_hex = hexv
        self._follow_btn.setChecked(False)
        self._highlight(hexv)
        self._apply_preview_color()
        self._push_live()

    def _pick_custom(self) -> None:
        dlg = QColorDialog()
        if self._color_hex:
            try:
                dlg.setCurrentColor(QColor("#" + self._color_hex))
            except Exception:
                pass
        if dlg.exec() == QColorDialog.DialogCode.Accepted:
            c = dlg.currentColor()
            hexv = f"{c.red():02x}{c.green():02x}{c.blue():02x}"
            self._pick_color(hexv)

    def _follow_theme(self) -> None:
        if self._follow_btn.isChecked():
            self._color_hex = ""
            self._highlight("")
            self._apply_preview_color()
            self._push_live()


    # -- load / commit -------------------------------------------------
    def refresh(self) -> None:
        cfg = self._app.config_manager.config
        period = getattr(cfg, "breath_period_s", None)
        if not period or period <= 0:
            speed = getattr(cfg, "breath_speed", 50)
            period = breath_speed_to_period(int(speed)) if speed else 2.4
        maxb = getattr(cfg, "breath_max_brightness", 1.0) or 1.0
        color = getattr(cfg, "breath_color", "") or ""
        self._period_s = float(period)
        self._max = float(maxb)
        self._color_hex = color
        # breathing on/off master switch
        enabled = bool(getattr(cfg, "breath_enabled", True))
        self._enabled = enabled
        self._enabled_btn.setChecked(enabled)
        self._enabled_btn.setText("呼吸灯：" + ("开" if enabled else "关"))
        try:
            self._preview.set_enabled(enabled)
        except Exception:
            pass
        # sync controls
        self._period_slider.blockSignals(True)
        self._max_slider.blockSignals(True)
        self._period_slider.setValue(max(5, min(80, int(round(self._period_s * 10)))))
        self._max_slider.setValue(max(25, min(100, int(round(self._max * 100)))))
        self._period_slider.blockSignals(False)
        self._max_slider.blockSignals(False)
        self._period_val.setText(f"{self._period_s:.1f} s")
        self._max_val.setText(f"{int(round(self._max * 100))}%")
        # 谷值 + 获得焦点动画三项（缺字段时回落默认值）
        _fl = getattr(cfg, "breath_floor", None)
        try:
            _fl = None if _fl is None else float(_fl)
        except (TypeError, ValueError):
            _fl = None
        if _fl is not None and not (0.0 <= _fl <= 1.0):
            _fl = None
        self._floor = 0.1 if _fl is None else _fl
        self._flash_ms = int(getattr(cfg, "breath_flash_ms", 180) or 180)
        self._hold_ms = int(getattr(cfg, "breath_hold_ms", 120) or 120)
        self._scrim_ms = int(getattr(cfg, "breath_scrim_ms", 200) or 200)
        for _sl, _lb, _v, _fmt in (
                (self._floor_slider, self._floor_val,
                 int(round(self._floor * 100)), lambda v: f"{v / 100.0:.2f}"),
                (self._flash_slider, self._flash_val,
                 self._flash_ms, lambda v: f"{v} ms"),
                (self._hold_slider, self._hold_val,
                 self._hold_ms, lambda v: f"{v} ms"),
                (self._scrim_slider, self._scrim_val,
                 self._scrim_ms, lambda v: f"{v} ms")):
            _sl.blockSignals(True)
            _sl.setValue(max(_sl.minimum(), min(_sl.maximum(), _v)))
            _sl.blockSignals(False)
            _lb.setText(_fmt(_v))
        # colour UI
        self._follow_btn.setChecked(not self._color_hex)
        self._highlight(self._color_hex)
        # live preview（颜色 + 6 个波形参数一次推齐）
        self._apply_preview_color()
        self._sync_sim()
        # 卡片区背景（tray）编辑已统一移到蒙版页 ThemeMaskPanel，
        # 本页不再读写 tray_*，避免两处互相覆盖。
        self._status.setText("")

    def _save(self) -> None:
        cfg = self._app.config_manager.config
        cfg.breath_period_s = self._period_s
        cfg.breath_color = self._color_hex
        cfg.breath_max_brightness = self._max
        cfg.breath_enabled = self._enabled
        cfg.breath_floor = self._floor
        cfg.breath_flash_ms = self._flash_ms
        cfg.breath_hold_ms = self._hold_ms
        cfg.breath_scrim_ms = self._scrim_ms
        # tray_* 不在本页保存（唯一入口 = 蒙版页底板卡）
        try:
            self._app.save_all()
        except Exception:
            pass
        try:
            win = getattr(self._app, "_window", None)
            if win is not None:
                win.apply_breath(self._period_s, self._max, self._color_hex,
                                 floor=self._floor,
                                 flash_ms=self._flash_ms,
                                 hold_ms=self._hold_ms,
                                 scrim_ms=self._scrim_ms)
                win.set_breath_enabled(self._enabled)
            self._overlay._apply_window_border()
        except Exception:
            pass
        self._status.setText("已保存呼吸灯设置")
        self._overlay.open_layout()

    def _cancel(self) -> None:
        # Revert the live HUD + this window border to the *saved* config, so a
        # colour picked in preview (but not 保存) does not linger on screen.
        cfg = self._app.config_manager.config
        try:
            win = getattr(self._app, "_window", None)
            if win is not None:
                period = float(getattr(cfg, "breath_period_s", None) or 2.4)
                maxb = float(getattr(cfg, "breath_max_brightness", 1.0) or 1.0)
                color = getattr(cfg, "breath_color", "") or ""
                _fl = getattr(cfg, "breath_floor", None)
                try:
                    _fl = None if _fl is None else float(_fl)
                except (TypeError, ValueError):
                    _fl = None
                if _fl is not None and not (0.0 <= _fl <= 1.0):
                    _fl = None
                win.apply_breath(period, maxb, color,
                                 floor=(0.1 if _fl is None else _fl),
                                 flash_ms=int(getattr(cfg, "breath_flash_ms", 180) or 180),
                                 hold_ms=int(getattr(cfg, "breath_hold_ms", 120) or 120),
                                 scrim_ms=int(getattr(cfg, "breath_scrim_ms", 200) or 200))
                win.set_breath_enabled(bool(getattr(cfg, "breath_enabled", True)))
                # tray_* 恢复交由蒙版页负责，本页不再处理
            self._overlay._apply_window_border()
        except Exception:
            pass
        self._overlay.open_layout()

    # -- nav ------------------------------------------------------------
    def nav_items(self) -> list[NavItem]:
        # 2026-08-30 整合后按视觉顺序排列（视觉顺序 = 导航顺序，铁律）：
        #   预览演示按钮 → 开关 → 颜色 → 周期 → 亮度 → 谷值 → 冲亮 → 停留
        #   → 黑罩 → 卡片区背景 → 保存/取消
        items: list[NavItem] = []
        items.append(NavItem(self._back_btn, "action",
                             confirm=self._overlay.open_layout, hint="B"))
        # 冲亮/停留/黑罩只在焦点切换那一瞬间生效，常态呼吸观察不到；
        # 没这两个触发按钮，那三个滑块等于无法验证。
        for _b, _cb in zip(self._preview.nav_buttons(),
                           (self._preview.demo_gain, self._preview.demo_lose)):
            items.append(NavItem(_b, "action", confirm=_cb))
        # 呼吸灯开关：旧代码漏了注册，手柄/键盘根本选不到（掌机主用）。
        items.append(NavItem(self._enabled_btn, "toggle",
                             confirm=self._on_enabled, hint="A"))
        for sw in self._swatches:
            items.append(NavItem(sw, "action",
                                 confirm=lambda hx=sw._hex: self._pick_color(hx)))
        items.append(NavItem(self._custom_btn, "action",
                             confirm=self._pick_custom, hint="A"))
        items.append(NavItem(self._follow_btn, "action",
                             confirm=self._follow_theme))
        items.append(NavItem(self._period_slider, "slider",
                             nudge=lambda d, sl=self._period_slider: (
                                 sl.setValue(max(5, min(80, sl.value() + d))), True)[1]))
        items.append(NavItem(self._max_slider, "slider",
                             nudge=lambda d, sl=self._max_slider: (
                                 sl.setValue(max(25, min(100, sl.value() + d))), True)[1]))
        items.append(NavItem(self._floor_slider, "slider",
                             nudge=lambda d, sl=self._floor_slider: (
                                 sl.setValue(max(0, min(100, sl.value() + d))), True)[1]))
        items.append(NavItem(self._flash_slider, "slider",
                             nudge=lambda d, sl=self._flash_slider: (
                                 sl.setValue(max(60, min(600, sl.value() + d))), True)[1]))
        items.append(NavItem(self._hold_slider, "slider",
                             nudge=lambda d, sl=self._hold_slider: (
                                 sl.setValue(max(0, min(600, sl.value() + d))), True)[1]))
        items.append(NavItem(self._scrim_slider, "slider",
                             nudge=lambda d, sl=self._scrim_slider: (
                                 sl.setValue(max(0, min(800, sl.value() + d))), True)[1]))
        # 卡片区背景（tray）导航项已随卡片一起移除（唯一入口 = 蒙版页）。
        items.append(NavItem(self._save_btn, "action",
                             confirm=self._save, hint="Y"))
        items.append(NavItem(self._cancel_btn, "action",
                             confirm=self._cancel, hint="B"))
        return items




class LayoutEffectPanel(QWidget):
    """排版效果：调整主页面功能卡片的「卡片间距」与「外圈边距」。

    改动在页面内 mini 卡片堆上实时预览，保存后写入 layout.json 并即时套用
    到真实 HUD（win.apply_card_layout）。与呼吸灯自定义页同源的编辑范式。"""

    def __init__(self, app: Any, overlay: Any) -> None:
        super().__init__()
        self.setStyleSheet(
            "QLabel { background: transparent; border: none; }"
            "QPushButton { border: none; }"
            "QPushButton:hover { border: none; }"
            "QPushButton:pressed { border: none; }"
            "QPushButton:focus { border: none; }"
            "QPushButton:checked { border: none; }")
        self._app = app
        self._overlay = overlay
        self._gap = 4
        self._margin = 8

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # ---- header ----------------------------------------------------
        header = QHBoxLayout()
        header.setSpacing(8)
        back = QPushButton("← 返回")
        back.setFixedHeight(24)
        back.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #cfe6ff;"
            " border: none; border-radius: 5px;"
            " padding: 2px 10px; font-size: 11px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.16); }")
        back.clicked.connect(lambda _=False: self._overlay.open_layout())
        self._back_btn = back
        header.addWidget(back)
        title = ClickableTitle("排版效果",
                                color=_hex(get_theme_accent()),
                                size=PAGE_TITLE_SIZE)
        header.addWidget(title)
        header.addStretch(1)
        self._status = QLabel("")
        self._status.setStyleSheet("color: #8b97aa; font-size: 10px;")
        header.addWidget(self._status)
        root.addLayout(header)

        # ---- live preview ----------------------------------------------
        # 预览框用 QScrollArea(widgetResizable=False) 包裹并限制最大高度，
        # 这样无论卡片间距 / 外圈边距调到多大，预览框高度都不会无限膨胀
        # （maxHeight=220，超出时出滚动条）。注意必须用 widgetResizable=False：
        # 设为 True 会让 setSpacing 在「滚动区 widget 的根布局」上失效（与
        # 主窗口 inner_layout 同款坑），而预览的实时间距正是改 _pv_lay。
        pv_card = _EditCard("实时预览", self)
        pvc = pv_card.content()
        self._pv = QFrame()
        self._pv.setStyleSheet(
            "QFrame { background: #0e1117;"
            " border: 1px solid rgba(255,255,255,0.08);"
            " border-radius: 8px; }")
        self._pv_lay = QVBoxLayout(self._pv)
        self._pv_lay.setContentsMargins(8, 8, 8, 8)
        self._pv_lay.setSpacing(4)
        for _i in range(6):
            c = QFrame()
            c.setFixedHeight(30)
            c.setStyleSheet(
                "QFrame { background: rgba(95,175,235,0.18);"
                " border: 1px solid rgba(95,175,235,0.40);"
                " border-radius: 6px; }")
            self._pv_lay.addWidget(c)
        self._pv_scroll = QScrollArea(self)
        # 关键：垂直方向必须用 Preferred（不要用默认的 Expanding），否则
        # 父布局里 pv_card 会被撑得很大、与下面"卡片间距/外圈边距"那块
        # 滚动区瓜分剩余空间，"实时预览"上下多出一大块无用的空隙。
        # widgetResizable 仍为 False 是因为 True 会让 setSpacing 在滚动区
        # 内的根布局上失效（与主窗口 inner_layout 同款坑）。
        self._pv_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._pv_scroll.setWidgetResizable(False)
        self._pv_scroll.setFixedHeight(220)
        self._pv_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._pv_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._pv_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._pv_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { background: rgba(255,255,255,0.05);"
            " width: 6px; border-radius: 3px; }"
            "QScrollBar::handle:vertical { background: rgba(127,209,255,0.35);"
            " border-radius: 3px; min-height: 30px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {"
            " height: 0px; }")
        self._pv_scroll.setWidget(self._pv)
        pvc.addWidget(self._pv_scroll)
        root.addWidget(pv_card)

        # ---- scrollable area -------------------------------------------
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { background: rgba(255,255,255,0.05);"
            " width: 6px; border-radius: 3px; }"
            "QScrollBar::handle:vertical { background: rgba(127,209,255,0.35);"
            " border-radius: 3px; min-height: 30px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {"
            " height: 0px; }")
        scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        content = QWidget()
        content.setStyleSheet("background: transparent; border: none;")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(10)
        scroll.setWidget(content)
        root.addWidget(scroll)

        # ---- 卡片间距 --------------------------------------------------
        gcard = _EditCard("卡片间距", self)
        gcf = gcard.content()
        grow = QHBoxLayout()
        self._gap_slider = _NoWheelSlider(Qt.Orientation.Horizontal)
        self._gap_slider.setRange(0, 12)
        self._gap_slider.setValue(4)
        self._gap_slider.setStyleSheet(
            "QSlider { border: none; background: transparent; }"
            "QSlider::groove:horizontal { border: none; height: 6px;"
            " background: rgba(255,255,255,0.12); border-radius: 3px; }"
            "QSlider::handle:horizontal { border: none; background: #7fd1ff;"
            " width: 12px; height: 12px; margin: -3px 0; border-radius: 6px; }"
            "QSlider::sub-page:horizontal { background: #7fd1ff; border-radius: 3px; }")
        self._gap_val = QLabel("4 px")
        self._gap_val.setStyleSheet("color: #dfe6f0; font-size: 11px;")
        self._gap_val.setFixedWidth(46)
        self._gap_val.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._gap_slider.valueChanged.connect(self._on_gap)
        grow.addWidget(self._gap_slider, 1)
        grow.addWidget(self._gap_val)
        gcf.addLayout(grow)
        _desc_g = QLabel("上下相邻功能卡片之间的垂直间隙。越小越紧凑；"
                         "建议 ≥3px，避免选中态微光与邻卡糊在一起")
        _desc_g.setWordWrap(True)
        _desc_g.setStyleSheet("color: #aeb9c9; font-size: 10px;")
        gcf.addWidget(_desc_g)
        content_layout.addWidget(gcard)

        # ---- 外圈边距 --------------------------------------------------
        mcard = _EditCard("外圈边距", self)
        # 2026-08-30: 保留引用 —— 「主题与蒙版」页会把这张卡整体领走复用。
        self._margin_card = mcard
        mcf = mcard.content()
        mrow = QHBoxLayout()
        self._margin_slider = _NoWheelSlider(Qt.Orientation.Horizontal)
        self._margin_slider.setRange(0, 16)
        self._margin_slider.setValue(8)
        self._margin_slider.setStyleSheet(
            "QSlider { border: none; background: transparent; }"
            "QSlider::groove:horizontal { border: none; height: 6px;"
            " background: rgba(255,255,255,0.12); border-radius: 3px; }"
            "QSlider::handle:horizontal { border: none; background: #7fd1ff;"
            " width: 12px; height: 12px; margin: -3px 0; border-radius: 6px; }"
            "QSlider::sub-page:horizontal { background: #7fd1ff; border-radius: 3px; }")
        self._margin_val = QLabel("8 px")
        self._margin_val.setStyleSheet("color: #dfe6f0; font-size: 11px;")
        self._margin_val.setFixedWidth(46)
        self._margin_val.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._margin_slider.valueChanged.connect(self._on_margin)
        mrow.addWidget(self._margin_slider, 1)
        mrow.addWidget(self._margin_val)
        mcf.addLayout(mrow)
        _desc_m = QLabel("卡片区外圈留白。调小 → 更贴边（卡片区背景边会变窄）；"
                         "调大 → 卡片离窗口边缘更远")
        _desc_m.setWordWrap(True)
        _desc_m.setStyleSheet("color: #aeb9c9; font-size: 10px;")
        mcf.addWidget(_desc_m)
        content_layout.addWidget(mcard)

        content_layout.addStretch(1)

        # ---- footer ----------------------------------------------------
        # 保存(绿) / 取消(红) 样式与位置统一照「页面布局编辑」页：右侧排布、
        # 保存绿取消红，并加常驻手柄快捷键提示（进页面即见，不依赖导航激活）。
        foot = QHBoxLayout()
        foot.setSpacing(8)
        self._save_btn = HintButton("保存")
        self._save_btn.setFixedWidth(120)
        self._save_btn.setStyleSheet(
            "QPushButton { background: #82ff1e; color: #0c0f16; border: none;"
            " border-radius: 7px; padding: 7px 16px;"
            " font-weight: bold; font-size: 12px; }"
            "QPushButton:hover { background: #9bff52; }")
        self._save_btn.clicked.connect(self._save)
        self._cancel_btn = HintButton("取消")
        self._cancel_btn.setFixedWidth(120)
        self._cancel_btn.setStyleSheet(
            "QPushButton { background: rgba(230,75,90,0.22); color: #ff9b9b;"
            " border: none; border-radius: 7px;"
            " padding: 7px 16px; font-size: 12px; }"
            "QPushButton:hover { background: rgba(230,75,90,0.36); }")
        self._cancel_btn.clicked.connect(self._cancel)
        foot.addStretch(1)
        foot.addWidget(self._save_btn)
        foot.addWidget(self._cancel_btn)
        # 常驻手柄提示：保存=Y，取消=B（进页面即显示，不依赖手柄/键盘激活）。
        self._save_btn.set_gamepad_hint("Y")
        self._save_btn.set_hint_always_visible(True)
        self._cancel_btn.set_gamepad_hint("B")
        self._cancel_btn.set_hint_always_visible(True)
        root.addLayout(foot)

    # -- handlers -------------------------------------------------------
    def _on_gap(self, v: int) -> None:
        self._gap = max(0, int(v))
        self._gap_val.setText(f"{self._gap} px")
        self._apply_preview()

    def _on_margin(self, v: int) -> None:
        self._margin = max(0, int(v))
        self._margin_val.setText(f"{self._margin} px")
        self._apply_preview()

    def _apply_preview(self) -> None:
        try:
            self._pv_lay.setSpacing(self._gap)
            self._pv_lay.setContentsMargins(
                self._margin, self._margin, self._margin, self._margin)
            # widgetResizable=False 不会自动把内容撑满滚动区宽度，手动同步
            # 预览内容宽度到视口，保证 mini 卡片始终横向填满、不被压窄。
            if getattr(self, "_pv_scroll", None) is not None:
                vw = self._pv_scroll.viewport().width()
                if vw > 0:
                    self._pv.setFixedWidth(vw)
            self._pv.update()
            if getattr(self, "_pv_scroll", None) is not None:
                self._pv_scroll.update()
        except Exception:
            pass

    # -- load / commit -------------------------------------------------
    def refresh(self) -> None:
        cfg = self._app.config_manager.config
        gap = int(getattr(cfg, "card_gap", 4) or 4)
        margin = int(getattr(cfg, "card_outer_margin", 8) or 8)
        self._gap = max(0, int(gap))
        self._margin = max(0, int(margin))
        self._gap_slider.blockSignals(True)
        self._margin_slider.blockSignals(True)
        self._gap_slider.setValue(self._gap)
        self._margin_slider.setValue(self._margin)
        self._gap_slider.blockSignals(False)
        self._margin_slider.blockSignals(False)
        self._gap_val.setText(f"{self._gap} px")
        self._margin_val.setText(f"{self._margin} px")
        self._apply_preview()
        self._status.setText("")

    def _save(self) -> None:
        cfg = self._app.config_manager.config
        cfg.card_gap = max(0, int(self._gap))
        cfg.card_outer_margin = max(0, int(self._margin))
        try:
            self._app.save_all()
        except Exception:
            pass
        # 即时套用到真实 HUD。
        try:
            win = getattr(self._app, "_window", None)
            if win is not None and hasattr(win, "apply_card_layout"):
                win.apply_card_layout(self._gap, self._margin)
        except Exception:
            pass
        self._status.setText("已保存排版效果")
        self._overlay.open_layout()

    def _cancel(self) -> None:
        # 还原到已保存配置（放弃预览改动）。
        cfg = self._app.config_manager.config
        try:
            win = getattr(self._app, "_window", None)
            if win is not None and hasattr(win, "apply_card_layout"):
                gap = int(getattr(cfg, "card_gap", 4) or 4)
                margin = int(getattr(cfg, "card_outer_margin", 8) or 8)
                win.apply_card_layout(gap, margin)
        except Exception:
            pass
        self._overlay.open_layout()

    # -- nav ------------------------------------------------------------
    def nav_items(self) -> list[NavItem]:
        items: list[NavItem] = []
        items.append(NavItem(self._back_btn, "action",
                             confirm=self._overlay.open_layout, hint="B"))
        items.append(NavItem(self._gap_slider, "slider",
                             nudge=lambda d, sl=self._gap_slider: (
                                 sl.setValue(max(0, min(12, sl.value() + d))), True)[1]))
        items.append(NavItem(self._margin_slider, "slider",
                             nudge=lambda d, sl=self._margin_slider: (
                                 sl.setValue(max(0, min(16, sl.value() + d))), True)[1]))
        items.append(NavItem(self._save_btn, "action",
                             confirm=self._save, hint="Y"))
        items.append(NavItem(self._cancel_btn, "action",
                             confirm=self._cancel, hint="B"))
        return items


# --------------------------------------------------------------------------
# 主题与蒙版（2026-08-30 新增顶级子页）
# --------------------------------------------------------------------------
def _hex_qcol(hexstr: str, alpha: int = 255) -> QColor:
    """'#rrggbb' / 'rrggbb' → QColor(alpha)；解析失败回退默认底色。"""
    h = (hexstr or "0e1117").lstrip("#")
    if len(h) != 6:
        h = "0e1117"
    try:
        c = QColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        c = QColor(14, 17, 23)
    c.setAlpha(max(0, min(255, int(alpha))))
    return c


class _MaskPreviewCanvas(QWidget):
    """「主题与蒙版」mini-HUD 实时预览画布。

    左栏 = 页面布局编辑预览（简化版：工具栏 / 页分隔条 / 块卡 / 底部条），
    右栏 = nbj 主页面预览（呼吸灯 + 单列块卡）；两栏均单列多行，还原主窗真实排布。
    区域红框只标在这两栏预览上（不再画到真实 HUD 窗口，2026-08-30 决策）。
    数值不落缓存：每次 paintEvent 都从 breath / 排版效果面板**现读**内部状态，
    由 owner 的 200ms 轮询 QTimer 驱动 update —— 任何控件（滑块/色板/开关）
    改了值，预览最多 0.2s 内跟上，无需在每个回调里插桩。"""

    def __init__(self, owner: "ThemeMaskPanel") -> None:
        super().__init__(owner)
        self._owner = owner
        self.setMinimumHeight(280)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Preferred)

    def paintEvent(self, ev: Any) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        if w <= 8 or h <= 8:
            p.end()
            return
        o = self._owner
        b, e = o._breath, o._eff
        accent = _hex_qcol(getattr(b, "_color_hex", "") or "39d3c4", 255)
        rid = getattr(o, "_active_region", None)

        # 布局：顶部两栏标题 + 左右两个预览窗口（固定像素绘制，按钮不漂移）
        pad = 8
        cap_h = 16
        col_gap = 14
        col_w = (w - 2 * pad - col_gap) / 2
        self._cap(p, "页面布局编辑预览（左）", pad, 2)
        self._cap(p, "nbj 主页面预览（右）", pad + int(col_w) + col_gap, 2)

        lw = QRect(pad, pad + cap_h, int(col_w), h - pad - cap_h - pad)
        rw = QRect(pad + int(col_w) + col_gap, pad + cap_h,
                   int(col_w), h - pad - cap_h - pad)
        ltray, lcard = self._draw_window(p, lw, "editor", o, b, e, accent)
        rtray, rcard = self._draw_window(p, rw, "main", o, b, e, accent)

        # 区域标注：红框 + 编号，同时标在两栏对应区域（圆角跟随元素）
        if rid:
            self._draw_highlight(p, rid, lw, ltray, lcard,
                                 rw, rtray, rcard, o, b)
        p.end()

    @staticmethod
    def _font(px, bold=False):
        f = QFont()
        f.setPixelSize(px)
        f.setBold(bold)
        return f

    def _cap(self, p, text, x, y) -> None:
        p.setPen(QColor(150, 162, 180))
        p.setFont(self._font(10, False))
        p.drawText(QRectF(x, y, 320, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   text)

    def _draw_window(self, p, win, side, o, b, e, accent):
        R_W, R_T = 16, 14
        p.setPen(Qt.PenStyle.NoPen)
        # 窗底色跟「页面底色」设置走（默认近黑）。
        p.setBrush(_hex_qcol(getattr(o, "_page_bg_hex", "") or "0b0e13", 255))
        p.drawRoundedRect(QRectF(win), R_W, R_W)
        if side == "editor":
            p.setPen(QPen(accent, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(win), R_W, R_W)
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(accent)
            p.drawEllipse(QRectF(win.x() + 9, win.y() + 8, 7, 7))

        ix, iy = win.x() + 4, win.y() + 4
        iw = win.width() - 8
        if side == "editor":
            tb_h, dv_h, bb_h = 22, 14, 30
            self._editor_top(p, QRect(ix, iy, iw, tb_h), accent)
            self._divider(p, QRect(ix, iy + tb_h, iw, dv_h), "第 1 页 - 4 张")
            cy0 = iy + tb_h + dv_h + 4
            bb_y = win.y() + win.height() - bb_h - 2
            self._editor_bottom(p, QRect(ix, bb_y, iw, bb_h), accent)
            # 2026-08-30 修复遮挡：tray 底边让出底部按钮栏（旧值 win 底-8，
            # tray/卡片比按钮栏多伸约 24px，最后几张卡直接叠在按钮上）。
            avail_b = bb_y - 4
        else:
            tb_h = 16
            self._main_top(p, QRect(ix, iy, iw, tb_h), accent)
            cy0 = iy + tb_h + 4
            bb_h = 0
            avail_b = win.y() + win.height() - 8
        tray = QRect(int(ix + 6), int(cy0 - 3), int(iw - 12),
                   int((avail_b - (cy0 - 3)) - 6))
        self._draw_tray(p, tray, o, b, R_T)
        if side == "editor" and getattr(o, "dim_preview_on",
                                        lambda: False)():
            a = max(0, min(255, int(getattr(b, "_editor_dim_alpha", 118))))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, a))
            p.drawRoundedRect(QRectF(tray), R_T, R_T)
        names = (["Power Settings", "锁帧控制", "进程监控", "LS 预设"]
                 if side == "editor" else
                 ["进程冻结", "自动锁帧", "电源计划", "性能监控", "磁贴"])
        n = len(names)
        gap = 4
        pad_t = 8
        card_h = (tray.height() - 2 * pad_t - gap * (n - 1)) / n
        cards = []
        for i, nm in enumerate(names):
            cq = QRect(int(tray.x() + 8),
                       int(tray.y() + pad_t + i * (card_h + gap)),
                       int(tray.width() - 16), int(card_h))
            cards.append(cq)
            self._draw_card(p, cq, nm, o, b, R_T, i == 0)
        return tray, (cards[0] if cards else tray)

    def _editor_top(self, p, r, accent):
        # 2026-08-30 修复错位：芯片改「从右往左按实测文本宽」排布，并给左侧
        # 标题留保护区（旧实现固定 x+64 与 70px 宽标题文本重叠）。
        p.setPen(accent)
        p.setFont(self._font(10, True))
        p.drawText(QRectF(r.x() + 6, r.y(), 70, r.height()),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "布局编辑")
        chips = [("↻ 重启", QColor(230, 120, 40), QColor(235, 240, 248)),
                 ("贴右+", QColor(120, 90, 200), QColor(235, 240, 248)),
                 ("磁贴+", QColor(120, 90, 200), QColor(235, 240, 248)),
                 ("分页+", QColor(120, 90, 200), QColor(235, 240, 248))]
        x = r.right() - 4
        p.setFont(self._font(9, False))
        fm = p.fontMetrics()
        guard = r.x() + 78  # 标题「布局编辑」保护区右界
        for text, fill, tcol in chips:
            cw = fm.horizontalAdvance(text) + 12
            x -= cw
            if x < guard:
                break
            self._chip(p, QRect(int(x), r.y() + 4, cw, r.height() - 8),
                       text, fill, text_col=tcol)
            x -= 4

    def _divider(self, p, r, text):
        p.setPen(QColor(70, 80, 96))
        p.drawLine(r.x() + 6, r.y() + r.height() // 2,
                   r.right() - 6, r.y() + r.height() // 2)
        p.setPen(QColor(120, 132, 150))
        p.setFont(self._font(9, False))
        p.drawText(QRectF(r.x(), r.y(), r.width(), r.height()),
                   Qt.AlignmentFlag.AlignCenter, text)

    def _editor_bottom(self, p, r, accent):
        # 2026-08-30 修复错位：同样改实测宽度；左右两组相向排布，中间留缝，
        # 放不下就整组跳过（绝不重叠）。
        ch = r.height() - 10
        y = r.y() + 5
        p.setFont(self._font(9, False))
        fm = p.fontMetrics()
        # 右组：Y)保存 / B)取消（从右往左）
        xr = r.right() - 4
        for text, fill, tcol in (
                ("Y)保存", QColor(90, 200, 60), QColor(10, 16, 10)),
                ("B)取消", QColor(200, 80, 90), QColor(255, 220, 220))):
            cw = fm.horizontalAdvance(text) + 12
            xr -= cw
            self._chip(p, QRect(int(xr), y, cw, ch), text, fill, text_col=tcol)
            xr -= 4
        # 左组：鼠标提示芯片（从左往右，不超过右组左界 - 8）
        p.setPen(QColor(150, 162, 180))
        p.drawText(QRectF(r.x() + 4, r.y(), 34, r.height()),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "鼠标")
        xl = r.x() + 40
        limit = xr - 8
        for text, fill in (("左键双击·编辑项", QColor(60, 160, 90)),
                           ("右键双击·隐藏", QColor(70, 150, 180))):
            cw = fm.horizontalAdvance(text) + 12
            if xl + cw > limit:
                break
            self._chip(p, QRect(int(xl), y, cw, ch), text, fill)
            xl += cw + 4

    def _main_top(self, p, r, accent):
        p.setPen(accent)
        p.setFont(self._font(10, True))
        p.drawText(QRectF(r.x() + 18, r.y(), r.width() - 20, r.height()),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "凝冰结魄")
        for i in range(3):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(120, 200, 255) if i == 0
                       else QColor(80, 90, 104))
            p.drawEllipse(QRectF(r.right() - 6 - (3 - i) * 10,
                                 r.y() + r.height() // 2 - 3, 6, 6))

    def _chip(self, p, r, text, fill, text_col=QColor(235, 240, 248)):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawRoundedRect(QRectF(r), 5, 5)
        p.setPen(text_col)
        p.setFont(self._font(9, False))
        p.drawText(QRectF(r), Qt.AlignmentFlag.AlignCenter, text)

    def _draw_tray(self, p, tray, o, b, R_T):
        grad = QLinearGradient(tray.x(), tray.y(), tray.right(), tray.bottom())
        grad.setColorAt(0.0, QColor(40, 60, 120))
        grad.setColorAt(0.5, QColor(90, 50, 110))
        grad.setColorAt(1.0, QColor(30, 90, 120))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(grad)
        p.drawRoundedRect(QRectF(tray), R_T, R_T)
        if getattr(b, "_card_bg_enabled", False):
            pm = o.bg_pixmap()
            if str(getattr(b, "_card_bg_mode", "solid")) == "image"                     and pm is not None and not pm.isNull():
                # 2026-08-30 修复①③⑥：
                # ③ 溢出 —— 图先裁进 tray 圆角再画（KeepAspectRatioByExpanding
                #    的图比 tray 大，旧实现没有 clip，铺到圆角外）；
                #    蒙版 —— 画完图后把「图片蒙版色」按 40% 叠上去（旧实现蒙版色
                #    只在 solid 分支用到，image 分支等于没有蒙版）；
                # ⑥ 性能 —— scaled(Smooth) 每帧执行，4K 图 50-200ms/帧；改为按
                #    (路径, 宽, 高) 缓存，只有变化时重缩放一次，每帧只贴图。
                key = (str(getattr(b, "_card_bg_image", "") or ""),
                       tray.width(), tray.height())
                if getattr(self, "_pv_scaled_key", None) != key:
                    self._pv_scaled = pm.scaled(
                        tray.width(), tray.height(),
                        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation)
                    self._pv_scaled_key = key
                sc = self._pv_scaled
                p.save()
                clip = QPainterPath()
                clip.addRoundedRect(QRectF(tray), R_T, R_T)
                p.setClipPath(clip)
                p.drawPixmap(tray.x() + (tray.width() - sc.width()) // 2,
                             tray.y() + (tray.height() - sc.height()) // 2, sc)
                # 图片蒙版色（40% 不透明度），把亮图压暗保证卡片文字可读
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_hex_qcol(getattr(b, "_card_bg_color_hex", ""),
                                     102))
                p.drawRoundedRect(QRectF(tray), R_T, R_T)
                p.restore()
            else:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_hex_qcol(getattr(b, "_card_bg_color_hex", ""), 255))
                p.drawRoundedRect(QRectF(tray), R_T, R_T)
        op = int(getattr(b, "_tray_opacity", 86))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_hex_qcol(getattr(b, "_tray_color_hex", ""),
                             255 * op // 100))
        p.drawRoundedRect(QRectF(tray), R_T, R_T)
        blur = int(getattr(b, "_tray_blur", 18))
        frost = max(0, min(120, blur * 3))
        if frost > 0:
            p.setBrush(QColor(220, 230, 245, frost))
            p.drawRoundedRect(QRectF(tray), R_T, R_T)
        # 外框线宽度跟设置走（0=关）；旧内存只有布尔时派生 2/0。
        _bw = int(getattr(o, "_tray_border_width",
                          2 if getattr(b, "_tray_border", True) else 0) or 0)
        if _bw > 0:
            p.setPen(QPen(_hex_qcol(getattr(b, "_color_hex", "") or "39d3c4",
                                    200), max(1, _bw)))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(tray), R_T, R_T)

    def _draw_card(self, p, cq, name, o, b, R_T, first):
        fill = _hex_qcol(getattr(b, "_card_fill_hex", ""),
                         int(getattr(b, "_card_fill_alpha", 165)))
        p.setPen(QPen(QColor(44, 56, 80), 1))
        p.setBrush(fill)
        p.drawRoundedRect(QRectF(cq), R_T, R_T)
        p.setPen(QColor(200, 210, 224))
        p.setFont(self._font(9, True))
        p.drawText(QRectF(cq.x() + 8, cq.y(), cq.width() - 16, cq.height()),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   name)
        if first:
            p.setPen(QPen(QColor(240, 200, 90), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(cq.x() + 1, cq.y() + 1,
                                     cq.width() - 2, cq.height() - 2), R_T, R_T)

    def _draw_highlight(self, p, rid, lw, ltray, lcard, rw, rtray, rcard,
                        o, b):
        red = QColor(255, 70, 70)
        num = {"tint": "①", "base": "②", "card": "③",
               "dim": "④", "ring": "⑤"}
        label = {"tint": "底板色", "base": "替换底色", "card": "单卡",
                 "dim": "蒙版", "ring": "留白"}
        if rid == "dim":
            a = max(0, min(255, int(getattr(b, "_editor_dim_alpha", 118))))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, a))
            p.drawRoundedRect(QRectF(ltray), 14, 14)
        if rid in ("tint", "base", "dim"):
            targets = [(lw, ltray), (rw, rtray)]
        elif rid == "card":
            targets = [(lw, lcard), (rw, rcard)]
        else:
            targets = [(lw, lw), (rw, rw)]
        for win, rect in targets:
            p.setPen(QPen(red, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(rect), 14 if rid != "ring" else 16,
                              14 if rid != "ring" else 16)
        bx = lw.x() + 6
        by = (ltray.y() + 6) if rid != "ring" else (lw.y() + 6)
        txt = f"{num.get(rid, '')} {label.get(rid, '')}"
        p.setFont(self._font(11, True))
        bw = max(54, p.fontMetrics().horizontalAdvance(txt) + 14)
        p.setPen(QPen(red, 2))
        p.setBrush(QColor(20, 0, 0, 220))
        p.drawRoundedRect(QRectF(bx, by, bw, 20), 6, 6)
        p.setPen(Qt.GlobalColor.white)
        p.drawText(QRectF(bx, by, bw, 20),
                   Qt.AlignmentFlag.AlignCenter, txt)

# --- ThemeMaskPanel 自建卡用到的轻量样式助手 -------------------------------
def _tm_btn_style() -> str:
    return ("QPushButton { background: rgba(127,209,255,0.15); color: #7fd1ff;"
            " border: none; border-radius: 6px; padding: 5px 12px; font-size: 11px; }"
            "QPushButton:hover { background: rgba(127,209,255,0.28); }")


def _tm_swatch_style(hexv: str) -> str:
    h = (hexv or "0e1117").lstrip("#")
    if not h:
        h = "0e1117"
    return f"QFrame {{ background-color: #{h}; border: none; border-radius: 5px; }}"


def _tm_chk_style() -> str:
    return ("QCheckBox { color: #dfe6f0; font-size: 12px; background: transparent;"
            " border: none; }"
            "QCheckBox::indicator { width: 15px; height: 15px; }")


# --- 2026-08-30 主题与蒙版 v2：紧凑控件助手 --------------------------------
# 滑条统一 QSS（更紧凑：5px 槽 / 11px 手柄），全部配 _NoWheelSlider 使用
# （悬停滚轮不改值、事件冒泡给外层滚动区滚页面）。
_TM_SLIDER_QSS = (
    "QSlider { border: none; background: transparent; }"
    "QSlider::groove:horizontal { border: none; height: 5px;"
    " background: rgba(255,255,255,0.12); border-radius: 2px; }"
    "QSlider::handle:horizontal { border: none; background: #7fd1ff;"
    " width: 11px; height: 11px; margin: -3px 0; border-radius: 5px; }"
    "QSlider::sub-page:horizontal { background: #7fd1ff; border-radius: 2px; }")

# 预设色板（丰富多彩版，全部为 HUD 低亮度安全色系）。
_TM_PRESETS_PAGE = ["000000", "050607", "0c1016", "120c16",
                    "0a1510", "15100a", "160a0a", "10101a"]
_TM_PRESETS_TRAY = ["0b0d10", "0e1117", "161a22", "1a2332",
                    "162032", "2d1b1b", "1b2d24", "231b2d",
                    "2e1f14", "143029", "1e1b2e", "291a1a"]
_TM_PRESETS_MASK = ["050607", "0c1016", "1a0f0f",
                    "0f1a12", "151020", "0d0d1a"]
_TM_PRESETS_FILL = ["0b0d10", "181b21", "252a34", "1e293b",
                    "162032", "2e1b1b", "1b2e22", "261b2e",
                    "2e2a1b", "1b2a2e", "2e1b2a", "1b1b2e"]


class _TMSwitch(QCheckBox):
    """胶囊开关（替代勾选框）。整控件自绘：轨道 + 圆形滑钮 + 右侧文字。

    选中/未选中只影响颜色与滑钮位置；勾选语义与 QCheckBox 完全一致，
    所以手柄导航（"toggle" 类型 confirm=toggle）无需任何适配。
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._track_w, self._track_h = 34, 18

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        s = super().sizeHint()
        return QSize(s.width() + self._track_w + 6,
                     max(s.height(), self._track_h + 2))

    def paintEvent(self, ev: Any) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        tw, th = self._track_w, self._track_h
        ty = max(0, (self.height() - th) // 2)
        on = self.isChecked()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(57, 211, 196, 235) if on else QColor(55, 65, 81, 255))
        p.drawRoundedRect(QRectF(0, ty, tw, th), th / 2.0, th / 2.0)
        kx = (tw - th + 2.0) if on else 2.0
        p.setBrush(QColor(255, 255, 255, 255))
        p.drawEllipse(QRectF(kx, ty + 2.0, th - 4.0, th - 4.0))
        if self.text():
            p.setPen(QColor(223, 230, 240) if on else QColor(147, 154, 174))
            f = QFont()
            f.setPixelSize(12)
            p.setFont(f)
            p.drawText(QRectF(tw + 8, 0, self.width() - tw - 8, self.height()),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       self.text())
        p.end()


class _TransparentDot(QFrame):
    """「透明/无」预设点：画棋盘底 + 一道红色斜线便于辨认，整体裁成圆形。

    纯 QSS 画不出斜线，故在 paintEvent 里自绘；圆形用 ``setMask`` 椭圆裁剪
    （比 paintEvent 内 ``setClipPath`` 更稳，offscreen 下 grab 也不崩）。
    选中态用 ``set_selected`` 切换（不走 setStyleSheet，避免覆盖自绘背景）。
    """

    def __init__(self, selected: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._selected = bool(selected)
        # 圆形裁剪：mask 让方角透明，与 QSS border-radius 的其余圆点一致。
        self.setMask(QRegion(
            self.rect().adjusted(1, 1, -1, -1),
            QRegion.RegionType.Ellipse))

    def set_selected(self, v: bool) -> None:
        if self._selected != v:
            self._selected = bool(v)
            self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        r = self.rect()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 棋盘占位底（区分于「跟随主题」灰斜纹）；受 mask 裁剪为圆形。
        cell = 4
        for y in range(0, r.height(), cell):
            for x in range(0, r.width(), cell):
                dark = ((x // cell) + (y // cell)) % 2 == 0
                p.fillRect(x, y, cell, cell,
                           QColor("#2a2f3a") if dark else QColor("#161a22"))
        # 中间一道红色斜线（禁止/无 语义）。
        p.setPen(QPen(QColor("#ff5a5a"), 2))
        p.drawLine(r.topLeft(), r.bottomRight())
        # 外框（圆）：选中高亮 / 未选灰边（mask 已裁成圆形）。
        if self._selected:
            p.setPen(QPen(QColor("#e8ecf1"), 2))
        else:
            p.setPen(QPen(QColor("#7a8696"), 1))
        p.drawRect(r.adjusted(1, 1, -1, -1))


class _TMPresetRow(QWidget):
    """当前色块 + 横向滚动预设点 + 「自定义」按钮（单行，绝不撑宽窗口）。

    ``on_pick(token)`` 回调：点预设 / 自定义取色后调用。token 为：
      - 具体色：6 位 hex（不带 #）
      - ``allow_empty`` 的空点：''（语义由 ``empty_label`` 决定，如边框=跟随主题色）
      - ``transparent_value`` 非 None 时额外给一颗「透明」点：回调该值
        （如边框 'none' / 背景 ''），点样式为棋盘底+红色斜线
    """

    def __init__(self, presets: list[str], cur: str,
                 on_pick: Callable[[str], None],
        allow_empty: bool = False,
        empty_label: str = "",
        transparent_value: str | None = None,
        transparent_label: str = "",
        parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_pick = on_pick
        # (dot, token, kind) —— kind: 'normal' | 'empty' | 'transparent'
        self._dots: list[tuple[QFrame, str, str]] = []
        self._empty_dot: QFrame | None = None
        self._transparent_dot: QFrame | None = None

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self._sw = QFrame()
        self._sw.setFixedSize(18, 18)
        self._sw.setStyleSheet(_tm_swatch_style(cur or "0e1117"))
        row.addWidget(self._sw)

        # 预设区横向滚动：窗口再窄也不撑宽，多余预设点滚动可见。
        sa = QScrollArea()
        sa.setFixedHeight(22)
        sa.setWidgetResizable(True)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        sa.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sa.setFrameShape(QFrame.Shape.NoFrame)
        sa.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        inner = QWidget()
        drow = QHBoxLayout(inner)
        drow.setContentsMargins(0, 0, 0, 0)
        drow.setSpacing(4)
        if allow_empty:
            self._empty_dot = self._make_dot("", cur == "", "empty")
            self._empty_dot.setToolTip(empty_label)
            drow.addWidget(self._empty_dot)
        if transparent_value is not None:
            self._transparent_dot = self._make_dot(
                transparent_value, cur == transparent_value, "transparent")
            self._transparent_dot.setToolTip(transparent_label)
            drow.addWidget(self._transparent_dot)
        for hx in presets:
            drow.addWidget(self._make_dot(hx, cur == hx, "normal"))
        drow.addStretch(1)
        sa.setWidget(inner)
        row.addWidget(sa, 1)

        custom = QPushButton("自定义")
        custom.setStyleSheet(_tm_btn_style())
        custom.setFixedHeight(22)
        custom.clicked.connect(self._pick_custom)
        row.addWidget(custom)
        self._custom_btn = custom
        self.set_current(cur)

    @staticmethod
    def _dot_style(token: str, selected: bool, kind: str) -> str:
        if kind == "transparent":
            # 「透明/无」：斜纹棋盘占位，区别于「跟随主题」斜纹。
            bg = ("background: repeating-linear-gradient(45deg,"
                  " #2a2f3a 0 4px, #161a22 4px 8px);")
            edge = "#7a8696"
        elif kind == "empty":
            # 「跟随主题」：灰阶斜纹占位。
            bg = ("background-color: qlineargradient(x1:0,y1:0,x2:1,y2:1,"
                  " stop:0 #3a3f4a, stop:0.5 #232834, stop:1 #3a3f4a);")
            edge = "#5a6b7a"
        else:
            bg = f"background-color: #{token};"
            edge = "transparent"
        return (f"QFrame {{ {bg} border: 2px solid"
                f" {'#e8ecf1' if selected else edge};"
                f" border-radius: 8px; }}")

    def _make_dot(self, token: str, selected: bool, kind: str) -> QFrame:
        if kind == "transparent":
            d = _TransparentDot(selected)
        else:
            d = QFrame()
            d.setFixedSize(16, 16)
            d.setCursor(Qt.CursorShape.PointingHandCursor)
            d.setStyleSheet(self._dot_style(token, selected, kind))
        d.mousePressEvent = (  # type: ignore[assignment]
            lambda _e, h=token: self._choose(h))
        self._dots.append((d, token, kind))
        return d

    def _choose(self, token: str) -> None:
        self.set_current(token)
        self._on_pick(token)

    def _pick_custom(self) -> None:
        dlg = QColorDialog()
        try:
            start = QColor("#" + self._cur_hex)
            if start.isValid():
                dlg.setCurrentColor(start)
        except Exception:
            pass
        if dlg.exec() == QColorDialog.DialogCode.Accepted:
            c = dlg.currentColor()
            self._choose(f"{c.red():02x}{c.green():02x}{c.blue():02x}")

    def set_current(self, hexv: str) -> None:
        self._cur_hex = str(hexv or "")
        self._sw.setStyleSheet(_tm_swatch_style(self._cur_hex or "0e1117"))
        for d, token, kind in self._dots:
            if kind == "transparent":
                # 自绘点：用 set_selected 切换选中，setStyleSheet 会覆盖自绘。
                if hasattr(d, "set_selected"):
                    d.set_selected(token == self._cur_hex)  # type: ignore[attr-defined]
            else:
                d.setStyleSheet(self._dot_style(token, token == self._cur_hex, kind))

    def custom_button(self) -> QPushButton:
        """供手柄导航把「自定义取色」挂进 nav_items。"""
        return self._custom_btn


class _PathLabel(QLabel):
    """响应式路径标签：宽度随行内剩余空间收缩，resize 时中段省略重绘。

    修复：原 ``setFixedWidth(210)`` + 两按钮的最小宽度在窄 HUD 窗口（360px 级）
    下超过内容宽；而滚动区水平滚动条关闭，子控件溢出窗口右外侧被裁切/不可点。
    这里用 ``Ignored`` 尺寸策略让标签吃剩余空间（按钮占自然宽），并在
    ``resizeEvent`` 里按实际宽度重做中段省略，单行永不撑宽窗口。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = ""
        self.setStyleSheet("color: #aeb9c9; font-size: 10px;")
        self.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(20)

    def set_path(self, full: str) -> None:
        self._full = full or "（未选择图片）"
        self._elide()

    def _elide(self) -> None:
        if self.width() <= 0:
            # 首次布局尚未确定宽度：先放原文，resizeEvent 会补一次省略。
            self.setText(self._full)
            return
        fm = self.fontMetrics()
        self.setText(fm.elidedText(
            self._full, Qt.TextElideMode.ElideMiddle, self.width()))

    def resizeEvent(self, ev: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(ev)
        self._elide()


class ThemeMaskPanel(QWidget):
    """「主题与蒙版」——把散落在 LayoutEditPanel（卡片区背景 / 替换底色 / 单卡填充 /
    编辑蒙版）与 LayoutEffectPanel（外圈留白）里的 5 块页面外观设置统一收进一个
    顶级子页。

    实现方式（2026-08-30 修订 · 方案 B）：本页**自建** 5 张设置卡，每张卡的控件直接
    调 ``core.style`` 里的 ``apply_*`` 纯函数——同一函数同时写 live config + 推给真实
    HUD，所以原 LayoutEditPanel / LayoutEffectPanel 改同一项也能即时同步（共享
    apply 函数 = 单一真值源）。本页不 reparent 任何旧卡、不动旧面板的 UI 拓扑。

    同步策略：本页的初始值**从 ``self._breath`` / ``self._eff`` 现读**（它们就是这 5
    项的当前内存态，无论是刚从 config 加载还是用户在旧面板改过）。每次改值除了调
    ``apply_*``（写 config + 推 HUD），**还把新值镜像回 ``self._breath`` / ``self._eff``
    的内存**，保证：① mini-HUD 预览（读 breath 内存）实时反映；② 旧面板 ``_save`` 不会
    把旧值盖回 config；③ 两边永远不会互相覆盖。

    区域标注：每个设置卡悬停 / 手柄聚焦时，在面板内 mini-HUD 预览上红框标出正在改
    的区域（tint/base/card/dim/ring 五个 id，由 ``_MaskPreviewCanvas._draw_highlight``
    绘制）。红框**只画在 mini-HUD 预览里**，绝不在真实 NBJ 窗口上绘制。
    """

    def __init__(self, app: Any, overlay: Any, breath: Any, eff: Any) -> None:
        super().__init__()
        self._app = app
        self._overlay = overlay
        self._breath = breath          # 供 _MaskPreviewCanvas 现读内存态
        self._eff = eff
        self._active_region: Optional[str] = None

        # 初始值：从源面板现读（单一真值源）。
        # 2026-08-30 v2：本页扩为 6 张设置卡（新增 页面底色；底板卡并入
        # 托盘颜色/透明度/模糊/外框线宽度/外圈留白/自定义背景）。托盘四件套
        # 同样从 breath 现读（呼吸灯自定义页改过也能跟上），缺省回落 cfg。
        _cfgv = getattr(app.config_manager, "config", None)
        # 单一真值源 = config：config 已经在每次 apply_* 实时写入，且落盘。
        # 旧实现从 breath._card_bg_* 读，但 breathing 面板从未在启动后把这些
        # 字段从 config 加载进内存 → 永远 False → 重启后开关误显示「关闭」。
        # 这里改读 config，并镜像回 breath 让 mini-HUD 预览也正确。
        self._card_bg_enabled = bool(getattr(_cfgv, "card_bg_enabled", False))
        self._card_bg_mode = str(getattr(_cfgv, "card_bg_mode", "solid") or "solid")
        self._card_bg_color_hex = str(getattr(_cfgv, "card_bg_color", "") or "")
        self._card_bg_image = str(getattr(_cfgv, "card_bg_image", "") or "")
        _b = breath
        _b._card_bg_enabled = self._card_bg_enabled
        _b._card_bg_mode = self._card_bg_mode
        _b._card_bg_color_hex = self._card_bg_color_hex
        _b._card_bg_image = self._card_bg_image
        self._card_fill_hex = str(getattr(breath, "_card_fill_hex", "") or "")
        self._card_fill_alpha = int(getattr(breath, "_card_fill_alpha", 165) or 165)
        self._editor_dim_alpha = int(getattr(breath, "_editor_dim_alpha", 118) or 118)
        self._margin = int(getattr(eff, "_margin", 8) or 8)
        # 底板托盘四件套（颜色/透明度%/外框线宽度/模糊）
        self._tray_color_hex = str(
            getattr(breath, "_tray_color_hex", "")
            or getattr(_cfgv, "tray_color", "") or "0e1117")
        _t_op = getattr(breath, "_tray_opacity", None)
        if _t_op is None:
            _t_op = getattr(_cfgv, "tray_opacity", 86)
        self._tray_opacity = max(0, min(100, int(_t_op)))
        _t_bw = getattr(_cfgv, "tray_border_width", None)
        if _t_bw is None:
            _t_bw = 2 if bool(getattr(breath, "_tray_border",
                                      getattr(_cfgv, "tray_border", True))) else 0
        self._tray_border_width = max(0, min(4, int(_t_bw)))
        _t_bl = getattr(breath, "_tray_blur", None)
        if _t_bl is None:
            _t_bl = getattr(_cfgv, "tray_blur", 18)
        self._tray_blur = max(0, min(50, int(_t_bl)))
        # 页面底色（空 = 跟随主题）
        self._page_bg_hex = str(getattr(_cfgv, "page_bg_color", "") or "")

        # 取消快照（_cancel 还原用）
        self._snap = self._collect()

        self.setStyleSheet(
            "QLabel { background: transparent; border: none; }"
            "QPushButton { border: none; }"
            "QPushButton:hover { border: none; }"
            "QPushButton:pressed { border: none; }"
            "QPushButton:focus { border: none; }"
            "QPushButton:checked { border: none; }"
            "QCheckBox { color: #dfe6f0; }")

        # 2026-08-30 v2：整体收紧（margins 8→6 / 间距 8→6 / 卡内行高紧凑）。
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # ---- header ----------------------------------------------------
        header = QHBoxLayout()
        header.setSpacing(8)
        back = QPushButton("← 返回")
        back.setFixedHeight(24)
        back.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #cfe6ff;"
            " border: none; border-radius: 5px; padding: 2px 10px; font-size: 11px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.16); }")
        back.clicked.connect(lambda _=False: self._overlay.open_layout())
        self._back_btn = back
        header.addWidget(back)
        title = ClickableTitle("主题与蒙版",
                               color=_hex(get_theme_accent()),
                               size=PAGE_TITLE_SIZE)
        header.addWidget(title)
        header.addStretch(1)
        self._status = QLabel("")
        self._status.setStyleSheet("color: #8b97aa; font-size: 10px;")
        header.addWidget(self._status)
        root.addLayout(header)

        # ---- mini-HUD 实时预览 ------------------------------------------
        pv_card = _EditCard("mini-HUD 实时预览", self)
        self._preview = _MaskPreviewCanvas(self)
        self._preview.setMinimumHeight(240)
        pv_card.content().addWidget(self._preview)
        _desc_pv = QLabel(
            "悬停 / 聚焦设置卡，预览红框标出对应区域；改值 0.2s 内实时跟上。")
        _desc_pv.setWordWrap(True)
        _desc_pv.setStyleSheet("color: #aeb9c9; font-size: 10px;")
        pv_card.content().addWidget(_desc_pv)
        root.addWidget(pv_card)

        # ---- 分区滚动区 --------------------------------------------------
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { background: rgba(255,255,255,0.05); width: 6px;"
            " border-radius: 3px; }"
            "QScrollBar::handle:vertical { background: rgba(127,209,255,0.35);"
            " border-radius: 3px; min-height: 30px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }")
        content = QWidget()
        content.setStyleSheet("background: transparent; border: none;")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(6)
        scroll.setWidget(content)
        root.addWidget(scroll)

        def _section(text: str) -> None:
            lbl = QLabel(text)
            lbl.setStyleSheet(
                "color: #39d3c4; font-weight: bold; font-size: 11px;"
                " background: transparent; border: none;")
            content_layout.addWidget(lbl)

        # ===== 页面底色（2026-08-30 新增；空 = 跟随主题）=====
        _section("页面底色（整个 HUD 窗口的背景）")
        self._page_bg_card = self._build_page_bg_card()
        content_layout.addWidget(self._page_bg_card)

        # ===== 底板：托盘颜色/透明度/模糊/外框线宽度/外圈留白/自定义背景 =====
        _section("底板（卡片区背景）")
        self._tray_card = self._build_tray_card()
        content_layout.addWidget(self._tray_card)

        # ===== 单卡 =====
        _section("单卡（每张卡自身的填充）")
        self._card_fill_card = self._build_card_fill_card()
        content_layout.addWidget(self._card_fill_card)

        # ===== 编辑蒙版（深度 + 预览开关收进同一张卡）=====
        _section("编辑蒙版（编辑态压暗，只在编辑态出现）")
        self._editor_dim_card = self._build_editor_dim_card()
        content_layout.addWidget(self._editor_dim_card)

        content_layout.addStretch(1)

        # ---- footer ----------------------------------------------------
        foot = QHBoxLayout()
        foot.setSpacing(8)
        self._save_btn = HintButton("保存")
        self._save_btn.setFixedWidth(120)
        self._save_btn.setStyleSheet(
            "QPushButton { background: #82ff1e; color: #0c0f16; border: none;"
            " border-radius: 7px; padding: 7px 16px; font-weight: bold; font-size: 12px; }"
            "QPushButton:hover { background: #9bff52; }")
        self._save_btn.clicked.connect(self._save)
        self._cancel_btn = HintButton("取消")
        self._cancel_btn.setFixedWidth(120)
        self._cancel_btn.setStyleSheet(
            "QPushButton { background: rgba(230,75,90,0.22); color: #ff9b9b;"
            " border: none; border-radius: 7px; padding: 7px 16px; font-size: 12px; }"
            "QPushButton:hover { background: rgba(230,75,90,0.36); }")
        self._cancel_btn.clicked.connect(self._cancel)
        foot.addStretch(1)
        foot.addWidget(self._save_btn)
        foot.addWidget(self._cancel_btn)
        self._save_btn.set_gamepad_hint("Y")
        self._save_btn.set_hint_always_visible(True)
        self._cancel_btn.set_gamepad_hint("B")
        self._cancel_btn.set_hint_always_visible(True)
        root.addLayout(foot)

        # ---- 区域标注：设置卡 → 区域 id（底板卡对应 tint，含外圈留白）----
        self._card_regions: dict[int, str] = {}
        for card, rid in (
            (self._page_bg_card, None),
            (self._tray_card, "tint"),
            (self._card_fill_card, "card"),
            (self._editor_dim_card, "dim"),
        ):
            if rid is None:
                continue
            self._card_regions[id(card)] = rid
            card.installEventFilter(self)

        # ---- 预览轮询（仅页面可见时跑）--------------------------------
        self._poll = QTimer(self)
        self._poll.setInterval(200)
        self._poll.timeout.connect(self._preview.update)

    # -- 各设置卡构造 -------------------------------------------------------
    def _row_widget(self, layout: Any) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w

    def _tm_slider_row(self, label_text: str, lo: int, hi: int, val: int,
                       cb: Callable[[int], None],
                       suffix: str = "") -> tuple[QWidget, Any, QLabel]:
        """紧凑滑条行：`标签 | _NoWheelSlider | 值`。滚轮不改值（冒泡滚页面）。"""
        row = QHBoxLayout()
        row.setSpacing(6)
        lbl = QLabel(label_text)
        lbl.setStyleSheet("color: #aeb9c9; font-size: 11px;")
        lbl.setFixedWidth(66)
        row.addWidget(lbl)
        sl = _NoWheelSlider(Qt.Orientation.Horizontal)
        sl.setRange(lo, hi)
        sl.setValue(val)
        sl.setStyleSheet(_TM_SLIDER_QSS)
        sl.valueChanged.connect(cb)
        row.addWidget(sl, 1)
        val_lbl = QLabel(f"{val}{suffix}")
        val_lbl.setStyleSheet("color: #dfe6f0; font-size: 11px;")
        val_lbl.setFixedWidth(52)
        val_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(val_lbl)
        return self._row_widget(row), sl, val_lbl

    def _build_page_bg_card(self) -> QWidget:
        card = _EditCard("页面底色", self)
        cf = card.content()
        self._page_bg_row = _TMPresetRow(
            _TM_PRESETS_PAGE, self._page_bg_hex, self._on_page_bg,
            allow_empty=True)
        cf.addWidget(self._page_bg_row)
        _d = QLabel("「跟随主题」= 用主题自带底色；自定义取色即时生效。")
        _d.setWordWrap(True)
        _d.setStyleSheet("color: #8b97aa; font-size: 10px;")
        cf.addWidget(_d)
        return card

    def _build_tray_card(self) -> QWidget:
        card = _EditCard("底板 · 颜色 / 透明度 / 模糊 / 外框线 / 留白", self)
        cf = card.content()

        # 底板颜色（预设 + 自定义）
        self._tray_color_row = _TMPresetRow(
            _TM_PRESETS_TRAY, self._tray_color_hex, self._on_tray_color)
        cf.addWidget(self._tray_color_row)

        w, sl, vl = self._tm_slider_row(
            "透明度", 0, 100, self._tray_opacity, self._on_tray_opacity, "%")
        self._tray_opacity_row_w, self._tray_opacity_slider = w, sl
        self._tray_opacity_val = vl
        cf.addWidget(w)

        w, sl, vl = self._tm_slider_row(
            "模糊", 0, 50, self._tray_blur, self._on_tray_blur, "px")
        self._tray_blur_row_w, self._tray_blur_slider = w, sl
        self._tray_blur_val = vl
        cf.addWidget(w)

        w, sl, vl = self._tm_slider_row(
            "外框线宽度", 0, 4, self._tray_border_width,
            self._on_tray_border_width, "px")
        self._tray_bw_row_w, self._tray_bw_slider = w, sl
        self._tray_bw_val = vl
        cf.addWidget(w)

        w, sl, vl = self._tm_slider_row(
            "外圈留白", 0, 16, self._margin, self._on_margin, " px")
        self._margin_row_w, self._margin_slider = w, sl
        self._margin_val = vl
        cf.addWidget(w)

        # 分隔线 + 自定义背景（放最下面，v2 布局）
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background: rgba(255,255,255,0.08); border: none;")
        sep.setFixedHeight(1)
        cf.addWidget(sep)

        self._card_bg_enabled_btn = _TMSwitch("自定义背景")
        self._card_bg_enabled_btn.setChecked(self._card_bg_enabled)
        self._card_bg_enabled_btn.toggled.connect(self._on_card_bg_enabled)
        cf.addWidget(self._card_bg_enabled_btn)

        # 展开面板：选择图片 + 图片蒙版颜色
        self._card_bg_expand = QFrame()
        self._card_bg_expand.setStyleSheet(
            "QFrame { background: rgba(0,0,0,0.20);"
            " border: 1px dashed rgba(255,255,255,0.16); border-radius: 6px; }")
        ex = QVBoxLayout(self._card_bg_expand)
        ex.setContentsMargins(8, 6, 8, 6)
        ex.setSpacing(6)
        img_row = QHBoxLayout()
        img_row.setSpacing(6)
        # 响应式路径标签：随剩余空间收缩，resize 时中段省略，单行永不撑宽窗口。
        img_lbl = _PathLabel()
        self._card_bg_image_lbl = img_lbl
        img_lbl.set_path(self._card_bg_image)
        img_row.addWidget(img_lbl, 1)
        img_btn = QPushButton("选择图片...")
        img_btn.setStyleSheet(_tm_btn_style())
        img_btn.setFixedHeight(22)
        img_btn.clicked.connect(self._pick_card_bg_image)
        self._card_bg_image_btn = img_btn
        img_row.addWidget(img_btn)
        img_clr = QPushButton("清除")
        img_clr.setStyleSheet(_tm_btn_style())
        img_clr.setFixedHeight(22)
        img_clr.clicked.connect(self._clear_card_bg_image)
        self._card_bg_image_clear_btn = img_clr
        img_row.addWidget(img_clr)
        ex.addLayout(img_row)
        self._card_bg_color_row = _TMPresetRow(
            _TM_PRESETS_MASK, self._card_bg_color_hex, self._on_card_bg_color)
        ex.addWidget(self._card_bg_color_row)
        cf.addWidget(self._card_bg_expand)

        self._sync_card_bg_controls()
        return card

    def _build_card_fill_card(self) -> QWidget:
        card = _EditCard("单卡填充", self)
        cf = card.content()
        self._card_fill_color_row = _TMPresetRow(
            _TM_PRESETS_FILL, self._card_fill_hex, self._on_card_fill_color)
        cf.addWidget(self._card_fill_color_row)

        alpha_pct = round(self._card_fill_alpha / 255.0 * 100)
        w, sl, vl = self._tm_slider_row(
            "不透明度", 0, 100, alpha_pct, self._on_card_fill_alpha, "%")
        self._card_fill_alpha_row_w = w
        self._card_fill_slider = sl
        self._card_fill_alpha_val = vl
        cf.addWidget(w)
        return card

    def _build_editor_dim_card(self) -> QWidget:
        card = _EditCard("编辑页蒙版深度", self)
        cf = card.content()
        pct = round(self._editor_dim_alpha / 255.0 * 100)
        w, sl, vl = self._tm_slider_row(
            "蒙版深度", 0, 100, pct, self._on_editor_dim, "%")
        self._editor_dim_row_w = w
        self._editor_dim_slider = sl
        self._editor_dim_val = vl
        self._editor_dim_val.setText(f"{pct}% (α{self._editor_dim_alpha})")
        cf.addWidget(w)
        # 预览叠加开关收进本卡（v2 布局），沿用 _dim_preview_chk 属性名。
        self._dim_preview_chk = _TMSwitch("预览中叠加编辑蒙版")
        self._dim_preview_chk.setChecked(True)
        self._dim_preview_chk.toggled.connect(lambda _v: self._preview.update())
        cf.addWidget(self._dim_preview_chk)
        return card

    # -- 快照 / 收集 -------------------------------------------------------
    def _collect(self) -> dict:
        return {
            "card_bg_enabled": self._card_bg_enabled,
            "card_bg_color_hex": self._card_bg_color_hex,
            "card_bg_image": self._card_bg_image,
            "card_fill_hex": self._card_fill_hex,
            "card_fill_alpha": self._card_fill_alpha,
            "editor_dim_alpha": self._editor_dim_alpha,
            "margin": self._margin,
            "tray_color_hex": self._tray_color_hex,
            "tray_opacity": self._tray_opacity,
            "tray_border_width": self._tray_border_width,
            "tray_blur": self._tray_blur,
            "page_bg_hex": self._page_bg_hex,
        }

    # -- card_bg 处理 ------------------------------------------------------
    def _sync_card_bg_controls(self) -> None:
        # v2：开关打开 → 展开面板（图片 + 图片蒙版颜色）整体可见。
        self._card_bg_expand.setVisible(self._card_bg_enabled)

    def _on_card_bg_enabled(self, checked: bool) -> None:
        self._card_bg_enabled = bool(checked)
        self._sync_card_bg_controls()
        self._push_card_bg()

    def _on_card_bg_color(self, hexv: str) -> None:
        self._card_bg_color_hex = str(hexv or "")
        self._push_card_bg()

    def _set_card_bg_path_label(self, path: str) -> None:
        """路径标签响应式显示：中段省略，完整路径放 tooltip。

        标签为 ``_PathLabel``，省略宽度随控件实际宽度变化（resizeEvent 内重做），
        因此长路径不会再撑宽窗口。
        """
        lbl = getattr(self, "_card_bg_image_lbl", None)
        if lbl is None:
            return
        lbl.set_path(path)
        lbl.setToolTip(path or "（未选择图片）")

    def _pick_card_bg_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择卡片区背景图片", "", "图片 (*.png *.jpg *.jpeg *.bmp)")
        if path:
            self._card_bg_image = path
            self._set_card_bg_path_label(path)
            self._push_card_bg()

    def _clear_card_bg_image(self) -> None:
        self._card_bg_image = ""
        self._set_card_bg_path_label("")
        self._push_card_bg()

    def _push_card_bg(self) -> None:
        # 模式自动派生：选了图片 → image，否则 solid（v2 去掉手动模式按钮）。
        self._card_bg_mode = "image" if self._card_bg_image else "solid"
        style.apply_card_bg(self._app, self._card_bg_enabled, self._card_bg_mode,
                             self._card_bg_color_hex, self._card_bg_image)
        b = self._breath
        b._card_bg_enabled = self._card_bg_enabled
        b._card_bg_mode = self._card_bg_mode
        b._card_bg_color_hex = self._card_bg_color_hex
        b._card_bg_image = self._card_bg_image

    # -- 底板托盘处理（颜色/透明度/模糊/外框线宽度）-------------------------
    def _on_tray_color(self, hexv: str) -> None:
        self._tray_color_hex = str(hexv or "")
        self._push_tray()

    def _on_tray_opacity(self, val: int) -> None:
        self._tray_opacity = max(0, min(100, int(val)))
        self._tray_opacity_val.setText(f"{self._tray_opacity}%")
        self._push_tray()

    def _on_tray_blur(self, val: int) -> None:
        self._tray_blur = max(0, min(50, int(val)))
        self._tray_blur_val.setText(f"{self._tray_blur}px")
        self._push_tray()

    def _on_tray_border_width(self, val: int) -> None:
        self._tray_border_width = max(0, min(4, int(val)))
        self._tray_bw_val.setText(
            ("关" if self._tray_border_width == 0
             else f"{self._tray_border_width}px"))
        self._push_tray()

    def _push_tray(self) -> None:
        # 单一真值源：cfg.tray_* + win.apply_tray + 镜像回呼吸灯页内存，
        # 两边永不互相覆盖（呼吸灯页 _save 用的是它自己的内存值）。
        style.apply_tray_settings(
            self._app, self._tray_color_hex, self._tray_opacity,
            self._tray_border_width, self._tray_blur)
        b = self._breath
        b._tray_color_hex = self._tray_color_hex
        b._tray_opacity = self._tray_opacity
        b._tray_border = self._tray_border_width > 0
        b._tray_blur = self._tray_blur

    # -- 页面底色处理 ------------------------------------------------------
    def _on_page_bg(self, hexv: str) -> None:
        self._page_bg_hex = str(hexv or "")
        style.apply_page_bg(self._app, self._page_bg_hex)

    # -- card_fill 处理 ---------------------------------------------------
    def _on_card_fill_color(self, hexv: str) -> None:
        self._card_fill_hex = str(hexv or "")
        self._push_card_fill()

    def _on_card_fill_alpha(self, val: int) -> None:
        self._card_fill_alpha = round(val / 100.0 * 255)
        self._card_fill_alpha_val.setText(f"{val}%")
        self._push_card_fill()

    def _push_card_fill(self) -> None:
        style.apply_card_fill(self._app, self._card_fill_hex, self._card_fill_alpha)
        b = self._breath
        b._card_fill_hex = self._card_fill_hex
        b._card_fill_alpha = self._card_fill_alpha

    # -- editor_dim 处理 --------------------------------------------------
    def _on_editor_dim(self, val: int) -> None:
        self._editor_dim_alpha = round(val / 100.0 * 255)
        self._editor_dim_val.setText(f"{val}% (α{self._editor_dim_alpha})")
        self._push_editor_dim()

    def _push_editor_dim(self) -> None:
        style.apply_editor_dim(self._app, self._editor_dim_alpha)
        b = self._breath
        b._editor_dim_alpha = self._editor_dim_alpha

    # -- margin 处理 ------------------------------------------------------
    def _on_margin(self, v: int) -> None:
        self._margin = max(0, int(v))
        self._margin_val.setText(f"{self._margin} px")
        self._push_margin()

    def _push_margin(self) -> None:
        style.apply_card_margin(self._app, self._margin)
        self._eff._margin = self._margin

    # -- 预览辅助（供 _MaskPreviewCanvas 调用）---------------------------
    def dim_preview_on(self) -> bool:
        return self._dim_preview_chk.isChecked()

    def bg_pixmap(self) -> Any:
        path = str(getattr(self._breath, "_card_bg_image", "") or "")
        if not path:
            return None
        if getattr(self, "_bg_img_path", None) != path:
            self._bg_img_path = path
            try:
                self._bg_img = QPixmap(path) if os.path.isfile(path) else None
            except Exception:
                self._bg_img = None
        return getattr(self, "_bg_img", None)

    # -- 区域标注 ---------------------------------------------------------
    def _region_for(self, obj: Any) -> Optional[str]:
        o = obj
        while o is not None:
            rid = self._card_regions.get(id(o))
            if rid:
                return rid
            o = o.parent()
        return None

    def _set_highlight(self, rid: Optional[str]) -> None:
        self._active_region = rid
        self._preview.update()

    def eventFilter(self, obj: Any, event: Any) -> bool:  # noqa: N802
        et = event.type()
        if et in (QEvent.Type.Enter, QEvent.Type.HoverEnter, QEvent.Type.FocusIn):
            rid = self._region_for(obj)
            if rid:
                self._set_highlight(rid)
        elif et in (QEvent.Type.Leave, QEvent.Type.HoverLeave,
                    QEvent.Type.FocusOut):
            if self._region_for(obj):
                self._set_highlight(None)
        return super().eventFilter(obj, event)

    # -- show / hide -----------------------------------------------------
    def showEvent(self, ev: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(ev)
        self._poll.start()

    def hideEvent(self, ev: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(ev)
        self._poll.stop()
        self._set_highlight(None)

    # -- load / commit ---------------------------------------------------
    def refresh(self) -> None:
        """从源面板重新现读（用户在旧面板改过后回本页时保持一致）。"""
        _cfgv = getattr(self._app.config_manager, "config", None)
        # 同 __init__：card_bg_* 单一真值源 = config（breath 未从 config 加载，
        # 不能作为开关初值；这里读 config 后镜像回 breath 供预览）。
        self._card_bg_enabled = bool(getattr(_cfgv, "card_bg_enabled", False))
        self._card_bg_mode = str(getattr(_cfgv, "card_bg_mode", "solid") or "solid")
        self._card_bg_color_hex = str(getattr(_cfgv, "card_bg_color", "") or "")
        self._card_bg_image = str(getattr(_cfgv, "card_bg_image", "") or "")
        _b = self._breath
        _b._card_bg_enabled = self._card_bg_enabled
        _b._card_bg_mode = self._card_bg_mode
        _b._card_bg_color_hex = self._card_bg_color_hex
        _b._card_bg_image = self._card_bg_image
        self._card_fill_hex = str(getattr(self._breath, "_card_fill_hex", "") or "")
        self._card_fill_alpha = int(getattr(self._breath, "_card_fill_alpha", 165) or 165)
        self._editor_dim_alpha = int(getattr(self._breath, "_editor_dim_alpha", 118) or 118)
        self._margin = int(getattr(self._eff, "_margin", 8) or 8)
        self._tray_color_hex = str(
            getattr(self._breath, "_tray_color_hex", "")
            or getattr(_cfgv, "tray_color", "") or "0e1117")
        _t_op = getattr(self._breath, "_tray_opacity", None)
        if _t_op is None:
            _t_op = getattr(_cfgv, "tray_opacity", 86)
        self._tray_opacity = max(0, min(100, int(_t_op)))
        _t_bw = getattr(_cfgv, "tray_border_width", None)
        if _t_bw is None:
            _t_bw = 2 if bool(getattr(self._breath, "_tray_border",
                                      getattr(_cfgv, "tray_border", True))) else 0
        self._tray_border_width = max(0, min(4, int(_t_bw)))
        _t_bl = getattr(self._breath, "_tray_blur", None)
        if _t_bl is None:
            _t_bl = getattr(_cfgv, "tray_blur", 18)
        self._tray_blur = max(0, min(50, int(_t_bl)))
        self._page_bg_hex = str(getattr(_cfgv, "page_bg_color", "") or "")
        # 回填可见控件
        self._page_bg_row.set_current(self._page_bg_hex)
        self._tray_color_row.set_current(self._tray_color_hex)
        self._tray_opacity_slider.blockSignals(True)
        self._tray_blur_slider.blockSignals(True)
        self._tray_bw_slider.blockSignals(True)
        self._margin_slider.blockSignals(True)
        self._tray_opacity_slider.setValue(self._tray_opacity)
        self._tray_blur_slider.setValue(self._tray_blur)
        self._tray_bw_slider.setValue(self._tray_border_width)
        self._margin_slider.setValue(self._margin)
        self._tray_opacity_slider.blockSignals(False)
        self._tray_blur_slider.blockSignals(False)
        self._tray_bw_slider.blockSignals(False)
        self._margin_slider.blockSignals(False)
        self._tray_opacity_val.setText(f"{self._tray_opacity}%")
        self._tray_blur_val.setText(f"{self._tray_blur}px")
        self._tray_bw_val.setText(
            "关" if self._tray_border_width == 0
            else f"{self._tray_border_width}px")
        self._margin_val.setText(f"{self._margin} px")
        self._card_bg_enabled_btn.setChecked(self._card_bg_enabled)
        self._set_card_bg_path_label(self._card_bg_image)
        self._card_bg_color_row.set_current(self._card_bg_color_hex)
        self._sync_card_bg_controls()
        self._card_fill_color_row.set_current(self._card_fill_hex)
        self._card_fill_slider.blockSignals(True)
        self._card_fill_slider.setValue(round(self._card_fill_alpha / 255.0 * 100))
        self._card_fill_slider.blockSignals(False)
        self._card_fill_alpha_val.setText(f"{round(self._card_fill_alpha / 255.0 * 100)}%")
        self._editor_dim_slider.blockSignals(True)
        self._editor_dim_slider.setValue(round(self._editor_dim_alpha / 255.0 * 100))
        self._editor_dim_slider.blockSignals(False)
        self._editor_dim_val.setText(f"{round(self._editor_dim_alpha / 255.0 * 100)}% (α{self._editor_dim_alpha})")
        self._status.setText("")
        self._set_highlight(None)
        self._preview.update()

    def _save(self) -> None:
        # live config 已由各 apply_* 实时写内存；这里只 flush 到磁盘。
        cm = getattr(self._app, "config_manager", None)
        if cm is not None:
            try:
                cm.save()
            except Exception:
                pass
        self._status.setText("已保存")
        self._overlay.open_layout()

    def _cancel(self) -> None:
        # 还原到进入本页时的快照（config + HUD + 源面板内存 + 可见控件）。
        s = self._snap
        self._card_bg_enabled = s["card_bg_enabled"]
        self._card_bg_color_hex = s["card_bg_color_hex"]
        self._card_bg_image = s["card_bg_image"]
        self._card_fill_hex = s["card_fill_hex"]
        self._card_fill_alpha = s["card_fill_alpha"]
        self._editor_dim_alpha = s["editor_dim_alpha"]
        self._margin = s["margin"]
        self._tray_color_hex = s["tray_color_hex"]
        self._tray_opacity = s["tray_opacity"]
        self._tray_border_width = s["tray_border_width"]
        self._tray_blur = s["tray_blur"]
        self._page_bg_hex = s["page_bg_hex"]
        self._push_card_bg()
        self._push_card_fill()
        self._push_editor_dim()
        self._push_margin()
        self._push_tray()
        self._on_page_bg(self._page_bg_hex)
        self.refresh()
        self._overlay.open_layout()

    # -- nav -------------------------------------------------------------
    def nav_items(self) -> list:
        items: list = []
        items.append(NavItem(self._back_btn, "action",
                             confirm=self._overlay.open_layout, hint="B"))
        # 页面底色 / 底板颜色 / 图片蒙版 / 单卡填充 的「自定义」取色按钮。
        items.append(NavItem(self._page_bg_row.custom_button(), "action",
                             confirm=self._page_bg_row._pick_custom, hint="A"))
        items.append(NavItem(self._tray_color_row.custom_button(), "action",
                             confirm=self._tray_color_row._pick_custom, hint="A"))
        items.append(NavItem(self._tray_opacity_slider, "slider",
                             nudge=lambda d, sl=self._tray_opacity_slider: (
                                 sl.setValue(max(0, min(100, sl.value() + d))), True)[1]))
        items.append(NavItem(self._tray_blur_slider, "slider",
                             nudge=lambda d, sl=self._tray_blur_slider: (
                                 sl.setValue(max(0, min(50, sl.value() + d))), True)[1]))
        items.append(NavItem(self._tray_bw_slider, "slider",
                             nudge=lambda d, sl=self._tray_bw_slider: (
                                 sl.setValue(max(0, min(4, sl.value() + d))), True)[1]))
        items.append(NavItem(self._margin_slider, "slider",
                             nudge=lambda d, sl=self._margin_slider: (
                                 sl.setValue(max(0, min(16, sl.value() + d))), True)[1]))
        items.append(NavItem(self._card_bg_enabled_btn, "toggle",
                             confirm=self._card_bg_enabled_btn.toggle, hint="A"))
        items.append(NavItem(self._card_bg_image_btn, "action",
                             confirm=self._pick_card_bg_image, hint="A"))
        items.append(NavItem(self._card_bg_color_row.custom_button(), "action",
                             confirm=self._card_bg_color_row._pick_custom, hint="A"))
        items.append(NavItem(self._card_fill_color_row.custom_button(), "action",
                             confirm=self._card_fill_color_row._pick_custom, hint="A"))
        items.append(NavItem(self._card_fill_slider, "slider",
                             nudge=lambda d, sl=self._card_fill_slider: (
                                 sl.setValue(max(0, min(100, sl.value() + d))), True)[1]))
        items.append(NavItem(self._editor_dim_slider, "slider",
                             nudge=lambda d, sl=self._editor_dim_slider: (
                                 sl.setValue(max(0, min(100, sl.value() + d))), True)[1]))
        items.append(NavItem(self._dim_preview_chk, "toggle",
                             confirm=self._dim_preview_chk.toggle, hint="A"))
        items.append(NavItem(self._save_btn, "action",
                             confirm=self._save, hint="Y"))
        items.append(NavItem(self._cancel_btn, "action",
                             confirm=self._cancel, hint="B"))
        return items


class XInputGateEditPanel(QWidget):
    """手柄隔离设置：凝冰结魄前台时目标游戏读不到手柄。

    双模式：
    - DLL 劫持（单机游戏）：fake DLL 部署到游戏目录，HUD 模式手柄可用
    - 设备禁用（反作弊游戏）：前台禁用物理手柄，HUD 模式用键盘，需管理员
    """

    def __init__(self, app: Any, overlay: Any) -> None:
        super().__init__()
        self.setStyleSheet(
            "QLabel { background: transparent; border: none; }"
            "QPushButton { border: none; }"
            "QPushButton:hover { border: none; }"
            "QListWidget { background: rgba(255,255,255,0.05);"
            " border: 1px solid rgba(255,255,255,0.10); border-radius: 6px; }"
            "QCheckBox { color: #dfe6f0; }"
            "QCheckBox::indicator { width: 16px; height: 16px; }")
        self._app = app
        self._overlay = overlay

        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(12, 12, 12, 12)

        title = QLabel("手柄隔离 · 编辑项")
        title.setStyleSheet(
            f"QLabel{{color: {_block_card_color_hex(self._app, 'power')};"
            f"font-size: {PAGE_TITLE_SIZE}px; font-weight: bold;}}")
        root.addWidget(title)

        note = QLabel(
            "凝冰结魄前台时，目标游戏读不到手柄。\n"
            "单机游戏用 DLL 劫持模式（HUD 手柄可用，零风险）；"
            "反作弊游戏用设备禁用模式（HUD 用键盘，零风险）。")
        note.setWordWrap(True)
        note.setStyleSheet("color: #aeb9c9; font-size: 11px;")
        root.addWidget(note)

        # --- 总开关 ---
        self._enable_chk = QCheckBox("启用手柄隔离")
        self._enable_chk.setStyleSheet("color: #dfe6f0; font-size: 13px;")
        self._enable_chk.toggled.connect(self._on_toggle_enabled)
        root.addWidget(self._enable_chk)

        # --- 添加游戏 ---
        btn_row = QHBoxLayout()
        self._add_btn = HintButton("添加游戏...")
        self._add_btn.setStyleSheet(
            "QPushButton { background: rgba(127,209,255,0.15); color: #7fd1ff;"
            " border: none; border-radius: 6px; padding: 6px 14px; }")
        self._add_btn.clicked.connect(self._on_add_game)
        btn_row.addWidget(self._add_btn)
        self._remove_btn = HintButton("移除选中")
        self._remove_btn.setStyleSheet(
            "QPushButton { background: rgba(255,100,100,0.12); color: #ff8888;"
            " border: none; border-radius: 6px; padding: 6px 14px; }")
        self._remove_btn.clicked.connect(self._on_remove_game)
        btn_row.addWidget(self._remove_btn)
        btn_row.addStretch(1)
        root.addLayout(btn_row)

        # --- 游戏列表 ---
        self._list = QListWidget()
        self._list.setStyleSheet(
            "QListWidget { background: rgba(255,255,255,0.05); color: #dfe6f0;"
            " border: 1px solid rgba(255,255,255,0.10); border-radius: 6px;"
            " font-size: 11px; }")
        root.addWidget(self._list)

        # --- 管理员状态提示 ---
        self._admin_lbl = QLabel()
        self._admin_lbl.setWordWrap(True)
        self._admin_lbl.setStyleSheet("color: #aeb9c9; font-size: 11px;")
        root.addWidget(self._admin_lbl)

        root.addStretch(1)

        # --- 关闭按钮 ---
        close = HintButton("关闭")
        close.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 6px; padding: 6px 14px 6px 18px; }")
        close.clicked.connect(self._overlay.open_layout)
        self._close = close
        hb = QHBoxLayout()
        hb.addStretch(1)
        hb.addWidget(close)
        root.addLayout(hb)

    def _cfg(self):
        app = self._app
        return getattr(app, "config", None)

    def _on_toggle_enabled(self, checked: bool) -> None:
        cfg = self._cfg()
        if cfg is not None:
            cfg.xinput_gate_enabled = bool(checked)
            self._save()

    def _on_add_game(self) -> None:
        from PyQt6.QtWidgets import QFileDialog
        from ..platform.xinput_gate import XInputGate
        path, _ = QFileDialog.getOpenFileName(
            self, "选择游戏 exe", "", "可执行文件 (*.exe)")
        if not path:
            return
        # 自动检测反作弊
        is_ac = XInputGate.detect_anticheat(path)
        mode = "device_disable" if is_ac else "dll_hijack"
        cfg = self._cfg()
        if cfg is None:
            return
        # 避免重复添加
        for g in cfg.xinput_gate_games:
            if g.get("exe", "").lower() == path.lower():
                return
        cfg.xinput_gate_games.append({"exe": path, "mode": mode})
        # DLL 劫持模式：部署 fake DLL
        if mode == "dll_hijack":
            try:
                gate = XInputGate()
                gate.deploy_to_game(path)
                gate.close()
            except Exception:
                pass
        self._save()
        self.refresh()

    def _on_remove_game(self) -> None:
        row = self._list.currentRow()
        if row < 0:
            return
        cfg = self._cfg()
        if cfg is None or row >= len(cfg.xinput_gate_games):
            return
        g = cfg.xinput_gate_games.pop(row)
        # DLL 劫持模式：移除 fake DLL
        if g.get("mode") == "dll_hijack":
            try:
                from ..platform.xinput_gate import XInputGate
                gate = XInputGate()
                gate.remove_from_game(g.get("exe", ""))
                gate.close()
            except Exception:
                pass
        self._save()
        self.refresh()

    def _save(self) -> None:
        app = self._app
        if hasattr(app, "save_all"):
            app.save_all()

    def refresh(self) -> None:
        cfg = self._cfg()
        enabled = bool(cfg and getattr(cfg, "xinput_gate_enabled", False))
        self._enable_chk.blockSignals(True)
        self._enable_chk.setChecked(enabled)
        self._enable_chk.blockSignals(False)
        # 游戏列表
        self._list.clear()
        if cfg:
            for g in getattr(cfg, "xinput_gate_games", []):
                exe = g.get("exe", "")
                mode = g.get("mode", "dll_hijack")
                name = os.path.basename(exe)
                tag = "设备禁用" if mode == "device_disable" else "DLL劫持"
                self._list.addItem(f"{name}  [{tag}]  {exe}")
        # 管理员状态
        from ..platform.xinput_gate import is_admin
        admin = is_admin()
        if admin:
            self._admin_lbl.setText("管理员权限: 是（设备禁用模式可用）")
            self._admin_lbl.setStyleSheet("color: #5dcaa5; font-size: 11px;")
        else:
            self._admin_lbl.setText(
                "管理员权限: 否（设备禁用模式不可用，DLL 劫持模式不受影响）")
            self._admin_lbl.setStyleSheet("color: #fac775; font-size: 11px;")

    def nav_items(self) -> list[NavItem]:
        items: list[NavItem] = []
        items.append(NavItem(self._enable_chk, "toggle",
                             confirm=lambda: self._enable_chk.toggle(), hint="A"))
        items.append(NavItem(self._add_btn, "action",
                             confirm=self._on_add_game, hint="A"))
        items.append(NavItem(self._remove_btn, "action",
                             confirm=self._on_remove_game, hint="X"))
        items.append(NavItem(self._close, "action",
                             confirm=self._overlay.open_layout, hint="B"))
        return items


class RtssEditPanel(QWidget):
    """锁帧控制卡片编辑项：自动锁帧「升压上限」与「升压阈值」两个参数。

    这两个滑块原先直接堆在锁帧卡片主页，导致卡片过高、信息过载；现抽出到
    独立的编辑项页面，与风扇 / 降压编辑项保持一致的「可滚动 + 手柄控制 + 风格
    统一」体验（参照 UndervoltEditPanel）。参数直接读写 RtssBlock
    （app.blocks["rtss"]），松手即落盘，下一拍自动锁帧即生效。
    """

    def __init__(self, app: Any, overlay: Any) -> None:
        super().__init__()
        self.setStyleSheet(
            "QLabel { background: transparent; border: none; }"
            "QPushButton { border: none; }"
            "QPushButton:hover { border: none; }"
            "QPushButton:pressed { border: none; }"
            "QPushButton:focus { border: none; }"
            "QPushButton:checked { border: none; }")
        self._app = app
        self._overlay = overlay
        self._key = "rtss"

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 滚动区域：编辑项内容超出可视高度即出现竖向滚动条、可滚轮 / 键盘滚动。
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea{border:none; background:transparent;}"
            "QScrollBar:vertical{background:rgba(255,255,255,0.06); width:8px;"
            " border-radius:4px; margin:2px;}"
            "QScrollBar::handle:vertical{background:rgba(255,255,255,0.22);"
            " border-radius:4px;}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical"
            "{height:0px;}")
        inner = QWidget()
        inner.setStyleSheet("QWidget{background:transparent;}")
        # 约束 inner 最大宽度 = 滚动区可视宽度，确保内容不超出编辑 overlay 可用
        # 宽度（窗口宽度），横向不会溢出、不被压扁。
        inner.setMaximumWidth(2000)
        self._scroll = scroll
        self._inner = inner
        lay = QVBoxLayout(inner)
        lay.setSpacing(10)
        lay.setContentsMargins(14, 14, 14, 14)
        self._lay = lay

        title = ClickableTitle(_card_mark_name("锁帧控制") + " · 编辑项",
                                color=_block_card_color_hex(self._app, "rtss"),
                                size=PAGE_TITLE_SIZE)
        title.clicked.connect(lambda _=False: self._open_faq())
        lay.addWidget(title)

        desc = QLabel(
            "锁帧控制（RTSS 自动锁帧）：升压上限决定自动锁帧在低负载场景顶到的帧率，"
            "升压阈值决定系统 GPU 负载低于多少时触发升压。二者仅持久化到配置，"
            "下一拍自动锁帧即生效。拖动即时预览，松手写入配置。")
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #aeb9c9; font-size: 11px;")
        lay.addWidget(desc)

        # --- 升压上限 --------------------------------------------------
        t_title = QLabel("升压上限（自动锁帧顶到的帧率）")
        t_title.setStyleSheet("color: #62e0a8; font-weight: bold; font-size: 13px;")
        lay.addWidget(t_title)
        t_desc = QLabel(
            "自动锁帧（GPU 负载低于升压阈值时）把帧率顶到该上限，避免低负载空转高帧。"
            "范围即本机支持的帧率区间。")
        t_desc.setWordWrap(True)
        t_desc.setStyleSheet("color: #aeb9c9; font-size: 11px;")
        lay.addWidget(t_desc)
        self._target_slider, self._target_val = self._make_target_row(lay)

        # --- 升压阈值 --------------------------------------------------
        h_title = QLabel("升压阈值（触发升压的 GPU 负载上限）")
        h_title.setStyleSheet("color: #62e0a8; font-weight: bold; font-size: 13px;")
        lay.addWidget(h_title)
        h_desc = QLabel(
            "系统 GPU 负载低于该百分比时，自动锁帧将帧率顶到「升压上限」。数值越低越激进。")
        h_desc.setWordWrap(True)
        h_desc.setStyleSheet("color: #aeb9c9; font-size: 11px;")
        lay.addWidget(h_desc)
        self._thr_slider, self._thr_val = self._make_thr_row(lay)

        # --- 卡片颜色 --------------------------------------------------
        cc_lbl = QLabel("卡片颜色")
        cc_lbl.setStyleSheet("color: #7fd1ff; font-weight: bold; font-size: 13px;")
        lay.addWidget(cc_lbl)
        self._card_color_picker = CardColorPicker(
            self._app, self._overlay, "rtss")
        lay.addWidget(self._card_color_picker)

        lay.addStretch(1)

        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

        close = HintButton("关闭")
        close.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 6px;"
            " padding: 6px 14px 6px 18px; }")
        close.clicked.connect(self._overlay.open_layout)
        self._close = close
        hb = QHBoxLayout()
        hb.addStretch(1)
        hb.addWidget(close)
        root.addLayout(hb)

    def resizeEvent(self, ev: Any) -> None:  # noqa: N802 - Qt camelCase
        # 让 inner 内容宽度始终不超过滚动区可视宽度（= 窗口宽度），避免编辑项
        # 在窄窗口下横向溢出被裁切。
        super().resizeEvent(ev)
        if getattr(self, "_scroll", None) is not None:
            vw = self._scroll.viewport().width()
            if vw > 0:
                self._inner.setMaximumWidth(vw)

    # -- 疑惑解答（点击顶部「锁帧控制 · 编辑项」标题打开）------------
    def _open_faq(self) -> None:
        faq = self._overlay.faq()
        if faq is not None:
            faq.open_for("rtss", "鼠标 标题")

    # -- 区块访问 ------------------------------------------------------
    def _block(self) -> Any:
        return self._app.blocks.get("rtss") if self._app else None

    # -- 滑块行（滚轮不调滑块：滚轮滚动页面，与降压编辑项一致）----------
    def _make_target_row(self, root: "QVBoxLayout") -> "tuple[QSlider, QLabel]":
        b = self._block()
        fmin = getattr(b, "FPS_MIN", 20) if b else 20
        fmax = getattr(b, "FPS_MAX", 60) if b else 60
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel("升压上限")
        lbl.setStyleSheet("color: #cdd6e2; font-size: 11px;")
        lbl.setFixedWidth(104)
        slider = _UVNoWheelSlider(Qt.Orientation.Horizontal)
        slider.setRange(fmin, fmax)
        slider.setSingleStep(5)
        # 响应式：窄窗口收缩到 60px 下限，宽窗口占满（与降压编辑项一致）。
        slider.setMinimumWidth(60)
        slider.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Fixed)
        slider.setStyleSheet(_UV_EDIT_SLIDER_STYLE)
        val = QLabel("60 FPS")
        val.setStyleSheet("color: #62e0a8; font-size: 11px;")
        val.setFixedWidth(64)
        val.setAlignment(Qt.AlignmentFlag.AlignRight)
        row.addWidget(lbl)
        row.addWidget(slider)
        row.addWidget(val)
        root.addLayout(row)
        slider.valueChanged.connect(lambda v: self._on_target(v, save=False))
        slider.sliderReleased.connect(
            lambda: self._on_target(slider.value(), save=True))
        return slider, val

    def _make_thr_row(self, root: "QVBoxLayout") -> "tuple[QSlider, QLabel]":
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel("升压阈值")
        lbl.setStyleSheet("color: #cdd6e2; font-size: 11px;")
        lbl.setFixedWidth(104)
        slider = _UVNoWheelSlider(Qt.Orientation.Horizontal)
        slider.setRange(10, 90)
        slider.setSingleStep(5)
        # 响应式：窄窗口收缩到 60px 下限，宽窗口占满（与降压编辑项一致）。
        slider.setMinimumWidth(60)
        slider.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Fixed)
        slider.setStyleSheet(_UV_EDIT_SLIDER_STYLE)
        val = QLabel("50%")
        val.setStyleSheet("color: #62e0a8; font-size: 11px;")
        val.setFixedWidth(64)
        val.setAlignment(Qt.AlignmentFlag.AlignRight)
        row.addWidget(lbl)
        row.addWidget(slider)
        row.addWidget(val)
        root.addLayout(row)
        slider.valueChanged.connect(lambda v: self._on_thr(v, save=False))
        slider.sliderReleased.connect(
            lambda: self._on_thr(slider.value(), save=True))
        return slider, val

    def _on_target(self, v: int, save: bool) -> None:
        v = int(v)
        b = self._block()
        self._target_val.setText(f"{v} FPS")
        if b is not None:
            fmin = getattr(b, "FPS_MIN", 20)
            fmax = getattr(b, "FPS_MAX", 60)
            b.auto_target = max(fmin, min(fmax, v))
            if save:
                b.save_config()

    def _on_thr(self, v: int, save: bool) -> None:
        v = int(v)
        b = self._block()
        self._thr_val.setText(f"{v}%")
        if b is not None:
            b.auto_gpu_load_pct = max(10, min(90, v))
            if save:
                b.save_config()

    # -- 公开 API ------------------------------------------------------
    def edit_target(self, token: str) -> None:
        self._key = token or "rtss"
        self._card_color_picker.set_block_key(self._key)
        self.refresh()
        # 兜底：首次 resize 时 viewport 宽度可能未就绪（vw=0 被跳过），
        # 延后一拍再约束 inner 最大宽度，避免编辑项在窄窗口下超出。
        QTimer.singleShot(0, self._sync_inner_width)

    def _sync_inner_width(self) -> None:
        vw = self._scroll.viewport().width()
        if vw:
            self._inner.setMaximumWidth(vw)

    def refresh(self) -> None:
        b = self._block()
        if b is None:
            return
        fmin = getattr(b, "FPS_MIN", 20)
        fmax = getattr(b, "FPS_MAX", 60)
        self._target_slider.setRange(fmin, fmax)
        t = max(fmin, min(fmax, int(b.auto_target)))
        self._target_slider.blockSignals(True)
        self._target_slider.setValue(t)
        self._target_slider.blockSignals(False)
        self._target_val.setText(f"{t} FPS")
        h = max(10, min(90, int(b.auto_gpu_load_pct)))
        self._thr_slider.blockSignals(True)
        self._thr_slider.setValue(h)
        self._thr_slider.blockSignals(False)
        self._thr_val.setText(f"{h}%")

    def nav_items(self) -> list[NavItem]:
        items: list[NavItem] = []
        if getattr(self, "_card_color_picker", None) is not None:
            items.append(NavItem(self._card_color_picker._custom_btn, "action",
                                 confirm=self._card_color_picker._pick_custom,
                                 hint="C"))
            items.append(NavItem(self._card_color_picker._follow_btn, "action",
                                 confirm=self._card_color_picker._follow))

        # 升压上限（手柄：A×2 武装 → 方向键 nudge；nudge 在边界返回 False 回退焦点）
        def _nudge_target(d: int) -> bool:
            old = self._target_slider.value()
            b = self._block()
            fmin = getattr(b, "FPS_MIN", 20) if b else 20
            fmax = getattr(b, "FPS_MAX", 60) if b else 60
            nv = max(fmin, min(fmax, old + d * 5))
            if nv == old:
                return False
            self._target_slider.setValue(nv)
            self._on_target(nv, save=True)
            return True

        # 升压阈值（手柄：A×2 武装 → 方向键 nudge）
        def _nudge_thr(d: int) -> bool:
            old = self._thr_slider.value()
            nv = max(10, min(90, old + d * 5))
            if nv == old:
                return False
            self._thr_slider.setValue(nv)
            self._on_thr(nv, save=True)
            return True

        items.append(NavItem(
            self._target_slider, "slider",
            confirm=lambda: self._on_target(self._target_slider.value(), save=True),
            nudge=_nudge_target, hint="H"))
        items.append(NavItem(
            self._thr_slider, "slider",
            confirm=lambda: self._on_thr(self._thr_slider.value(), save=True),
            nudge=_nudge_thr, hint="M"))
        items.append(NavItem(self._close, "action",
                             confirm=self._overlay.open_layout, hint="B"))
        return items


class PowerPlanEditPanel(QWidget):
    """电源计划卡片编辑项：导入 / 移除用户 .pow 计划 + 卡片颜色。

    预设计划（YMSilent / YMPerformance / YMMedium）随包分发、不可移除，这里仅作
    说明；用户自己的计划通过文件选择器导入（powercfg /import，需管理员），或一键
    移除（powercfg /delete，需管理员）。导入 / 移除结果在面板内 status 行即时反馈
    ——编辑页上下文里区块的 toast 不可见，故不复用区块 show_toast。
    """

    def __init__(self, app: Any, overlay: Any) -> None:
        super().__init__()
        self._app = app
        self._overlay = overlay
        self._key = "powerplan"
        self._user_rows: list[tuple[QWidget, QLabel, QPushButton]] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        title = ClickableTitle(_card_mark_name("电源计划") + " · 编辑项",
                                color=_block_card_color_hex(self._app, "powerplan"),
                                size=PAGE_TITLE_SIZE)
        root.addWidget(title)

        # 滚动区域：用户计划可能较多，超出可视高度即出现竖向滚动条。
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        apply_thin_scroll(scroll, always_on=False)
        self._inner = QWidget()
        lay = QVBoxLayout(self._inner)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        scroll.setWidget(self._inner)
        root.addWidget(scroll, 1)

        self._admin_lbl = QLabel("")
        self._admin_lbl.setStyleSheet("color:#ffb454; font-size:12px;")
        lay.addWidget(self._admin_lbl)

        self._import_btn = QPushButton("导入电源计划 (.pow)")
        self._import_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color:#dfe6f0;"
            " border:none; border-radius:8px; padding:8px 14px; font-size:14px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.14); }")
        self._import_btn.clicked.connect(self._on_import)
        lay.addWidget(self._import_btn)

        hint = QLabel("预设计划（YMSilent / YMPerformance / YMMedium）随包分发、"
                      "不可移除；导入 / 移除你自己的计划需以管理员身份运行本程序。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8b97aa; font-size:12px;")
        lay.addWidget(hint)

        ulbl = QLabel("我的电源计划")
        ulbl.setStyleSheet("color:#cdd6e4; font-size:14px; font-weight:bold;")
        lay.addWidget(ulbl)

        self._user_box = QVBoxLayout()
        self._user_box.setContentsMargins(0, 0, 0, 0)
        self._user_box.setSpacing(6)
        lay.addLayout(self._user_box)

        self._empty_lbl = QLabel("（暂无导入的电源计划）")
        self._empty_lbl.setStyleSheet("color:#6b7689; font-size:12px;")
        lay.addWidget(self._empty_lbl)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color:#9aa6b8; font-size:12px;")
        lay.addWidget(self._status_lbl)

        lay.addStretch(1)

        cc_lbl = QLabel("卡片颜色")
        cc_lbl.setStyleSheet("color:#cdd6e4; font-size:14px; font-weight:bold;")
        lay.addWidget(cc_lbl)
        self._card_color_picker = CardColorPicker(
            self._app, self._overlay, "powerplan")
        lay.addWidget(self._card_color_picker)

        close = HintButton("关闭")
        close.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 6px;"
            " padding: 6px 14px 6px 18px; }")
        close.clicked.connect(self._overlay.open_layout)
        self._close = close
        hb2 = QHBoxLayout()
        hb2.addStretch(1)
        hb2.addWidget(close)
        root.addLayout(hb2)

    def _block(self):
        return self._app.blocks.get("powerplan") if self._app else None

    def edit_target(self, token: str) -> None:
        self._key = token.split("#")[0] if "#" in token else token
        if getattr(self, "_card_color_picker", None) is not None:
            self._card_color_picker.set_block_key("powerplan")
        self.refresh()

    def refresh(self) -> None:
        b = self._block()
        if b is None:
            self._admin_lbl.setText("")
            self._empty_lbl.setVisible(True)
            return
        if not b.admin:
            self._admin_lbl.setText(
                "⚠ 非管理员：导入 / 移除电源计划需以管理员身份运行")
        else:
            self._admin_lbl.setText("")
        user_plans = list(
            getattr(b.config.powerplan_config, "user_plans", []) or [])
        n = len(user_plans)
        while len(self._user_rows) < n:
            row = QWidget()
            hb = QHBoxLayout(row)
            hb.setContentsMargins(0, 0, 0, 0)
            hb.setSpacing(6)
            name = QLabel("")
            name.setStyleSheet("color:#dfe6f0; font-size:12px;")
            name.setWordWrap(True)
            rm = QPushButton("移除")
            rm.setFixedSize(56, 26)
            rm.setStyleSheet(
                "QPushButton { background: rgba(255,107,107,0.16); color:#ff8a8a;"
                " border:none; border-radius:7px; font-size:12px; }"
                "QPushButton:hover { background: rgba(255,107,107,0.28); }")
            hb.addWidget(name, 1)
            hb.addWidget(rm, 0)
            self._user_box.addWidget(row)
            self._user_rows.append((row, name, rm))
        while len(self._user_rows) > n:
            row, _n, _r = self._user_rows.pop()
            self._user_box.removeWidget(row)
            row.deleteLater()
        for i, p in enumerate(user_plans):
            row, name, rm = self._user_rows[i]
            guid = str(p.get("guid", "")).strip().lower()
            nm = str(p.get("name", "")).strip()
            name.setText(nm)
            try:
                rm.clicked.disconnect()
            except Exception:
                pass
            rm.clicked.connect(lambda _=False, g=guid: self._on_remove(g))
            row.setVisible(True)
        self._empty_lbl.setVisible(n == 0)
        self._status_lbl.setText("")

    def _on_import(self) -> None:
        b = self._block()
        if b is None:
            return
        path, _f = QFileDialog.getOpenFileName(
            self, "选择电源计划文件", "",
            "电源计划文件 (*.pow);;所有文件 (*.*)")
        if not path:
            return
        guid = b.import_plan(path)
        if guid:
            self._status_lbl.setStyleSheet("color:#4be38a; font-size:12px;")
            self._status_lbl.setText("✓ 已导入：" + os.path.basename(path))
        else:
            self._status_lbl.setStyleSheet("color:#ff6b6b; font-size:12px;")
            self._status_lbl.setText("✗ 导入失败（需管理员权限，或文件损坏）")
        self.refresh()

    def _on_remove(self, guid: str) -> None:
        b = self._block()
        if b is None:
            return
        ok = b.remove_plan(guid)
        if ok:
            self._status_lbl.setStyleSheet("color:#4be38a; font-size:12px;")
            self._status_lbl.setText("✓ 已移除电源计划")
        else:
            self._status_lbl.setStyleSheet("color:#ff6b6b; font-size:12px;")
            self._status_lbl.setText("✗ 移除失败（预设计划不可移除，或需管理员）")
        self.refresh()

    def nav_items(self) -> list[NavItem]:
        items: list[NavItem] = []
        if getattr(self, "_card_color_picker", None) is not None:
            items.append(NavItem(self._card_color_picker._custom_btn, "action",
                                 confirm=self._card_color_picker._pick_custom,
                                 hint="C"))
            items.append(NavItem(self._card_color_picker._follow_btn, "action",
                                 confirm=self._card_color_picker._follow))
        items.append(NavItem(self._import_btn, "action",
                             confirm=self._on_import, hint="A"))
        for row, _n, rm in self._user_rows:
            if row.isVisible():
                items.append(NavItem(rm, "action", confirm=rm.click))
        items.append(NavItem(self._close, "action",
                             confirm=self._overlay.open_layout, hint="B"))
        return items


class LSEditPanel(QWidget):
    """LS预设 编辑项：设置 LS 主程序路径（自动设置 / 浏览）。

    路径单独存一份 ls_config.json（与 preset 目录隔离）。「自动设置」扫描当前
    LosslessScaling.exe 进程读取其路径；扫不到提示手动设置或浏览。内容整体包在
    QScrollArea 里，避免长提示被压缩看不见。"""

    def __init__(self, app: Any, overlay: Any) -> None:
        super().__init__()
        self.setStyleSheet(
            "QLabel { background: transparent; border: none; }"
            "QPushButton { border: none; }"
            "QPushButton:hover { border: none; }"
            "QPushButton:pressed { border: none; }"
            "QPushButton:focus { border: none; }"
            "QPushButton:checked { border: none; }")
        self._app = app
        self._overlay = overlay

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea{border:none; background:transparent;}"
            "QScrollBar:vertical{background:rgba(255,255,255,0.06); width:8px;"
            " border-radius:4px; margin:2px;}"
            "QScrollBar::handle:vertical{background:rgba(255,255,255,0.22);"
            " border-radius:4px;}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical"
            "{height:0px;}")
        inner = QWidget()
        inner.setStyleSheet("QWidget{background:transparent;}")
        inner.setMaximumWidth(2000)
        self._scroll = scroll
        self._inner = inner
        lay = QVBoxLayout(inner)
        lay.setSpacing(10)
        lay.setContentsMargins(14, 14, 14, 14)
        self._lay = lay

        title = ClickableTitle(_card_mark_name("LS预设") + " · 编辑项",
                                color=_block_card_color_hex(self._app, "ls"),
                                size=PAGE_TITLE_SIZE)
        title.clicked.connect(lambda _=False: self._open_faq())
        lay.addWidget(title)

        desc = QLabel(
            "设置 Lossless Scaling 主程序路径。卡片右上角「▶ 启动 LS」按钮会用此路径"
            "拉起 LS；启动凝冰结魄时若路径为空或失效，会自动扫描正在运行的 LS 进程并写入。")
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #aeb9c9; font-size: 11px;")
        lay.addWidget(desc)

        # --- 路径 ------------------------------------------------------
        path_lbl = QLabel("LS 主程序路径")
        path_lbl.setStyleSheet("color: #7fd1ff; font-weight: bold; font-size: 13px;")
        lay.addWidget(path_lbl)

        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText(
            "例如 C:\\Games\\Lossless Scaling\\losslessscaling.exe")
        self._path_edit.setMinimumHeight(34)
        self._path_edit.setStyleSheet(
            "QLineEdit{background:rgba(255,255,255,0.08); color:#dfe6f0;"
            " border-radius:4px; padding:4px 8px; font-size:12px;}")
        self._path_edit.textChanged.connect(lambda t: self._on_path_edit(t))
        self._suppress_path_persist = False
        lay.addWidget(self._path_edit)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._autoset_btn = HintButton("自动设置")
        self._autoset_btn.set_style("#7fd1ff", False)
        self._autoset_btn.clicked.connect(lambda _=False: self._on_autoset())
        btn_row.addWidget(self._autoset_btn)
        self._browse_btn = HintButton("浏览…")
        self._browse_btn.set_style("#7fd1ff", False)
        self._browse_btn.clicked.connect(lambda _=False: self._on_browse())
        btn_row.addWidget(self._browse_btn)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)

        self._status_lbl = QLabel("")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet(
            "color: #9fb4c9; font-size: 11px; background: rgba(127,209,255,0.08);"
            " border-radius: 6px; padding: 5px 8px;")
        lay.addWidget(self._status_lbl)

        lay.addStretch(1)

        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

        close = HintButton("关闭")
        close.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 6px;"
            " padding: 6px 14px 6px 18px; }")
        close.clicked.connect(self._overlay.open_layout)
        self._close = close
        hb = QHBoxLayout()
        hb.addStretch(1)
        hb.addWidget(close)
        root.addLayout(hb)

        self.block = None
        self.refresh()

    # -- 疑惑解答 ------------------------------------------------------
    def _open_faq(self) -> None:
        faq = self._overlay.faq()
        if faq is not None:
            faq.open_for("ls", "标题")

    # -- 区块访问 ------------------------------------------------------
    def _block(self) -> Any:
        return self._app.blocks.get("ls") if self._app else None

    # -- 提示 / 持久化 --------------------------------------------------
    def _toast(self, msg: str, success: bool = True) -> None:
        blk = self._block()
        if blk is not None and hasattr(blk, "show_toast"):
            blk.show_toast(msg, success=success)

    def _persist(self) -> None:
        try:
            self._app.save_all()
        except Exception:  # noqa: BLE001
            pass

    # -- 数据刷新 ------------------------------------------------------
    def refresh(self) -> None:
        self.block = self._block()
        if self.block is not None:
            # 防止「打开编辑项 → setText → textChanged → set_exe_path」把框里的值
            # 又写回一遍：旧逻辑会因此把空串/旧值持久化，覆盖用户刚选的路径。
            self._suppress_path_persist = True
            try:
                self._path_edit.setText(self.block.exe_path)
            finally:
                self._suppress_path_persist = False
            self._update_status()

    def _update_status(self) -> None:
        if self.block is None:
            self._status_lbl.setText("")
            return
        p = self.block.exe_path
        if not p:
            self._status_lbl.setText(
                "尚未设置 LS 路径。可点「自动设置」扫描运行中的 LS 进程，或「浏览…」"
                "手动选择 losslessscaling.exe。")
        elif os.path.exists(p):
            self._status_lbl.setText(f"当前路径有效：\n{p}")
        else:
            self._status_lbl.setText(
                f"路径已失效（文件不存在），下次启动将自动重新探测：\n{p}")

    # -- 交互 ----------------------------------------------------------
    def _on_path_edit(self, text: str) -> None:
        if self._suppress_path_persist:
            return
        if self.block is not None:
            self.block.set_exe_path(text.strip())
        self._update_status()
        self._persist()

    def _on_autoset(self) -> None:
        if self.block is None:
            return
        if self.block.auto_detect_exe_path():
            self._path_edit.setText(self.block.exe_path)
            self._toast("已自动设置 LS 路径", success=True)
        else:
            self._toast("未检测到 LS 进程，请手动设置或浏览", success=False)
        self._update_status()
        self._persist()

    def _on_browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 Lossless Scaling 主程序", "",
            "Lossless Scaling (*.exe);;全部文件 (*.*)")
        if path:
            self._path_edit.setText(path)
            if self.block is not None:
                self.block.set_exe_path(path)
            self._toast("已设置 LS 路径", success=True)
            self._update_status()
            self._persist()

    def nav_items(self) -> list[NavItem]:
        return [
            NavItem(self._path_edit, "field",
                    confirm=lambda: self._on_path_edit(self._path_edit.text()),
                    hint="E"),
            NavItem(self._autoset_btn, "action", confirm=self._on_autoset, hint="S"),
            NavItem(self._browse_btn, "action", confirm=self._on_browse, hint="W"),
            NavItem(self._close, "action",
                    confirm=self._overlay.open_layout, hint="X"),
        ]

    def resizeEvent(self, ev: Any) -> None:  # noqa: N802 - Qt camelCase
        super().resizeEvent(ev)
        if getattr(self, "_scroll", None) is not None:
            vw = self._scroll.viewport().width()
            if vw > 0:
                self._inner.setMaximumWidth(vw)


# -- 开发同步（调试）面板辅助函数 ------------------------------------------
def _devsync_base_dir(fallback: str = "") -> str:
    """日志/配置的基目录：冻结=exe 目录；开发=含 run.py 的项目根。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(5):
        if (os.path.isfile(os.path.join(d, "run.py"))
                and os.path.isdir(os.path.join(d, "src"))):
            return d
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return fallback or d


def _devsync_log(msg: str) -> None:
    """把开发同步每一步写 logs/devsync.log。

    掌机上「点了没反应 / 卡住 / 起不来」全靠这份日志定位：改前整个拉取流程
    跑在 Qt 主线程，卡住时既没有 UI 反馈也没有任何落盘痕迹，事后无从查证。
    """
    try:
        logs = os.path.join(_devsync_base_dir(), "logs")
        os.makedirs(logs, exist_ok=True)
        with open(os.path.join(logs, "devsync.log"), "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%m-%d %H:%M:%S") + " " + msg + "\n")
    except Exception:  # noqa: BLE001 - 日志失败绝不能影响主流程
        pass


def _devsync_robocopy(src: str, dst: str, extra: list[str]) -> int:
    """调用 robocopy；返回退出码（< 8 视为成功）。失败返回 16。

    带超时保护：robocopy 在网络共享异常/目标被锁定时可能无限挂起，而本函数
    跑在 devsync 后台线程，无 timeout 会永久阻塞该线程。故改用 Popen +
    wait(timeout=120)，超时后强制回收子进程（含其可能派生的 robocopy 子树）。

    注意（2026-09-01）：本函数**只能**由 _DevSyncPullWorker 在工作线程里调用。
    它同步阻塞调用方，绝不可从 Qt 主线程（按钮槽）直接调用——那会让事件循环
    停摆、窗口被 Windows 标记「无响应」。
    """
    try:
        cmd = ["robocopy", src, dst] + list(extra)
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        return 16
    try:
        return int(proc.wait(timeout=120))
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        return 16
    except Exception:  # noqa: BLE001
        return 16


def _devsync_compile_check(folder: str) -> list[str]:
    """递归编译校验；返回编译失败的文件列表（空 = 全部通过）。

    ⚠️ 2026-09-01 重写（掌机「拉取后起不来」的 P0 根因）：
    旧实现是 ``py_compile.compile(fp, doraise=True, quiet=2)``，而 CPython 里
    ``quiet >= 2`` 的语义恰恰是「**静默吞掉一切错误：不打印、不抛异常、也不
    返回失败**」。实测 Python 3.13：把内容为 ``def (:`` 的文件传进去，调用
    正常返回，``bad`` 恒为空列表——这道「坏同步不锁死掌机」的防线从来没生效过。
    后果链条：
      * 拉取侧放行 → 坏源码照样原子切换进 ``_ext/src``；
      * 启动侧 ``run.py`` 用了同样的 ``quiet=2``（见 _apply_external_src_override），
        校验同样恒通过 → 坏源码被插到 ``sys.path`` 最前；
      * 新进程 import 即崩 → 起不来；而磁盘上的 ``_ext/src`` 已经是坏版本，
        之后每次重启都起不来 = 掌机被自己的同步流程锁死。
    现改为内置 ``compile()``：语法错直接抛 SyntaxError，而且**不落 .pyc**
    （py_compile 会往目录里写 ``__pycache__``，污染这个马上要做原子切换的
    临时目录）。用 ``tokenize.open`` 读取以正确处理 BOM 与
    ``# -*- coding: gbk -*-`` 编码声明。
    """
    import tokenize

    bad: list[str] = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fn in files:
            if fn.endswith(".py"):
                fp = os.path.join(root, fn)
                try:
                    with tokenize.open(fp) as fh:
                        src = fh.read()
                    compile(src, fp, "exec")
                except Exception as exc:  # noqa: BLE001
                    _devsync_log("[compile] 失败 " + fp + "：" + repr(exc))
                    bad.append(fp)
    return bad


class _DevSyncPullWorker(QThread):
    """后台执行「拉取源码」三阶段：连通性预检 → robocopy → 编译校验 → 原子切换。

    为什么必须放后台线程（2026-09-01 修，掌机「点拉取并重启 = 直接卡住」根因）：
    改前 ``DevSyncEditPanel._on_pull_restart`` 是按钮槽函数，全程在 Qt 主线程
    里同步跑完 robocopy（SMB 共享，src 约 21MB / 842 文件，掌机 WiFi 上数十秒，
    最坏 120s 超时）+ 全量 py_compile（96 个 .py）+ rmtree/rename。这段时间
    Qt 事件循环完全停摆：窗口不重绘、不响应手柄/键盘，Windows 直接把标题栏
    标成「无响应」——用户看到的就是「点一下就卡死」；很多人会顺手强制结束进程，
    于是重启永远不发生，表现为「无法重启」。讽刺的是 ``_devsync_robocopy``
    的 docstring 早就写着「本函数跑在 devsync 后台线程」，而实际并不存在这个
    后台线程，注释与实现长期背离。

    信号（全部跨线程回主线程，主线程只做 setText，零阻塞）：
      * ``progress(str)``    — 阶段文案
      * ``swapped(str)``     — 切换完成，参数=新的本地 src 路径（调用方续重启）
      * ``failed(str)``      — 失败原因（调用方恢复 UI 并提示，绝不退出程序）
    """

    progress = pyqtSignal(str)
    swapped = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, share: str, share_src: str, dst: str,
                 parent: Any = None) -> None:
        super().__init__(parent)
        self._share = share
        self._share_src = share_src
        self._dst = dst
        self._tmp = dst + ".tmp"
        self._old = dst + ".bak"

    # -- 三阶段 ---------------------------------------------------------
    def run(self) -> None:  # pragma: no cover - 依赖网络/文件系统
        try:
            self._run_inner()
        except Exception as exc:  # noqa: BLE001 - 兜底：异常也要回 UI，不能静默
            _devsync_log("[pull] 未预期异常：" + repr(exc))
            self.failed.emit("拉取异常：" + repr(exc))

    def _run_inner(self) -> None:
        _devsync_log("[pull] start share=" + self._share_src +
                     " dst=" + self._dst)
        # 0) 连通性预检：共享不可达时 robocopy 会拖满 120s 超时才报错，
        #    先用 5s 探活快速失败，省掉两分钟的干等。
        self.progress.emit("正在检查笔记本共享…")
        if not _devsync_probe_nbj(self._share, 5.0):
            self.failed.emit("笔记本共享不可达：" + self._share +
                             "（确认笔记本开机、已开共享、掌机与它同一局域网）")
            return

        # 1) 拷到临时目录（不直接覆盖正在用的 _ext/src）
        self.progress.emit("正在拉取源码（robocopy，可能要几十秒）…")
        if os.path.isdir(self._tmp):
            shutil.rmtree(self._tmp, ignore_errors=True)
        t0 = time.monotonic()
        rc = _devsync_robocopy(self._share_src, self._tmp,
                               ["/MIR", "/XD", "__pycache__"])
        _devsync_log("[pull] robocopy rc=" + str(rc) +
                     " cost=%.1fs" % (time.monotonic() - t0))
        if rc >= 8:
            self.failed.emit("拉取失败（robocopy 返回 " + str(rc) +
                             "；rc>=8 表示有文件没拷成功，详见 logs/devsync.log）")
            return

        # 2) 编译校验（决策 1：坏同步不锁死掌机）
        self.progress.emit("正在编译校验新源码…")
        bad = _devsync_compile_check(self._tmp)
        _devsync_log("[pull] compile bad=" + str(len(bad)))
        if bad:
            _devsync_log("[pull] first bad: " + str(bad[:3]))
            shutil.rmtree(self._tmp, ignore_errors=True)
            self.failed.emit(
                "编译失败 " + str(len(bad)) + " 个文件，已放弃切换"
                "（当前版本未被破坏）。首个失败：" + os.path.basename(bad[0]))
            return

        # 3) 原子切换：旧版备份 → 新版就位；任一步失败都要能回到旧版。
        self.progress.emit("正在切换源码目录…")
        if os.path.isdir(self._old):
            shutil.rmtree(self._old, ignore_errors=True)
        if os.path.isdir(self._dst):
            try:
                os.rename(self._dst, self._old)
            except Exception as exc:  # noqa: BLE001
                # 目录被占用（进程 cwd 在其内部 / 杀软扫描 / 文件被锁）时
                # rename 会失败。此时 dst 原封不动，直接放弃切换即可，
                # 绝不能把 tmp 硬塞进去造成半新半旧。
                _devsync_log("[pull] rename dst->old 失败：" + repr(exc))
                shutil.rmtree(self._tmp, ignore_errors=True)
                self.failed.emit(
                    "无法备份当前源码目录（可能被占用）：" + repr(exc) +
                    "。已保持当前版本不变。")
                return
        try:
            os.rename(self._tmp, self._dst)
        except Exception as exc:  # noqa: BLE001
            # 回滚：把旧版搬回去，保证 _ext/src 始终是一个完整可用的版本。
            _devsync_log("[pull] rename tmp->dst 失败，回滚：" + repr(exc))
            if not os.path.isdir(self._dst) and os.path.isdir(self._old):
                try:
                    os.rename(self._old, self._dst)
                except Exception:  # noqa: BLE001
                    pass
            shutil.rmtree(self._tmp, ignore_errors=True)
            self.failed.emit(
                "切换新源码失败，已回滚到旧版本：" + repr(exc))
            return

        _devsync_log("[pull] swapped ok -> " + self._dst)
        self._write_swap_marker()
        self.progress.emit("源码已更新，正在重启…")
        self.swapped.emit(self._dst)

    def _write_swap_marker(self) -> None:
        """写「切换待确认」哨兵（约定与 ``run.py:_ext_src_self_heal`` 成对）。

        新实例 GUI 起来后会删掉它（``run_gui`` → ``_clear_ext_src_swap_pending``）。
        若下次启动它还在且已过期，说明这次切换后的新进程根本没活下来（能编译
        但 import 即崩之类），run.py 会自动把 ``.bak`` 换回来——这是「坏同步
        不锁死掌机」的最后一道闸：没有它，一次坏同步就能让掌机再也起不来，
        而用户此时已经打不开面板去重新拉代码了。
        """
        try:
            parent = os.path.dirname(self._dst) or "."
            os.makedirs(parent, exist_ok=True)
            with open(os.path.join(parent, "src.swap_pending.json"),
                      "w", encoding="utf-8") as fh:
                json.dump({"ts": time.time(), "src": self._dst,
                           "bak": self._old}, fh)
        except Exception:  # noqa: BLE001 - 哨兵写失败不该阻断切换流程
            pass


class _DevSyncTaskWorker(QThread):
    """通用后台任务：把同步阻塞的脏活（robocopy / rmtree）挪出主线程。

    「上传日志」原先也和拉取一样，在按钮槽里直接跑 robocopy —— 走 SMB 上传
    整个 logs 目录最坏要 120s 超时，同样会把窗口卡成「无响应」。
    """

    done = pyqtSignal(int)      # 任务返回值（robocopy 退出码）
    failed = pyqtSignal(str)    # 任务抛异常

    def __init__(self, fn: Callable[[], int], parent: Any = None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:  # pragma: no cover - 依赖网络/文件系统
        try:
            self.done.emit(int(self._fn()))
        except Exception as exc:  # noqa: BLE001
            _devsync_log("[task] 异常：" + repr(exc))
            self.failed.emit(str(exc))


def _devsync_probe_nbj(share: str, timeout: float = 2.0) -> bool:
    """带超时的共享探测：不可达/超时返回 False，绝不卡 UI 线程。"""
    holder: dict[str, bool] = {}

    def _run() -> None:
        try:
            holder["ok"] = os.path.isdir(share)
        except Exception:  # noqa: BLE001
            holder["ok"] = False

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(timeout)
    return bool(holder.get("ok", False))


def _devsync_scan_lan_nbj() -> list[str]:
    """net view 枚举 SMB 主机，逐个（带超时）探测 \\host\\NBJ 共享。"""
    found: list[str] = []
    try:
        out = subprocess.run(
            ["net", "view"], capture_output=True, text=True, timeout=15)
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if (not line or line.startswith("服务器")
                    or line.startswith("---") or line.startswith("命令")):
                continue
            parts = line.split()
            if not parts:
                continue
            host = parts[0].strip("\\")
            share = "\\\\" + host + "\\NBJ"
            if _devsync_probe_nbj(share, 2.0):
                found.append(share)
    except Exception:  # noqa: BLE001
        pass
    return found


class _DevSyncLanScan(QThread):
    """后台扫描局域网 NBJ 共享，避免阻塞 UI。结果经信号回主线程。"""

    found = pyqtSignal(list)

    def run(self) -> None:  # pragma: no cover - 依赖网络
        self.found.emit(_devsync_scan_lan_nbj())


def _devsync_aux_btn_style(color: str) -> str:
    return (
        "QPushButton{background: rgba(255,255,255,0.06); color:" + color + ";"
        " border:none; border-radius:6px; padding:7px 12px; font-size:12px;}"
        "QPushButton:hover{background: rgba(255,255,255,0.14);}"
    )


class DevSyncEditPanel(QWidget):
    """开发同步（调试）面板：掌机 ↔ 笔记本 源码/日志同步。

    设计（grill-me 已确认）：
      - 单向两管道：源码 笔记本→掌机（权威源=笔记本）；日志 掌机→笔记本。
      - 「拉取源码并重启」：robocopy 笔记本共享 src → 本地临时 → 编译校验 →
        原子切换 _ext/src，随后提权重生 exe 加载新源码（决策 1：坏同步不锁死）。
      - 「上传日志」：手动把本地 logs → 笔记本共享 logs（决策 2：不做自动上传）。
      - 配置存 exe 旁 sync_config.json（可编辑，IP 变了改配置即可，零代码改动）。
      - 「重新扫描局域网」：net view 探测 \\host\\NBJ，自动填入笔记本共享。
    """

    def __init__(self, app: Any, overlay: Any) -> None:
        super().__init__()
        self._app = app
        self._overlay = overlay
        self._base = self._resolve_base()
        self._scan_thread: Optional["_DevSyncLanScan"] = None
        # 拉取 worker（后台线程）与重启观察器（QTimer 轮询）都要**存住引用**：
        # 局部变量会被 GC，QThread 被回收等于线程被强拆，QTimer 被回收则轮询
        # 直接停摆——表现为「点了按钮后再也没动静」。
        self._pull_worker: Optional["_DevSyncPullWorker"] = None
        self._upload_worker: Optional["_DevSyncTaskWorker"] = None
        self._restart_watcher: Optional["_RestartWatcher"] = None
        self._upload_dst: str = ""

        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(12, 12, 12, 12)

        title = QLabel("开发同步（调试）")
        title.setStyleSheet(
            "QLabel{color: #8fd0ff; font-size: 15px; font-weight: bold;}")
        root.addWidget(title)

        note = QLabel(
            "掌机 ↔ 笔记本 源码/日志同步。源码从笔记本单向下发，日志手动回传。\n"
            "「拉取并重启」会先编译校验再切换，坏同步不会锁死掌机。")
        note.setWordWrap(True)
        note.setStyleSheet("color: #aeb9c9; font-size: 11px;")
        root.addWidget(note)

        # 笔记本共享（可编辑，IP 变了改这里即可，零代码改动）
        _row1 = QHBoxLayout()
        _row1.addWidget(QLabel("笔记本共享"))
        self._share_edit = QLineEdit()
        self._share_edit.setPlaceholderText(r"\\笔记本主机名\NBJ 或 \\192.168.1.56\NBJ")
        self._share_edit.setStyleSheet(
            "QLineEdit{background: rgba(255,255,255,0.06); color:#dfe6f0;"
            " border:1px solid rgba(255,255,255,0.12); border-radius:6px;"
            " padding:4px 8px; font-size:12px;}")
        _row1.addWidget(self._share_edit, 1)
        self._test_btn = HintButton("测试连接")
        self._test_btn.setStyleSheet(_devsync_aux_btn_style("#7fd1ff"))
        self._test_btn.clicked.connect(self._on_test)
        _row1.addWidget(self._test_btn)
        root.addLayout(_row1)

        # 本地外部源码（只读展示）
        _row2 = QHBoxLayout()
        _row2.addWidget(QLabel("本地源码"))
        self._local_lbl = QLabel()
        self._local_lbl.setStyleSheet("color:#9fb0c4; font-size:12px;")
        _row2.addWidget(self._local_lbl, 1)
        root.addLayout(_row2)

        # 状态
        self._status_lbl = QLabel("未同步")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet("color:#aeb9c9; font-size:12px;")
        root.addWidget(self._status_lbl)

        root.addStretch(1)

        # 主操作：拉取并重启
        _act = QHBoxLayout()
        self._pull_btn = HintButton("拉取源码并重启")
        self._pull_btn.setStyleSheet(
            "QPushButton{background: rgba(130,255,30,0.18); color:#aaff66;"
            " border:none; border-radius:6px; padding:8px 14px; font-size:12px;"
            " font-weight:bold;} QPushButton:hover{background: rgba(130,255,30,0.32);}")
        self._pull_btn.clicked.connect(self._confirm_pull_restart)
        _act.addWidget(self._pull_btn)
        root.addLayout(_act)

        # 次操作：上传 / 扫描 / 保存
        _act2 = QHBoxLayout()
        self._upload_btn = HintButton("上传日志")
        self._upload_btn.setStyleSheet(_devsync_aux_btn_style("#ffd778"))
        self._upload_btn.clicked.connect(self._confirm_upload)
        _act2.addWidget(self._upload_btn)
        self._scan_btn = HintButton("重新扫描局域网")
        self._scan_btn.setStyleSheet(_devsync_aux_btn_style("#c9a8ff"))
        self._scan_btn.clicked.connect(self._on_scan)
        _act2.addWidget(self._scan_btn)
        self._save_btn = HintButton("保存配置")
        self._save_btn.setStyleSheet(_devsync_aux_btn_style("#9fb0c4"))
        self._save_btn.clicked.connect(self._on_save)
        _act2.addWidget(self._save_btn)
        root.addLayout(_act2)

        # 关闭
        self._close_btn = HintButton("关闭")
        self._close_btn.setStyleSheet(_devsync_aux_btn_style("#dfe6f0"))
        self._close_btn.clicked.connect(self._overlay.open_layout)
        _hb = QHBoxLayout()
        _hb.addStretch(1)
        _hb.addWidget(self._close_btn)
        root.addLayout(_hb)

    # -- 路径解析 ---------------------------------------------------------
    @staticmethod
    def _resolve_base() -> str:
        """配置/外部源码所在基目录：冻结=exe 目录；开发=含 run.py 的项目根。

        与模块级 ``_devsync_base_dir()`` 同一套逻辑，委托过去避免两处漂移。
        """
        return _devsync_base_dir()

    def _cfg_path(self) -> str:
        return os.path.join(self._base, "sync_config.json")

    def _load_cfg(self) -> dict:
        try:
            with open(self._cfg_path(), "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:  # noqa: BLE001
            return {}

    def _save_cfg(self, cfg: dict) -> None:
        with open(self._cfg_path(), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)

    def _local_ext_src(self, cfg: dict) -> str:
        v = cfg.get("local_ext_src")
        if v:
            return v
        return os.path.join(self._base, "_ext", "src")

    def _logs_dir(self) -> str:
        return os.path.join(self._base, "logs")

    def _set_status(self, msg: str, ok: bool) -> None:
        try:
            self._status_lbl.setText(msg)
            self._status_lbl.setStyleSheet(
                ("color:#aaff66; font-size:12px;" if ok
                 else "color:#ff9a9a; font-size:12px;"))
        except Exception:  # noqa: BLE001
            pass

    # -- 动作 -------------------------------------------------------------
    def _on_test(self) -> None:
        share = self._share_edit.text().strip()
        if not share:
            self._set_status("请先填写笔记本共享路径", False)
            return
        ok = os.path.isdir(share)
        self._set_status(
            ("✅ 可访问：" + share) if ok else ("❌ 无法访问：" + share), ok)

    def _confirm_pull_restart(self) -> None:
        """防误触二次确认：拉取源码并重启会关闭当前实例。"""
        share = self._share_edit.text().strip() or self._load_cfg().get(
            "laptop_share", "")
        if not share:
            self._set_status("笔记本共享为空，先填或点「测试连接」", False)
            return
        src = os.path.join(share, "src")
        if not os.path.isdir(src):
            self._set_status("❌ 笔记本共享下找不到 src 目录：" + src, False)
            return
        ans = QMessageBox.question(
            self, "确认拉取并重启",
            "即将从笔记本拉取源码并重启凝冰结魄加载新版本。\n\n"
            "当前实例会退出，重启后生效。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            self._set_status("已取消拉取并重启", True)
            return
        self._on_pull_restart()

    def _set_busy(self, busy: bool) -> None:
        """拉取/重启期间禁用所有按钮：防连点，也让用户看清「正在忙」。"""
        for _b in (self._pull_btn, self._upload_btn, self._scan_btn,
                   self._save_btn, self._test_btn, self._close_btn):
            try:
                _b.setEnabled(not busy)
            except Exception:  # noqa: BLE001
                pass

    def _on_pull_restart(self) -> None:
        """启动后台拉取（robocopy → 编译校验 → 原子切换 → 重启）。

        2026-09-01：整段搬进 ``_DevSyncPullWorker``。改前这些步骤全在 Qt 主线程
        同步跑：robocopy 走 SMB 拉 21MB / 842 个文件（掌机 WiFi 数十秒，最坏
        120s 超时）+ 全量 py_compile（96 个 .py）+ rmtree/rename——期间事件循环
        停摆、窗口不重绘、不响应手柄键盘，Windows 直接标「无响应」，这就是掌机上
        「点一下就卡住、然后重启不了」的根因。
        """
        cfg = self._load_cfg()
        share = self._share_edit.text().strip() or cfg.get("laptop_share", "")
        if not share:
            self._set_status("笔记本共享为空，先填或点「测试连接」", False)
            return
        src = os.path.join(share, "src")
        dst = self._local_ext_src(cfg)
        if not os.path.isdir(src):
            self._set_status("❌ 笔记本共享下找不到 src 目录：" + src, False)
            return
        if self._pull_worker is not None and self._pull_worker.isRunning():
            return  # 正在拉，防重复点击
        self._set_busy(True)
        self._pull_worker = _DevSyncPullWorker(share, src, dst, self)
        self._pull_worker.progress.connect(self._on_pull_progress)
        self._pull_worker.swapped.connect(self._on_pull_swapped)
        self._pull_worker.failed.connect(self._on_pull_failed)
        self._pull_worker.finished.connect(self._on_pull_finished)
        self._pull_worker.start()

    def _on_pull_progress(self, msg: str) -> None:
        self._set_status("⏳ " + msg, True)

    def _on_pull_failed(self, msg: str) -> None:
        self._set_status("❌ " + msg, False)
        self._set_busy(False)

    def _on_pull_finished(self) -> None:
        self._pull_worker = None
        # 成功分支不恢复按钮：马上就要退出当前实例，恢复反而引诱用户点。

    def _on_pull_swapped(self, dst: str) -> None:
        """源码切换完成 → 记住共享路径 → 起新进程加载新源码。"""
        # 顺手把共享路径写进配置，下次打开不用再填。
        try:
            cfg = self._load_cfg()
            cfg["laptop_share"] = (self._share_edit.text().strip()
                                   or cfg.get("laptop_share", ""))
            self._save_cfg(cfg)
        except Exception:  # noqa: BLE001 - 存配置失败不该阻断重启
            pass
        self._set_status("✅ 源码已更新（" + dst + "），正在重启加载新版本…", True)
        # 留一帧让上面这句先画出来，再拉子进程（子进程起来后本进程就该退了）。
        QTimer.singleShot(300, self._relaunch)

    def _relaunch(self) -> None:
        """异步拉起重启子进程，确认存活后才退出当前实例。

        用 ``_start_async_restart``（QTimer 轮询，最长 8s，全程不阻塞）而不是
        旧的主线程 ``time.sleep(1.0)`` 版本：掌机冷启动偏慢，1 秒窗口既冻 UI
        又容易误判。
        """
        self._set_status("⏳ 正在启动新实例…", True)
        self._restart_watcher = _start_async_restart(
            self,
            on_ok=self._on_relaunch_ok,
            on_fail=self._on_relaunch_failed,
        )

    def _on_relaunch_ok(self) -> None:
        _quit_after_restart(self._app)

    def _on_relaunch_failed(self, msg: str) -> None:
        self._set_status("❌ " + msg, False)
        self._set_busy(False)
        try:
            QMessageBox.warning(self, "重启失败", msg)
        except Exception:  # noqa: BLE001
            pass

    def _confirm_upload(self) -> None:
        """防误触二次确认：上传日志会覆盖笔记本共享下的 logs。"""
        share = self._share_edit.text().strip() or self._load_cfg().get(
            "laptop_share", "")
        if not share:
            self._set_status("笔记本共享为空", False)
            return
        if not os.path.isdir(share):
            self._set_status("❌ 笔记本共享不可达：" + share, False)
            return
        dst = os.path.join(share, "logs")
        ans = QMessageBox.question(
            self, "确认上传日志",
            "即将把掌机本地 logs 上传到笔记本共享：\n\n" + dst + "\n\n是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            self._set_status("已取消上传日志", True)
            return
        self._on_upload()

    def _on_upload(self) -> None:
        """上传日志（后台线程跑 robocopy，不在主线程阻塞）。"""
        cfg = self._load_cfg()
        share = self._share_edit.text().strip() or cfg.get("laptop_share", "")
        if not share:
            self._set_status("笔记本共享为空", False)
            return
        dst = os.path.join(share, "logs")
        src = self._logs_dir()
        if not os.path.isdir(src):
            self._set_status("本地 logs 不存在", False)
            return
        if self._upload_worker is not None and self._upload_worker.isRunning():
            return  # 正在传，防重复点击
        self._upload_dst = dst
        self._set_busy(True)
        self._set_status("⏳ 正在上传日志…", True)
        self._upload_worker = _DevSyncTaskWorker(
            lambda: _devsync_robocopy(src, dst, ["/E", "/XO"]))
        self._upload_worker.done.connect(self._on_upload_done)
        self._upload_worker.failed.connect(self._on_upload_failed)
        self._upload_worker.start()

    def _on_upload_done(self, rc: int) -> None:
        self._set_busy(False)
        if rc >= 8:
            self._set_status("❌ 日志上传失败（" + str(rc) + "）", False)
            return
        self._set_status("✅ 日志已上传到 " + self._upload_dst, True)

    def _on_upload_failed(self, msg: str) -> None:
        self._set_busy(False)
        self._set_status("❌ 上传异常：" + msg, False)

    def _on_scan(self) -> None:
        if self._scan_thread is not None and self._scan_thread.isRunning():
            return  # 正在扫，防重复点击
        self._scan_btn.setEnabled(False)
        self._set_status("扫描局域网中…（最多约 15 秒）", True)
        self._scan_thread = _DevSyncLanScan()
        self._scan_thread.found.connect(self._on_scan_done)
        self._scan_thread.finished.connect(self._on_scan_finished)
        self._scan_thread.start()

    def _on_scan_done(self, hosts: list[str]) -> None:
        if not hosts:
            self._set_status("❌ 未发现 NBJ 共享（确认笔记本已开共享）", False)
            return
        self._share_edit.setText(hosts[0])
        self._set_status("✅ 发现共享：" + "，".join(hosts), True)

    def _on_scan_finished(self) -> None:
        self._scan_btn.setEnabled(True)
        self._scan_thread = None

    def _on_save(self) -> None:
        cfg = self._load_cfg()
        cfg["laptop_share"] = self._share_edit.text().strip()
        try:
            self._save_cfg(cfg)
            self._set_status("✅ 配置已保存", True)
        except Exception as exc:  # noqa: BLE001
            self._set_status("❌ 保存失败：" + str(exc), False)

    # -- 接口 -------------------------------------------------------------
    def refresh(self) -> None:
        cfg = self._load_cfg()
        self._share_edit.setText(cfg.get("laptop_share", ""))
        local = self._local_ext_src(cfg)
        self._local_lbl.setText(local)
        active = os.path.isdir(local)
        self._status_lbl.setText(
            "当前状态：外部源码已就位，下次重启生效"
            if active else "当前状态：使用冻结内置源码（尚未同步）")
        self._status_lbl.setStyleSheet("color:#aeb9c9; font-size:12px;")

    def nav_items(self) -> list[NavItem]:
        items: list[NavItem] = []
        items.append(NavItem(self._pull_btn, "action",
                             confirm=self._confirm_pull_restart, hint="A"))
        items.append(NavItem(self._upload_btn, "action",
                             confirm=self._confirm_upload, hint="Y"))
        items.append(NavItem(self._scan_btn, "action",
                             confirm=self._on_scan, hint="X"))
        items.append(NavItem(self._save_btn, "action",
                             confirm=self._on_save, hint="Y"))
        items.append(NavItem(self._close_btn, "action",
                             confirm=self._overlay.open_layout, hint="B"))
        return items


class EditOverlay(QWidget):
    """Full-window edit overlay — child of the HUD window, never a dialog.

    Mirrors the original ``$pnlEdit``: it hides the live cards and builds the
    editor inside the main window.  Sub-modes (layout / tile / procmon) are
    stacked; :meth:`nav_items` exposes the active one to the interaction
    controller so keyboard + gamepad drive it.
    """

    close_requested = pyqtSignal()

    def __init__(self, app: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._app = app
        self._mode: Optional[str] = None
        self.setObjectName("EditOverlay")
        # Background is intentionally transparent: the HUD's own card-tray
        # background (painted in _OverlayWindow.paintEvent via _paint_tray)
        # becomes the single source of truth for the editor backdrop.  This
        # merges the two previously separate scrims so colour / opacity /
        # border changes apply identically to the HUD and the editor.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._apply_window_border()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._stack = QStackedWidget(self)
        lay.addWidget(self._stack)
        self._layout_panel = LayoutEditPanel(app, self)
        self._tile_panel = TileEditPanel(app, self)
        self._procmon_panel = ProcMonEditPanel(app, self)
        self._button_panel = ButtonEditPanel(app, self)
        self._breathing_panel = BreathingEditPanel(app, self)
        self._freeze_panel = FreezeEditPanel(app, self)
        self._power_panel = PowerEditPanel(app, self)
        self._undervolt_panel = UndervoltEditPanel(app, self)
        self._perfmon_panel = PerfMonEditPanel(app, self)
        self._smt_panel = SmtEditPanel(app, self)
        self._fan_panel = FanEditPanel(app, self)
        self._rtss_panel = RtssEditPanel(app, self)
        self._card_color_panel = CardColorEditPanel(app, self)
        self._xinput_gate_panel = XInputGateEditPanel(app, self)
        self._devsync_panel = DevSyncEditPanel(app, self)
        self._layout_effect_panel = LayoutEffectPanel(app, self)
        # 主题与蒙版：依赖 breathing / layout_effect 面板（整体领走它们的
        # 设置卡复用），必须在这两者之后创建。
        self._theme_mask_panel = ThemeMaskPanel(
            app, self, self._breathing_panel, self._layout_effect_panel)
        self._ls_panel = LSEditPanel(app, self)
        self._powerplan_panel = PowerPlanEditPanel(app, self)
        self._stack.addWidget(self._layout_panel)
        self._stack.addWidget(self._tile_panel)
        self._stack.addWidget(self._procmon_panel)
        self._stack.addWidget(self._button_panel)
        self._stack.addWidget(self._breathing_panel)
        self._stack.addWidget(self._freeze_panel)
        self._stack.addWidget(self._power_panel)
        self._stack.addWidget(self._undervolt_panel)
        self._stack.addWidget(self._perfmon_panel)
        self._stack.addWidget(self._smt_panel)
        self._stack.addWidget(self._fan_panel)
        self._stack.addWidget(self._rtss_panel)
        self._stack.addWidget(self._ls_panel)
        self._stack.addWidget(self._powerplan_panel)
        self._stack.addWidget(self._card_color_panel)
        self._stack.addWidget(self._xinput_gate_panel)
        self._stack.addWidget(self._devsync_panel)
        self._stack.addWidget(self._layout_effect_panel)
        self._stack.addWidget(self._theme_mask_panel)

    # -- window border (neutral, never the bright theme accent) --------
    def _apply_window_border(self) -> None:
        """The editor has no own background: the HUD's _paint_tray() is the
        single backdrop.  We only keep a very faint border so the edit panel
        still reads as a distinct surface; the border radius matches the tray
        (14 px) so no double-corner is visible."""
        self.setStyleSheet(
            "EditOverlay { background: transparent; border: none; }"
        )

    # -- mode switching -------------------------------------------------
    def open_layout(self) -> None:
        # Leaving the button-style editor?  Collapse the HUD back to a single
        # page (it was expanded to all pages so the editor could list buttons
        # from every page).  Safe no-op when we weren't in button mode.
        if self._mode == "button":
            self._revert_editor_pages()
        self._mode = "layout"
        self._stack.setCurrentWidget(self._layout_panel)
        self._layout_panel.refresh()

    def open_xinput_gate(self) -> None:
        """打开手柄隔离设置面板。"""
        self._mode = "xinput_gate"
        self._stack.setCurrentWidget(self._xinput_gate_panel)
        self._xinput_gate_panel.refresh()

    def open_dev_sync(self) -> None:
        """打开开发同步（调试）面板。"""
        self._mode = "devsync"
        self._stack.setCurrentWidget(self._devsync_panel)
        self._devsync_panel.refresh()

    def _revert_editor_pages(self) -> None:
        """Undo the all-pages expansion done by :meth:`open_button`.

        Returns the HUD to showing only the current page and re-applies the
        active button style to it.  Duck-typed so we don't import the window
        class (avoids a circular import).  Note: ``self.parent()`` is the
        QStackedWidget that hosts this overlay, so we climb to the top-level
        window via ``self.window()``."""
        win = self.window()
        if win is None or not getattr(win, "_editor_all_pages", False):
            return
        win._editor_all_pages = False
        if hasattr(win, "rebuild"):
            try:
                win.rebuild()
            except Exception:
                pass

    def open_tile(self, extra_block: Any = None) -> None:
        self._mode = "tile"
        self._stack.setCurrentWidget(self._tile_panel)
        self._tile_panel.edit_target(extra_block)

    def open_procmon(self) -> None:
        self._mode = "procmon"
        self._stack.setCurrentWidget(self._procmon_panel)
        self._procmon_panel.refresh()

    def open_button(self) -> None:
        self._mode = "button"
        # Expand the HUD to instantiate EVERY page's panels (not just the
        # current one) so the button-style editor's list can show — and style —
        # buttons from cards on all pages.  The HUD sits behind the editor
        # overlay, so the extra pages are never seen.  We revert this in
        # :meth:`_revert_editor_pages` when leaving button mode.
        win = self.window()
        if win is not None and hasattr(win, "_editor_all_pages"):
            win._editor_all_pages = True
            if hasattr(win, "rebuild"):
                try:
                    # restyle=True: the freshly-built all-pages HUD starts from
                    # default (unstyled) buttons, so re-apply the active L2/L1
                    # styles — otherwise the preview list would show unstyled
                    # clones instead of the user's current button scheme.
                    win.rebuild(restyle=True, reload_tiles=False, heavy=False)
                except Exception:
                    pass
            # Lay out the (hidden) HUD synchronously BEFORE the editor measures
            # the buttons.  Without this, render_canvas() runs before Qt has
            # computed the panels' geometry and reads the default 640x480 unset
            # size — which makes the preview clones shrink to their text width
            # (the "预览按钮宽度不对" report).
            if hasattr(win, "_force_layout_pass"):
                try:
                    win._force_layout_pass()
                except Exception:
                    pass
        self._stack.setCurrentWidget(self._button_panel)
        self._button_panel.refresh()

    def open_breathing(self) -> None:
        self._mode = "breathing"
        self._stack.setCurrentWidget(self._breathing_panel)
        self._breathing_panel.refresh()

    def open_layout_effect(self) -> None:
        """打开「排版效果」子页：调整主页面卡片间距 / 外圈边距并实时套用。"""
        self._mode = "layout_effect"
        self._stack.setCurrentWidget(self._layout_effect_panel)
        self._layout_effect_panel.refresh()

    def open_theme_mask(self) -> None:
        """打开「主题与蒙版」子页：底板 / 单卡 / 编辑蒙版统一编辑，
        悬停设置卡在真实 HUD 上高亮对应区域 + mini-HUD 实时预览。"""
        self._mode = "theme_mask"
        self._stack.setCurrentWidget(self._theme_mask_panel)
        self._theme_mask_panel.refresh()

    def open_freeze(self) -> None:
        self._mode = "freeze"
        self._stack.setCurrentWidget(self._freeze_panel)
        self._freeze_panel.refresh()

    def open_power(self, token: str) -> None:
        self._mode = "power"
        self._stack.setCurrentWidget(self._power_panel)
        self._power_panel.edit_target(token)

    def open_undervolt(self, token: str) -> None:
        self._mode = "undervolt"
        self._stack.setCurrentWidget(self._undervolt_panel)
        self._undervolt_panel.edit_target(token)

    def open_ls(self, token: str) -> None:
        """打开 LS预设 编辑项（设置 LS 主程序路径）。"""
        self._mode = "ls"
        self._stack.setCurrentWidget(self._ls_panel)
        self._ls_panel.refresh()

    def open_powerplan(self, token: str) -> None:
        """打开 电源计划 编辑项（导入 / 移除用户 .pow + 卡片颜色）。"""
        self._mode = "powerplan"
        self._stack.setCurrentWidget(self._powerplan_panel)
        self._powerplan_panel.edit_target(token)

    def open_perfmon(self, token: str) -> None:
        self._mode = "perfmon"
        self._stack.setCurrentWidget(self._perfmon_panel)
        self._perfmon_panel.edit_target(token)

    def open_smt(self, token: str) -> None:
        self._mode = "smt"
        self._stack.setCurrentWidget(self._smt_panel)
        self._smt_panel.edit_target(token)

    def open_fan(self, token: str) -> None:
        self._mode = "fan"
        self._stack.setCurrentWidget(self._fan_panel)
        self._fan_panel.edit_target(token)

    def open_rtss(self, token: str) -> None:
        self._mode = "rtss"
        self._stack.setCurrentWidget(self._rtss_panel)
        self._rtss_panel.edit_target(token)

    def open_card_color(self, token: str) -> None:
        self._mode = "cardcolor"
        self._stack.setCurrentWidget(self._card_color_panel)
        self._card_color_panel.edit_target(token)

    def open_shortcut(self) -> None:
        """Open the standalone shortcut-button editor.  The editor instance
        (and its store/engine) lives on the main window, so we delegate to it
        via duck-typing — keeps EditOverlay free of a circular import."""
        win = self.window()
        if win is not None and hasattr(win, "_open_shortcut_editor"):
            win._open_shortcut_editor()

    def mode(self) -> Optional[str]:
        return self._mode

    def faq(self) -> Any:
        """返回主窗口的疑惑解答控制器（解耦：面板不直接持有窗口引用）。

        返回 ``None`` 表示主窗口未挂载 FAQ 模块。"""
        win = self.window()
        return getattr(win, "_faq", None)

    # -- nav ------------------------------------------------------------
    def nav_items(self) -> list[NavItem]:
        if self._mode == "layout":
            return self._layout_panel.nav_items()
        if self._mode == "tile":
            return self._tile_panel.nav_items()
        if self._mode == "procmon":
            return self._procmon_panel.nav_items()
        if self._mode == "button":
            return self._button_panel.nav_items()
        if self._mode == "breathing":
            return self._breathing_panel.nav_items()
        if self._mode == "freeze":
            return self._freeze_panel.nav_items()
        if self._mode == "power":
            return self._power_panel.nav_items()
        if self._mode == "undervolt":
            return self._undervolt_panel.nav_items()
        if self._mode == "perfmon":
            return self._perfmon_panel.nav_items()
        if self._mode == "smt":
            return self._smt_panel.nav_items()
        if self._mode == "fan":
            return self._fan_panel.nav_items()
        if self._mode == "rtss":
            return self._rtss_panel.nav_items()
        if self._mode == "ls":
            return self._ls_panel.nav_items()
        if self._mode == "powerplan":
            return self._powerplan_panel.nav_items()
        if self._mode == "xinput_gate":
            return self._xinput_gate_panel.nav_items()
        if self._mode == "devsync":
            return self._devsync_panel.nav_items()
        if self._mode == "layout_effect":
            return self._layout_effect_panel.nav_items()
        if self._mode == "theme_mask":
            return self._theme_mask_panel.nav_items()
        if self._mode == "cardcolor":
            return self._card_color_panel.nav_items()
        return []

    # -- 控制器 → 当前活动面板 的委托转发 ----------------------------------
    # InteractionController 只持有 EditOverlay 容器引用（self._edit_overlay），
    # 但 _edit_items / _toggle / _hint_set_input_mode / _set_sel 这些方法定义在
    # 各活动面板（LayoutEditPanel 等）上。容器必须转发，否则控制器 getattr 全部
    # 落空 → 手柄短按 Y 不进编辑项、X 不隐藏、设备互斥不切换、单光标清选中失效。
    def _edit_items(self, t: str) -> None:
        p = self._stack.currentWidget()
        fn = getattr(p, "_edit_items", None)
        if callable(fn):
            try:
                fn(t)
            except Exception:
                pass

    def _toggle(self, t: str) -> None:
        p = self._stack.currentWidget()
        fn = getattr(p, "_toggle", None)
        if callable(fn):
            try:
                fn(t)
            except Exception:
                pass

    def _hint_set_input_mode(self, mode: str) -> None:
        p = self._stack.currentWidget()
        fn = getattr(p, "_hint_set_input_mode", None)
        if callable(fn):
            try:
                fn(mode)
            except Exception:
                pass

    def _set_sel(self, t: "str | None") -> None:
        p = self._stack.currentWidget()
        fn = getattr(p, "_set_sel", None)
        if callable(fn):
            try:
                fn(t)
            except Exception:
                pass