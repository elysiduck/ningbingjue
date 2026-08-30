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
import os
import threading
from typing import Any, Callable, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QRectF, QPoint, QRect, QSize
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFileDialog,
    QFrame,
    QGraphicsBlurEffect,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
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
)

from ..core.color import THEME_NAMES, get_theme_accent
from ..core.color.rgb import Color
from ..core.config import Tile
from ..core.config import breath_speed_to_period
from .breathing import BreathingController
from .blur_util import blur_pixmap
from .button_edit import ButtonEditPanel
from .gamepad_hint import HintButton
from .interaction import NavItem
from .tile_button import TileButton
from .faq_popup import ClickableTitle
from ._scroll_style import apply_thin_scroll
from .panels import _card_mark_name

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
        # Add an individual tile inside this block.
        self._add_tile_btn = HintButton("新增磁贴")
        self._add_tile_btn.setStyleSheet(
            "QPushButton { background: rgba(130,255,30,0.14); color: #b6ff8a;"
            " border: none; border-radius: 7px;"
            " padding: 7px 12px 7px 20px; }")
        self._add_tile_btn.clicked.connect(self._add_tile)
        sf.addWidget(self._add_tile_btn)
        bl.addWidget(s_card)

        # --- Card: card colour (title + frame preset picker) ----------
        cc_card = self._card("卡片颜色", title_click=self._open_faq)
        ccf = cc_card.content()
        self._card_color_picker = CardColorPicker(
            self._app, self._overlay, "", on_change=self._refresh_card_color_preview)
        ccf.addWidget(self._card_color_picker)
        bl.addWidget(cc_card)

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
            " padding: 5px 10px 5px 20px; }")
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
            " padding: 5px 10px 5px 20px; }")
        self._icon_browse.clicked.connect(self._browse_icon)
        f_icon.addWidget(self._icon_browse)
        self._icon_clear = HintButton("用目标图标")
        self._icon_clear.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 6px;"
            " padding: 5px 10px 5px 20px; }")
        self._icon_clear.clicked.connect(self._clear_icon)
        f_icon.addWidget(self._icon_clear)
        tf.addLayout(f_icon)

        f_size = QHBoxLayout()
        f_size.addWidget(QLabel("尺寸"))
        self._size = QComboBox()
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
        self._name_color = QComboBox()
        for label, rgb in _NAME_COLORS:
            self._name_color.addItem(label, rgb)
        self._name_color.currentTextChanged.connect(self._apply_name_color)
        f_nc.addWidget(self._name_color, 1)
        tf.addLayout(f_nc)

        # Border colour override (default transparent = no border).
        f_bdr = QHBoxLayout()
        f_bdr.addWidget(QLabel("边框"))
        self._border_color = QLineEdit()
        self._border_color.setPlaceholderText("R,G,B 或留空=透明")
        self._border_color.editingFinished.connect(self._apply_border_color)
        f_bdr.addWidget(self._border_color, 1)
        tf.addLayout(f_bdr)

        # Background colour override (default transparent = no background).
        f_bg = QHBoxLayout()
        f_bg.addWidget(QLabel("背景"))
        self._bg_color = QLineEdit()
        self._bg_color.setPlaceholderText("R,G,B 或留空=透明")
        self._bg_color.editingFinished.connect(self._apply_bg_color)
        f_bg.addWidget(self._bg_color, 1)
        tf.addLayout(f_bg)

        self._del_tile_btn = HintButton("删除磁贴")
        self._del_tile_btn.setStyleSheet(
            "QPushButton { background: rgba(230,75,90,0.18); color: #ff9b9b;"
            " border: none; border-radius: 7px;"
            " padding: 6px 12px 6px 20px; }")
        self._del_tile_btn.clicked.connect(self._del_tile)
        tf.addWidget(self._del_tile_btn)
        bl.addWidget(t_card)

        # --- Footer ---
        foot = QHBoxLayout()
        self._del_block_btn = HintButton("删除区块")
        self._del_block_btn.setStyleSheet(
            "QPushButton { background: rgba(230,75,90,0.18); color: #ff9b9b;"
            " border: none; border-radius: 7px;"
            " padding: 7px 12px 7px 20px; }")
        self._del_block_btn.clicked.connect(self._del_block)
        self._save_btn = HintButton("保存")
        self._save_btn.setStyleSheet(
            "QPushButton { background: #82ff1e; color: #0c0f16; border: none;"
            " border-radius: 7px; padding: 7px 16px 7px 22px; font-weight: bold; }")
        self._save_btn.clicked.connect(self._save)
        self._cancel_btn = HintButton("取消")
        self._cancel_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.1); color: #dfe6f0;"
            " border: none; border-radius: 7px;"
            " padding: 7px 16px 7px 22px; }")
        self._cancel_btn.clicked.connect(self._cancel)
        foot.addWidget(self._del_block_btn)
        foot.addStretch(1)
        foot.addWidget(self._save_btn)
        foot.addWidget(self._cancel_btn)
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
        self._border_color.setText(getattr(t, "border_color", "") or "")
        self._bg_color.setText(getattr(t, "bg_color", "") or "")

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

    def _apply_border_color(self) -> None:
        if 0 <= self.sel < len(self.work):
            self.work[self.sel].border_color = self._border_color.text().strip()
            self._auto_save()

    def _apply_bg_color(self) -> None:
        if 0 <= self.sel < len(self.work):
            self.work[self.sel].bg_color = self._bg_color.text().strip()
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
        block = self.block
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
        items.append(NavItem(self._border_color, "input",
                             confirm=lambda w=self._border_color: w.setFocus()))
        items.append(NavItem(self._bg_color, "input",
                             confirm=lambda w=self._bg_color: w.setFocus()))
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
            " border-radius: 8px; font-size: 12px; }"
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
            " border: none; border-radius: 6px; padding: 6px 14px; font-size: 12px; }"
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
        diag_note.setStyleSheet("color: #aeb9c9; font-size: 12px;")
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
        note.setStyleSheet("color: #aeb9c9; font-size: 12px;")
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
        gm_note.setStyleSheet("color: #aeb9c9; font-size: 12px;")
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
        note.setStyleSheet("color: #aeb9c9; font-size: 12px;")
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
            pass
        win = getattr(self._app, "_window", None)
        if win is not None:
            try:
                win._refresh_all_accents()
            except Exception:
                pass
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
        desc.setStyleSheet("color: #aeb9c9; font-size: 12px;")
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

        # --- 卡片颜色 --------------------------------------------------
        cc_lbl = QLabel("卡片颜色")
        cc_lbl.setStyleSheet("color: #7fd1ff; font-weight: bold; font-size: 13px;")
        lay.addWidget(cc_lbl)
        self._card_color_picker = CardColorPicker(
            self._app, self._overlay, "undervolt")
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
        lbl.setStyleSheet("color: #cdd6e2; font-size: 12px;")
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
        val.setStyleSheet("color: #62e0a8; font-size: 12px;")
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
        lbl.setStyleSheet("color: #cdd6e2; font-size: 12px;")
        lbl.setFixedWidth(104)
        slider = _UVNoWheelSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 50)
        # 响应式：滑块占满剩余宽度，设 60px 下限避免被压到无法拖动。
        slider.setMinimumWidth(60)
        slider.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Fixed)
        slider.setStyleSheet(_UV_EDIT_SLIDER_STYLE)
        val = QLabel("0 mV")
        val.setStyleSheet("color: #62e0a8; font-size: 12px;")
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
        desc.setStyleSheet("color: #aeb9c9; font-size: 12px;")
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
        lbl.setStyleSheet("color: #cdd6e2; font-size: 12px;")
        lbl.setFixedWidth(150)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(min_val, max_val)
        # 响应式：窄窗口收缩到 60px 下限，宽窗口占满（与降压编辑项一致）。
        slider.setMinimumWidth(60)
        slider.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Fixed)
        slider.setStyleSheet(self._SLIDER_STYLE)
        val = QLabel(str(min_val))
        val.setStyleSheet("color: #ff9f43; font-size: 12px;")
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


class _WheelSlider(QSlider):
    """编辑项滑块：滚轮悬停即可调节（普通 QSlider 在 QScrollArea 内滚轮会被吞）。

    重写 wheelEvent：按 ``angleDelta().y()`` 正负以 singleStep 增减 value，并
    ``accept()`` 阻止事件冒泡到滚动区，从而滚轮只调滑块、不滚页面。拖动 /
    手柄方向键微调逻辑不受影响（仍走 valueChanged / nudge）。
    """

    def wheelEvent(self, ev: Any) -> None:  # noqa: N802 - Qt camelCase
        delta = ev.angleDelta().y()
        if delta == 0:
            ev.ignore()
            return
        step = self.singleStep() if delta > 0 else -self.singleStep()
        nv = max(self.minimum(), min(self.maximum(), self.value() + step))
        if nv != self.value():
            self.setValue(nv)
        ev.accept()


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
        desc.setStyleSheet("color: #aeb9c9; font-size: 12px;")
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
        rpm_lbl.setStyleSheet("color: #cdd6e2; font-size: 12px;")
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

        self._temp_src_combo = QComboBox()
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

        # --- 曲线拐点 --------------------------------------------------
        curve_title = QLabel("曲线拐点（温度 ℃ → 转速，0-255 占满速映射）")
        curve_title.setWordWrap(True)
        curve_title.setStyleSheet("color: #62e0a8; font-weight: bold; font-size: 13px;")
        lay.addWidget(curve_title)

        self._curve_container = QWidget()
        self._curve_layout = QVBoxLayout(self._curve_container)
        self._curve_layout.setSpacing(8)
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
        lbl.setStyleSheet("color: #cdd6e2; font-size: 12px;")
        lbl.setFixedWidth(88)
        slider = _WheelSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 255)
        slider.setSingleStep(5)
        # 响应式：窄窗口收缩到 60px 下限，宽窗口占满（与降压编辑项一致）。
        slider.setMinimumWidth(60)
        slider.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Fixed)
        slider.setStyleSheet(self._SLIDER_STYLE)
        val = QLabel("0")
        val.setStyleSheet("color: #62e0a8; font-size: 12px;")
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
        lbl.setStyleSheet("color: #cdd6e2; font-size: 12px;")
        lbl.setFixedWidth(88)
        slider = _WheelSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 10)
        # 响应式：窄窗口收缩到 60px 下限，宽窗口占满（与降压编辑项一致）。
        slider.setMinimumWidth(60)
        slider.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Fixed)
        slider.setStyleSheet(self._SLIDER_STYLE)
        val = QLabel("3")
        val.setStyleSheet("color: #62e0a8; font-size: 12px;")
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
        pts = list(blk.curve) if blk is not None else []
        for i, pt in enumerate(pts):
            row = QHBoxLayout()
            row.setSpacing(5)
            # 序号
            idx_lbl = QLabel(f"#{i + 1}")
            idx_lbl.setStyleSheet("color:#9aa6b8; font-size:12px;")
            idx_lbl.setFixedWidth(26)
            idx_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            # 温度（纯数字 + 独立单位标签）。SpinBox 加宽到 70px 并给数值右
            # padding，保证自带上下箭头按钮（约 18px）不与数字重叠 —— 修复
            # 掌机分数 DPI 下「数字被调整箭头遮挡」的 bug。保留箭头以便掌机
            # 点击微调；滚轮 / 方向键 / 直接点数字输入同样可用。
            temp = QSpinBox()
            temp.setRange(0, 110)
            temp.setMinimumHeight(30)
            temp.setFixedWidth(70)
            temp.setStyleSheet(
                "QSpinBox{background:rgba(255,255,255,0.08); color:#dfe6f0;"
                " border-radius:4px; padding:2px 20px 2px 6px; font-size:12px;}")
            temp.blockSignals(True)
            temp.setValue(int(pt["temp"]))
            temp.blockSignals(False)
            temp_unit = QLabel("℃")
            temp_unit.setStyleSheet("color:#9aa6b8; font-size:12px;")
            temp_unit.setFixedWidth(16)
            # 转速（0-255 占满速映射，纯数字 + 独立单位标签；同 temp 加宽防遮挡）
            pwm = QSpinBox()
            pwm.setRange(0, 255)
            pwm.setMinimumHeight(30)
            pwm.setFixedWidth(70)
            pwm.setStyleSheet(
                "QSpinBox{background:rgba(255,255,255,0.08); color:#dfe6f0;"
                " border-radius:4px; padding:2px 20px 2px 6px; font-size:12px;}")
            pwm.blockSignals(True)
            pwm.setValue(int(pt["pwm"]))
            pwm.blockSignals(False)
            pwm_unit = QLabel("%")
            pwm_unit.setStyleSheet("color:#9aa6b8; font-size:11px;")
            pwm_unit.setFixedWidth(16)
            # 删除
            rm = HintButton("−")
            rm.set_style("#e68080", False)
            rm.setMinimumHeight(30)
            rm.setFixedWidth(30)
            row.addWidget(idx_lbl)
            row.addWidget(temp)
            row.addWidget(temp_unit)
            row.addWidget(pwm)
            row.addWidget(pwm_unit)
            row.addWidget(rm)
            row.addStretch(1)
            self._curve_layout.addLayout(row)
            temp.valueChanged.connect(
                lambda _v, idx=i: self._on_curve_point(idx))
            pwm.valueChanged.connect(
                lambda _v, idx=i: self._on_curve_point(idx))
            rm.clicked.connect(lambda _=False, idx=i: self._on_remove_curve(idx))
            self._curve_rows.append((temp, pwm, rm))

    def _on_curve_point(self, idx: int) -> None:
        if not (0 <= idx < len(self._curve_rows)):
            return
        temp, pwm, _ = self._curve_rows[idx]
        blk = self._block()
        if blk is not None:
            blk.set_curve_point(idx, temp.value(), pwm.value())
        self._persist()

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
        blk.add_curve_point(new_temp, 255)
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
        self._restart_btn = HintButton("↻ 重启+")
        self._restart_btn.setStyleSheet(
            "QPushButton { background: rgba(255,170,60,0.16); color: #ffce8a;"
            " border: none; border-radius: 6px;"
            " padding: 4px 10px 4px 20px; font-size: 11px; font-weight: bold; }"
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
            " padding: 4px 8px; font-size: 11px; }"
            "QPushButton:hover { background: rgba(95,175,235,0.38); }")
        self._new_tile_btn.clicked.connect(self._new_tile_block)
        self._add_page_btn = HintButton("＋分页")
        self._add_page_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #b0bdd0;"
            " border: none; border-radius: 6px;"
            " padding: 4px 8px; font-size: 11px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.16); }")
        self._add_page_btn.clicked.connect(self._add_page_break)
        hdr_row.addWidget(self._new_tile_btn)
        hdr_row.addWidget(self._add_page_btn)
        root.addLayout(hdr_row)
        # Reflect the persisted alignment on the toggle label at open time.
        self._sync_align_btn()

        sub = QLabel("编辑项 配置 · ↑/↓ 页内调序 · ▲/▼ 跨页移动 · 每页最多 6 个")
        sub.setStyleSheet("color: #8b97aa; font-size: 10px;")
        root.addWidget(sub)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        apply_thin_scroll(scroll, always_on=False)
        self._list_inner = QWidget()
        self._list_lay = QVBoxLayout(self._list_inner)
        self._list_lay.setContentsMargins(0, 0, 0, 0)
        self._list_lay.setSpacing(2)
        scroll.setWidget(self._list_inner)
        root.addWidget(scroll, 1)

        # Button style editor — moved to a SEPARATE inline interface
        # (ButtonEditPanel) so it no longer clutters the layout editor.  This
        # entry button opens that interface (mirrors each functional card's
        # dedicated edit page).
        btn_style_hint = QLabel("按钮风格：进入独立编辑界面，可编辑颜色/边框/激活态并预览全部按钮")
        btn_style_hint.setStyleSheet("color: #8b97aa; font-size: 10px;")
        root.addWidget(btn_style_hint)
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
        foot = QHBoxLayout()
        foot.setSpacing(6)
        self._save_btn = HintButton("保存")
        self._cancel_btn = HintButton("取消")
        self._save_btn.setStyleSheet(
            "QPushButton { background: #82ff1e; color: #0c0f16; border: none;"
            " border-radius: 7px; padding: 7px 16px; font-weight: bold; }"
            "QPushButton:hover { background: #9dff4a; }")
        self._cancel_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.1); color: #dfe6f0;"
            " border: none; border-radius: 7px;"
            " padding: 7px 16px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.18); }")
        self._save_btn.clicked.connect(self._save)
        self._cancel_btn.clicked.connect(self._cancel)
        # 保存(Y)/取消(B) 手柄提示「常驻显示」：即便手柄导航未激活也亮着，
        # 方便用户随时知道可用按键（布局编辑页的核心动作按钮）。
        self._save_btn.set_gamepad_hint("Y")
        self._cancel_btn.set_gamepad_hint("B")
        self._save_btn.set_hint_always_visible(True)
        self._cancel_btn.set_hint_always_visible(True)
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
        foot.addWidget(self._breath_btn)
        foot.addWidget(self._shortcut_edit_btn)
        foot.addWidget(self._xinput_gate_btn)
        foot.addStretch(1)
        foot.addWidget(self._save_btn)
        foot.addWidget(self._cancel_btn)
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
        self._list_lay.addStretch(1)

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
        card.setStyleSheet(
            "QFrame { background: transparent; border-radius: 6px;"
            " border: none; }")
        lay = QHBoxLayout(card)
        lay.setContentsMargins(6, 1, 6, 1)
        lay.setSpacing(3)
        # 卡片列表标题颜色：服从每张卡自己设定的卡片色（与主页一致），
        # 而非统一呼吸灯色。
        cfg = self._app.config_manager.config
        csv = cfg.card_colors.get(t) if cfg else None
        if csv:
            accent = _hex(_parse_color(csv))
        elif base_type == "tile":
            accent = _hex(get_theme_accent())
        else:
            accent = _hex(block.color if block else None)
        name = QLabel(_card_mark_name(display_name))
        name.setStyleSheet(f"color: {accent}; font-weight: bold; font-size: 12px;")
        lay.addWidget(name, 1)

        # 行内按钮排布（按需求固定顺序）：
        #   卡片编辑(编辑项) · 页内上移(↑) · 页内下移(↓) · ▲移动到上一页 · ▼移动到下一页
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
        # Edit-button (leftmost): every functional card now opens a dedicated
        # edit page.  procmon / tile / freeze have rich pages; every other
        # card opens the generic 卡片颜色 editor.
        edit = QPushButton("编辑项")
        edit.setFixedSize(56, 26)
        edit.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.06); color: #b0bdd0;"
            " border: none; border-radius: 4px;"
            " font-size: 11px; padding: 0px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.14); }")
        edit.setToolTip("卡片编辑：打开该卡片/区块的专属编辑页")
        edit.clicked.connect(lambda _=False, tt=t: self._edit_items(tt))
        lay.addWidget(edit)
        up.clicked.connect(lambda _=False, p=pos: self._move(p, -1))
        down.clicked.connect(lambda _=False, p=pos: self._move(p, 1))
        prev_page.clicked.connect(lambda _=False, p=pos: self._move_to_prev_page(p))
        next_page.clicked.connect(lambda _=False, p=pos: self._move_to_next_page(p))
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
        card._edit = edit
        return card

    def _move(self, pos: int, delta: int) -> None:
        """页内上移 / 页内下移：仅在本页内与相邻卡片交换位置，
        不允许跨过本页边界（要跨页请用 ▲/▼ 按钮）。"""
        order, breaks = pg_move_in_page(
            self.order, self._page_breaks, PAGE_CAP, pos, delta)
        if order is self.order:
            return  # 被页边界挡住，未改动
        self.order = order
        self._render_rows()

    def _move_to_prev_page(self, pos: int) -> None:
        """▲ 移动到上一页：把该卡片挪到上一页末尾。

        前提：存在上一页。不封顶（PAGE_CAP=None）后不再有容量上限限制，
        所以唯一失败原因是「没有上一页」。"""
        res = pg_move_to_prev_page(
            self.order, self._page_breaks, PAGE_CAP, pos)
        if res is None:
            self._toast("已在第 1 页，无法再上移", success=False)
            return
        self.order, self._page_breaks = res
        self._normalize_self_breaks()   # 即时清掉可能产生的空页断点
        self._render_rows()
        seg = self._page_index_of(pos)
        pi = seg[0] if seg else 1
        self._toast(f"已移动到第 {pi + 1} 页")

    def _move_to_next_page(self, pos: int) -> None:
        """▼ 移动到下一页：把该卡片挪到下一页开头。

        前提：存在下一页。不封顶（PAGE_CAP=None）后不再有容量上限限制，
        所以唯一失败原因是「没有下一页」。"""
        res = pg_move_to_next_page(
            self.order, self._page_breaks, PAGE_CAP, pos)
        if res is None:
            self._toast("已在最后一页，无法再下移", success=False)
            return
        self.order, self._page_breaks = res
        self._normalize_self_breaks()   # 即时清掉可能产生的空页断点
        self._render_rows()
        seg = self._page_index_of(pos)
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
        cfg = self._app.config_manager.config
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
        """Persist any pending edits and fully restart the program.

        Strategy (reliable on Windows, including the frozen windowed/UAC-admin
        build):

          1. Spawn a brand-new instance of the exact same launch command and
             **wait for ``CreateProcess`` to succeed** — only then do we tear
             down the current process.  This guarantees the user always ends up
             with a running instance, instead of the old "closes but never
             restarts" failure where an in-place ``os.execl`` silently failed
             (or the relaunched copy fell into headless self-test and exited).

          2. ``os.execl`` is kept only as a last-ditch fallback if the spawn
             itself fails.

        Correctness notes:
          * ``--gui`` is forced into the command so the relaunched copy always
            opens the HUD; without it a frozen copy would run the headless
            engine self-test and immediately exit (looking like "no restart").
          * The child inherits ``os.environ`` (notably ``NINGBING_ROOT`` set by
            ``frozen_launcher``) and ``sys.argv`` (e.g. ``--root``), so it finds
            the same layout/config as this instance.
          * ``CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`` detaches the child
            from this process so it survives our exit and (in dev/console mode)
            avoids a flashing black console.
        """
        # 1. Save whatever is in flight so a restart never loses the user's work.
        try:
            self._app.save_all()
        except Exception:  # noqa: BLE001
            pass
        # 2. Hide the editor so the teardown looks clean.
        try:
            self._overlay.close_requested.emit()
        except Exception:  # noqa: BLE001
            pass

        import os
        import sys

        # 3. Rebuild the exact command that started us, forcing GUI mode.
        if getattr(sys, "frozen", False):
            # Packaged / frozen build: re-run the same executable.
            cmd = [sys.executable, *sys.argv[1:]]
            gui_idx = 1
        else:
            # Normal Python launch: python <abs script> <original args...>
            script = os.path.abspath(sys.argv[0])
            cmd = [sys.executable, script, *sys.argv[1:]]
            gui_idx = 2
        if "--gui" not in cmd:
            cmd.insert(gui_idx, "--gui")

        import subprocess

        # 4. Spawn the new instance FIRST (CreateProcess must succeed), then
        #    quit this one.  This is the reliable half of the restart.
        spawned = False
        try:
            subprocess.Popen(
                cmd,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW,
            )
            spawned = True
        except Exception:  # noqa: BLE001
            # Last-ditch: try in-place replacement (does not return on success).
            try:
                os.execl(sys.executable, sys.executable, *cmd[1:])
            except Exception:  # noqa: BLE001
                pass

        # 5. Tear down the current instance so only the freshly spawned one
        #    remains.  Quit the Qt loop; if that is somehow unavailable, force
        #    an immediate exit so we never leave a zombie with no HUD.
        try:
            from PyQt6.QtWidgets import QApplication

            qapp = QApplication.instance()
            if qapp is not None:
                qapp.quit()
                return
        except Exception:  # noqa: BLE001
            pass
        # Reached only if there is no live QApplication: bail out hard.
        if not spawned:
            try:
                os._exit(0)
            except Exception:  # noqa: BLE001
                pass

    def nav_items(self) -> list[NavItem]:
        items: list[NavItem] = []
        # Collect card rows in visual (top-to-bottom) order to assign matrix
        # rows deterministically.
        cards: list[QFrame] = []
        for idx in range(self._list_lay.count()):
            card = self._list_lay.itemAt(idx).widget()
            if card is not None and hasattr(card, "_up"):
                cards.append(card)
        n = len(cards)
        # Walk actual layout children; skip separators (no _up attr).
        for k, card in enumerate(cards):
            t = getattr(card, "_type", "")
            pos = getattr(card, "_pos", k)  # 真正的 order 索引
            r = k + 1  # 矩阵行：第 0 行留给页眉，卡片占 1..n
            # 卡片编辑（编辑项）最左 → 列 0；其后 ↑/↓/▲/▼ 依次列 1..4。
            if card._edit is not None:
                items.append(NavItem(card._edit, "action",
                                     confirm=lambda tt=t: self._edit_items(tt),
                                     hint="A", row=r, col=0))
            items.append(NavItem(card._up, "action",
                                 confirm=lambda p=pos: self._move(p, -1),
                                 row=r, col=1))
            items.append(NavItem(card._down, "action",
                                 confirm=lambda p=pos: self._move(p, 1),
                                 row=r, col=2))
            # 跨页三角形按钮（上一页 / 下一页），禁用时仍登记以便导航焦点一致。
            items.append(NavItem(card._prev, "action",
                                 confirm=lambda p=pos: self._move_to_prev_page(p),
                                 row=r, col=3))
            items.append(NavItem(card._next, "action",
                                 confirm=lambda p=pos: self._move_to_next_page(p),
                                 row=r, col=4))
        # 页眉/页脚切换按钮 = 矩阵第 0 行（列号与卡片按钮槽对齐：
        # 0=按钮风格编辑, 1=呼吸灯, 2=贴屏切换, 3=+ 磁贴, 4=＋分页, 5=重启+）。
        items.append(NavItem(self._btn_style_open, "action",
                             confirm=self._overlay.open_button, hint="A",
                             row=0, col=0))
        items.append(NavItem(self._breath_btn, "action",
                             confirm=self._overlay.open_breathing, hint="A",
                             row=0, col=1))
        items.append(NavItem(self._align_btn, "action",
                             confirm=self._toggle_window_align, hint="A",
                             row=0, col=2))
        items.append(NavItem(self._new_tile_btn, "action",
                             confirm=self._new_tile_block, hint="A",
                             row=0, col=3))
        items.append(NavItem(self._add_page_btn, "action",
                             confirm=self._add_page_break, row=0, col=4))
        items.append(NavItem(self._restart_btn, "action",
                             confirm=self._restart, hint="R", row=0, col=5))
        # 页脚按钮 = 矩阵末行 n+1（列 0=保存, 1=取消）。
        frow = n + 1
        items.append(NavItem(self._save_btn, "action",
                             confirm=self._save, hint="Y", row=frow, col=0))
        items.append(NavItem(self._cancel_btn, "action",
                             confirm=self._cancel, hint="B", row=frow, col=1))
        # 快捷按钮编辑 — 矩阵末行第 2 列（与保存/取消同处底部 foot 行，
        # 左右方向键可达：保存 ← 取消 ← 快捷按钮编辑）。
        # 注意：此处不再下发 hint（"A" 提示已由按钮 set_hint_suppressed 取消），
        # 仅保留导航可达性（方向键 + 确认触发 open_shortcut）。
        items.append(NavItem(self._shortcut_edit_btn, "action",
                             confirm=self._overlay.open_shortcut,
                             row=frow, col=2))
        # 手柄隔离 — 矩阵末行第 3 列
        if hasattr(self, "_xinput_gate_btn"):
            items.append(NavItem(self._xinput_gate_btn, "action",
                                 confirm=self._overlay.open_xinput_gate,
                                 row=frow, col=3))
        return items


# --------------------------------------------------------------------------
# In-window overlay that hosts the three sub-modes
# --------------------------------------------------------------------------
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
class _BreathPreview(QFrame):
    """A small strip that animates a breathing glow using its own
    ``BreathingController`` so the user sees 时间间隔/颜色/最大亮度 live,
    even though the real HUD is hidden behind the edit overlay."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._alpha = 1.0
        self._color = QColor(95, 175, 235)
        self.setMinimumHeight(48)
        self.setStyleSheet(
            "QFrame { background: #0e1117;"
            " border: 1px solid rgba(255,255,255,0.08);"
            " border-radius: 8px; }")

    def set_alpha(self, a: float) -> None:
        self._alpha = max(0.0, min(1.0, a))

    def set_color(self, c: QColor) -> None:
        if c is not None:
            self._color = c

    def paintEvent(self, _ev: Any) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        a = self._alpha
        glow = self._color
        grad = QLinearGradient(0, 0, w, 0)
        grad.setColorAt(0.0, QColor(glow.red(), glow.green(), glow.blue(), 0))
        grad.setColorAt(0.5, QColor(glow.red(), glow.green(), glow.blue(), int(225 * a)))
        grad.setColorAt(1.0, QColor(glow.red(), glow.green(), glow.blue(), 0))
        p.fillRect(0, 0, w, h, grad)
        p.end()


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
    """Customize the breathing-light glow: full period (seconds), glow colour
    (or follow the theme accent), and peak brightness.  Mirrors the other
    sub-page editors (button / tile / procmon) hosted in the edit overlay."""

    # Preset glow colours (reuse the name-colour palette).  Stored as "rrggbb".
    _PRESETS = [
        ("雪白", "255,255,255"), ("樱粉", "255,170,200"), ("冰蓝", "170,210,255"),
        ("薄荷", "180,255,180"), ("琥珀", "255,220,150"), ("柠黄", "245,215,75"),
        ("青碧", "45,215,175"), ("紫罗兰", "175,130,235"),
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
        self._period_s = 2.36
        self._max = 1.0
        self._color_hex = ""  # "" => follow theme accent
        self._swatches: list[QFrame] = []
        # -- card tray background (customizable) -----------------------
        self._tray_color_hex = "0e1117"
        self._tray_opacity = 86
        self._tray_border = True
        self._tray_blur = 18
        self._tray_swatches: list[QFrame] = []

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

        # ---- live preview ----------------------------------------------
        pv = _BreathPreview(self)
        root.addWidget(pv)
        self._preview = pv

        # ---- period (seconds) ------------------------------------------
        card = _EditCard("呼吸时间间隔", self)
        cf = card.content()
        prow = QHBoxLayout()
        self._period_slider = QSlider(Qt.Orientation.Horizontal)
        self._period_slider.setRange(5, 80)  # 0.5s .. 8.0s (tenths)
        self._period_slider.setValue(24)
        self._period_slider.setStyleSheet(
            "QSlider { border: none; background: transparent; }"
            "QSlider::groove:horizontal { border: none; height: 6px;"
            " background: rgba(255,255,255,0.12); border-radius: 3px; }"
            "QSlider::handle:horizontal { border: none; background: #7fd1ff;"
            " width: 12px; height: 12px; margin: -3px 0; border-radius: 6px; }"
            "QSlider::sub-page:horizontal { background: #7fd1ff; border-radius: 3px; }")
        self._period_val = QLabel("2.4 s")
        self._period_val.setStyleSheet("color: #dfe6f0; font-size: 11px;")
        self._period_val.setFixedWidth(46)
        self._period_val.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._period_slider.valueChanged.connect(self._on_period)
        prow.addWidget(self._period_slider, 1)
        prow.addWidget(self._period_val)
        cf.addLayout(prow)
        cf.addWidget(QLabel("一个完整呼吸（最暗→最亮→最暗）的时长"))
        root.addWidget(card)

        # ---- colour ----------------------------------------------------
        ccard = _EditCard("呼吸灯颜色", self)
        ccf = ccard.content()
        self._sw_row = QHBoxLayout()
        self._sw_row.setSpacing(5)
        self._swatches = []
        for label, rgb in self._PRESETS:
            sw = QFrame()
            sw.setFixedSize(34, 26)
            sw._hex = self._rgb_to_hex(rgb)
            sw._label = label
            sw.setStyleSheet(self._sw_style(False, sw._hex))
            sw.setCursor(Qt.CursorShape.PointingHandCursor)
            sw.setToolTip(label)
            sw.mousePressEvent = lambda _e, hx=sw._hex: self._pick_color(hx)  # type: ignore[assignment]
            self._sw_row.addWidget(sw)
            self._swatches.append(sw)
        self._sw_row.addStretch(1)
        ccf.addLayout(self._sw_row)

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
        ccf.addLayout(brow)
        ccf.addWidget(QLabel("留空则使用主题强调色（与之前一致）"))
        root.addWidget(ccard)

        # ---- max brightness --------------------------------------------
        mcard = _EditCard("最大亮度", self)
        mcf = mcard.content()
        mrow = QHBoxLayout()
        self._max_slider = QSlider(Qt.Orientation.Horizontal)
        self._max_slider.setRange(25, 100)  # percent
        self._max_slider.setValue(100)
        self._max_slider.setStyleSheet(
            "QSlider { border: none; background: transparent; }"
            "QSlider::groove:horizontal { border: none; height: 6px;"
            " background: rgba(255,255,255,0.12); border-radius: 3px; }"
            "QSlider::handle:horizontal { border: none; background: #7fd1ff;"
            " width: 12px; height: 12px; margin: -3px 0; border-radius: 6px; }"
            "QSlider::sub-page:horizontal { background: #7fd1ff; border-radius: 3px; }")
        self._max_val = QLabel("100%")
        self._max_val.setStyleSheet("color: #dfe6f0; font-size: 11px;")
        self._max_val.setFixedWidth(46)
        self._max_val.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._max_slider.valueChanged.connect(self._on_max)
        mrow.addWidget(self._max_slider, 1)
        mrow.addWidget(self._max_val)
        mcf.addLayout(mrow)
        mcf.addWidget(QLabel("峰值辉光强度（最暗值固定，保持呼吸感）"))
        root.addWidget(mcard)

        # ---- card tray background (customizable) ---------------------
        tcard = _EditCard("卡片区背景", self)
        tcf = tcard.content()
        # live preview
        tpv = _TrayPreview(self)
        tcf.addWidget(tpv)
        self._tray_preview = tpv
        # colour swatches + custom / border toggle on one compact row
        self._tray_sw_row = QHBoxLayout()
        self._tray_sw_row.setSpacing(4)
        for label, rgb in self._PRESETS:
            sw = QFrame()
            sw.setFixedSize(28, 22)
            sw._hex = self._rgb_to_hex(rgb)
            sw._label = label
            sw.setStyleSheet(self._sw_style_tray(False, sw._hex))
            sw.setCursor(Qt.CursorShape.PointingHandCursor)
            sw.setToolTip(label)
            sw.mousePressEvent = lambda _e, hx=sw._hex: self._pick_tray_color(hx)  # type: ignore[assignment]
            self._tray_sw_row.addWidget(sw)
            self._tray_swatches.append(sw)
        self._tray_custom_btn = QPushButton("自定义颜色…")
        self._tray_custom_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 6px;"
            " padding: 4px 8px; font-size: 10px; }")
        self._tray_custom_btn.clicked.connect(self._pick_tray_custom)
        self._tray_sw_row.addWidget(self._tray_custom_btn)
        self._tray_sw_row.addStretch(1)
        tcf.addLayout(self._tray_sw_row)
        # opacity slider
        orow = QHBoxLayout()
        self._tray_opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._tray_opacity_slider.setRange(0, 100)
        self._tray_opacity_slider.setValue(86)
        self._tray_opacity_slider.setStyleSheet(
            "QSlider { border: none; background: transparent; }"
            "QSlider::groove:horizontal { border: none; height: 6px;"
            " background: rgba(255,255,255,0.12); border-radius: 3px; }"
            "QSlider::handle:horizontal { border: none; background: #7fd1ff;"
            " width: 12px; height: 12px; margin: -3px 0; border-radius: 6px; }"
            "QSlider::sub-page:horizontal { background: #7fd1ff; border-radius: 3px; }")
        self._tray_opacity_val = QLabel("86%")
        self._tray_opacity_val.setStyleSheet("color: #dfe6f0; font-size: 11px;")
        self._tray_opacity_val.setFixedWidth(46)
        self._tray_opacity_val.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._tray_opacity_slider.valueChanged.connect(self._on_tray_opacity)
        orow.addWidget(self._tray_opacity_slider, 1)
        orow.addWidget(self._tray_opacity_val)
        tcf.addLayout(orow)
        # blur slider (frosted desktop backdrop)
        brow = QHBoxLayout()
        self._tray_blur_slider = QSlider(Qt.Orientation.Horizontal)
        self._tray_blur_slider.setRange(0, 50)
        self._tray_blur_slider.setValue(18)
        self._tray_blur_slider.setStyleSheet(
            "QSlider { border: none; background: transparent; }"
            "QSlider::groove:horizontal { border: none; height: 6px;"
            " background: rgba(255,255,255,0.12); border-radius: 3px; }"
            "QSlider::handle:horizontal { border: none; background: #7fd1ff;"
            " width: 12px; height: 12px; margin: -3px 0; border-radius: 6px; }"
            "QSlider::sub-page:horizontal { background: #7fd1ff; border-radius: 3px; }")
        self._tray_blur_val = QLabel("18px")
        self._tray_blur_val.setStyleSheet("color: #dfe6f0; font-size: 11px;")
        self._tray_blur_val.setFixedWidth(46)
        self._tray_blur_val.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._tray_blur_slider.valueChanged.connect(self._on_tray_blur)
        brow.addWidget(self._tray_blur_slider, 1)
        brow.addWidget(self._tray_blur_val)
        tcf.addLayout(brow)
        trow2 = QHBoxLayout()
        trow2.addWidget(QLabel("背景透明度（0% = 完全透明）"), 1)
        # border toggle
        self._tray_border_btn = QPushButton("边框：开")
        self._tray_border_btn.setCheckable(True)
        self._tray_border_btn.setChecked(True)
        self._tray_border_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #dfe6f0;"
            " border: none; border-radius: 6px;"
            " padding: 5px 10px; font-size: 11px; }"
            "QPushButton:checked { background: rgba(127,209,255,0.28);"
            " color: #bfe8ff; border: none; }")
        self._tray_border_btn.clicked.connect(self._toggle_tray_border)
        trow2.addWidget(self._tray_border_btn)
        tcf.addLayout(trow2)
        root.addWidget(tcard)

        root.addStretch(1)

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

        # ---- live preview animation (own controller) ------------------
        self._preview_breath = BreathingController(
            period_s=self._period_s, max_brightness=self._max)
        self._preview_breath.on_scanline = lambda a: (
            self._preview.set_alpha(a), self._preview.update())
        self._preview_breath.start_scanline()

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

    def _sw_style_tray(self, selected: bool, hex_color: str) -> str:
        return (f"QFrame {{ background-color: #{hex_color}; border: none;"
                f" border-radius: 5px; }}")

    def _accent_fallback(self) -> QColor:
        win = getattr(self._app, "_window", None)
        acc = getattr(win, "_accent", None)
        if isinstance(acc, QColor):
            return acc
        return QColor(95, 175, 235)

    def _apply_preview_color(self) -> None:
        if self._color_hex:
            try:
                self._preview.set_color(QColor("#" + self._color_hex))
                self._preview.update()
                return
            except Exception:
                pass
        self._preview.set_color(self._accent_fallback())
        self._preview.update()

    def _push_live(self) -> None:
        """Push the current in-editor breathing/tray settings to the live HUD
        and this window's outer border, so the neon frame tracks the chosen
        breathing colour *in real time* — before 保存.  Persisted only on
        _save; this is preview-only."""
        try:
            win = getattr(self._app, "_window", None)
            if win is not None:
                win.apply_breath(self._period_s, self._max, self._color_hex)
                win.apply_tray(self._tray_color_hex, self._tray_opacity,
                               self._tray_border, self._tray_blur)
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
        self._preview_breath.set_period(self._period_s)

    def _on_max(self, val: int) -> None:
        self._max = val / 100.0
        self._max_val.setText(f"{val}%")
        self._preview_breath.set_max_brightness(self._max)

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
            self._color_hex = hexv
            self._follow_btn.setChecked(False)
            self._highlight(hexv)
            self._apply_preview_color()
            self._push_live()

    def _follow_theme(self) -> None:
        if self._follow_btn.isChecked():
            self._color_hex = ""
            self._highlight("")
            self._apply_preview_color()
            self._push_live()

    # -- card tray handlers --------------------------------------------
    def _on_tray_opacity(self, val: int) -> None:
        self._tray_opacity = val
        self._tray_opacity_val.setText(f"{val}%")
        self._apply_tray_preview()

    def _on_tray_blur(self, val: int) -> None:
        self._tray_blur = max(0, min(50, int(val)))
        self._tray_blur_val.setText(f"{self._tray_blur}px")
        self._apply_tray_preview()

    def _pick_tray_color(self, hexv: str) -> None:
        self._tray_color_hex = hexv
        self._highlight_tray(hexv)
        self._apply_tray_preview()

    def _pick_tray_custom(self) -> None:
        dlg = QColorDialog()
        if self._tray_color_hex:
            try:
                dlg.setCurrentColor(QColor("#" + self._tray_color_hex))
            except Exception:
                pass
        if dlg.exec() == QColorDialog.DialogCode.Accepted:
            c = dlg.currentColor()
            hexv = f"{c.red():02x}{c.green():02x}{c.blue():02x}"
            self._tray_color_hex = hexv
            self._highlight_tray(hexv)
            self._apply_tray_preview()

    def _toggle_tray_border(self) -> None:
        self._tray_border = self._tray_border_btn.isChecked()
        self._tray_border_btn.setText("边框：" + ("开" if self._tray_border else "关"))
        self._apply_tray_preview()

    def _highlight_tray(self, hexv: str) -> None:
        for sw in self._tray_swatches:
            sel = (getattr(sw, "_hex", "") == hexv)
            sw.setStyleSheet(self._sw_style_tray(sel, sw._hex))

    def _apply_tray_preview(self) -> None:
        try:
            c = (QColor("#" + self._tray_color_hex)
                 if self._tray_color_hex else QColor(14, 17, 23))
        except Exception:
            c = QColor(14, 17, 23)
        self._tray_preview.set_params(
            c, self._tray_opacity, self._tray_border, self._tray_blur)

    # -- load / commit -------------------------------------------------
    def refresh(self) -> None:
        cfg = self._app.config_manager.config
        period = getattr(cfg, "breath_period_s", None)
        if not period or period <= 0:
            speed = getattr(cfg, "breath_speed", 50)
            period = breath_speed_to_period(int(speed)) if speed else 2.36
        maxb = getattr(cfg, "breath_max_brightness", 1.0) or 1.0
        color = getattr(cfg, "breath_color", "") or ""
        self._period_s = float(period)
        self._max = float(maxb)
        self._color_hex = color
        # sync controls
        self._period_slider.blockSignals(True)
        self._max_slider.blockSignals(True)
        self._period_slider.setValue(max(5, min(80, int(round(self._period_s * 10)))))
        self._max_slider.setValue(max(25, min(100, int(round(self._max * 100)))))
        self._period_slider.blockSignals(False)
        self._max_slider.blockSignals(False)
        self._period_val.setText(f"{self._period_s:.1f} s")
        self._max_val.setText(f"{int(round(self._max * 100))}%")
        # colour UI
        self._follow_btn.setChecked(not self._color_hex)
        self._highlight(self._color_hex)
        # live preview
        self._preview_breath.set_period(self._period_s)
        self._preview_breath.set_max_brightness(self._max)
        self._apply_preview_color()
        # card tray background
        tray_color = getattr(cfg, "tray_color", "0e1117") or "0e1117"
        tray_opacity = int(getattr(cfg, "tray_opacity", 86) or 86)
        tray_border = bool(getattr(cfg, "tray_border", True))
        tray_blur = int(getattr(cfg, "tray_blur", 18) or 18)
        self._tray_color_hex = tray_color
        self._tray_opacity = tray_opacity
        self._tray_border = tray_border
        self._tray_blur = tray_blur
        self._tray_opacity_slider.blockSignals(True)
        self._tray_opacity_slider.setValue(max(0, min(100, int(tray_opacity))))
        self._tray_opacity_slider.blockSignals(False)
        self._tray_opacity_val.setText(f"{int(tray_opacity)}%")
        self._tray_blur_slider.blockSignals(True)
        self._tray_blur_slider.setValue(max(0, min(50, int(tray_blur))))
        self._tray_blur_slider.blockSignals(False)
        self._tray_blur_val.setText(f"{int(tray_blur)}px")
        self._tray_border_btn.setChecked(tray_border)
        self._tray_border_btn.setText("边框：" + ("开" if tray_border else "关"))
        self._highlight_tray(tray_color)
        self._apply_tray_preview()
        self._status.setText("")

    def _save(self) -> None:
        cfg = self._app.config_manager.config
        cfg.breath_period_s = self._period_s
        cfg.breath_color = self._color_hex
        cfg.breath_max_brightness = self._max
        cfg.tray_color = self._tray_color_hex
        cfg.tray_opacity = int(self._tray_opacity)
        cfg.tray_border = bool(self._tray_border)
        cfg.tray_blur = int(self._tray_blur)
        try:
            self._app.save_all()
        except Exception:
            pass
        # Push live to the real HUD if it is built.
        try:
            win = getattr(self._app, "_window", None)
            if win is not None:
                win.apply_breath(self._period_s, self._max, self._color_hex)
                win.apply_tray(self._tray_color_hex, self._tray_opacity,
                               self._tray_border, self._tray_blur)
            # Recolor this editor window's outer border to the new accent.
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
                period = float(getattr(cfg, "breath_period_s", None) or 2.36)
                maxb = float(getattr(cfg, "breath_max_brightness", 1.0) or 1.0)
                color = getattr(cfg, "breath_color", "") or ""
                win.apply_breath(period, maxb, color)
                tray_color = getattr(cfg, "tray_color", "0e1117") or "0e1117"
                tray_opacity = int(getattr(cfg, "tray_opacity", 86) or 86)
                tray_border = bool(getattr(cfg, "tray_border", True))
                tray_blur = int(getattr(cfg, "tray_blur", 18) or 18)
                win.apply_tray(tray_color, tray_opacity, tray_border, tray_blur)
            self._overlay._apply_window_border()
        except Exception:
            pass
        self._overlay.open_layout()

    # -- nav ------------------------------------------------------------
    def nav_items(self) -> list[NavItem]:
        items: list[NavItem] = []
        items.append(NavItem(self._back_btn, "action",
                             confirm=self._overlay.open_layout, hint="B"))
        items.append(NavItem(self._period_slider, "slider",
                             nudge=lambda d, sl=self._period_slider: (
                                 sl.setValue(max(5, min(80, sl.value() + d))), True)[1]))
        for sw in self._swatches:
            items.append(NavItem(sw, "action",
                                 confirm=lambda hx=sw._hex: self._pick_color(hx)))
        items.append(NavItem(self._custom_btn, "action",
                             confirm=self._pick_custom, hint="A"))
        items.append(NavItem(self._follow_btn, "action",
                             confirm=self._follow_theme))
        items.append(NavItem(self._max_slider, "slider",
                             nudge=lambda d, sl=self._max_slider: (
                                 sl.setValue(max(25, min(100, sl.value() + d))), True)[1]))
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
        note.setStyleSheet("color: #aeb9c9; font-size: 12px;")
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
            " font-size: 12px; }")
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
        desc.setStyleSheet("color: #aeb9c9; font-size: 12px;")
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
        lbl.setStyleSheet("color: #cdd6e2; font-size: 12px;")
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
        val.setStyleSheet("color: #62e0a8; font-size: 12px;")
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
        lbl.setStyleSheet("color: #cdd6e2; font-size: 12px;")
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
        val.setStyleSheet("color: #62e0a8; font-size: 12px;")
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
        # Outer window border follows the breathing-light colour (the single
        # theme accent) — see _apply_window_border().
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
        self._stack.addWidget(self._card_color_panel)
        self._stack.addWidget(self._xinput_gate_panel)

    # -- window border (neutral, never the bright theme accent) --------
    def _apply_window_border(self) -> None:
        """Repaint the editor window's outer border using a subtle neutral
        colour.  The breathing-light accent is reserved for glows and titles,
        not structural borders."""
        self.setStyleSheet(
            "background: rgba(14, 17, 23, 0.78);"
            " border: 1px solid rgba(255, 255, 255, 0.10);"
            " border-radius: 16px;"
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
        if self._mode == "xinput_gate":
            return self._xinput_gate_panel.nav_items()
        if self._mode == "cardcolor":
            return self._card_color_panel.nav_items()
        return []
