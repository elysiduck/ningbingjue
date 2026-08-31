"""Interactive PyQt6 panels for each 凝冰结魄 block.

State-driven design: every panel reads the live block *attributes*
(``block.fps``, ``block.tdp``, ``block.candidates`` ...) to paint its
widgets and connects Qt signals straight to the block's public methods.

Performance model
-----------------
The blocks' data refresh (``refresh_panel`` / ``gpu_float_tick``) spawns
external processes (``wmic`` enumeration, ``ryzenadj`` GPU reads, Lossless
Scaling probes).  Doing that on the UI thread every second is what made the
window feel frozen.  So each panel exposes :meth:`heavy_refresh` returning the
callable that performs the slow work (or ``None`` when the block's refresh is
already cheap).  The window owns a single background :class:`QThread` that runs
every panel's ``heavy_refresh`` off the UI thread; the UI thread only calls
:meth:`sync` (a cheap repaint from cached state) on a fast 1 s timer and again
when the background pass finishes.

The toast adapters are thread-safe: any Qt mutation triggered from a worker
thread is dispatched to the main thread via a cross-thread ``pyqtSignal``/
``pyqtSlot`` connection (auto-queued), so it always runs on the widget's
owner thread regardless of whether the emitting thread has an event loop.
"""

from __future__ import annotations


import os

import sys
import time
import traceback
from typing import Any, Callable

from PyQt6 import sip
from PyQt6.QtCore import QEvent, QSize, QTimer, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import (QAction, QColor, QFont, QPainter,
                         QPainterPath, QPen, QPixmap)
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..core.color.palette import TILE_BG, TILE_BG_HOVER, TILE_FOCUS_BORDER
from ..core.color.rgb import Color
from ..core.color.semantic import get_theme_accent
from ..platform.ryzenadj import hvci_enabled
from ..core.metrics import MetricSampler
from .interaction import NavItem
from .smt_chart import SmtChartWidget
from .tile_button import TileButton
from .gamepad_hint import HintButton, GamepadHintBar
from ._scroll_style import apply_thin_scroll


def _hex(c: Any) -> str:
    """Convert a ``Color`` (or anything with r/g/b) to ``#rrggbb``."""
    if c is None:
        return "#cfd8e3"
    r = getattr(c, "r", 207)
    g = getattr(c, "g", 216)
    b = getattr(c, "b", 227)
    return f"#{r:02x}{g:02x}{b:02x}"


def _norm_exe(exe: Any) -> str:
    """Normalise an exe path/name to its lowercased basename for whitelist match."""
    if not exe:
        return ""
    return os.path.basename(str(exe)).lower().strip()


def _parse_color(csv: str) -> Color:
    """Parse a "R,G,B" string into a Color; transparent if empty."""
    if not csv or not csv.strip():
        return Color(0, 0, 0, 0)
    parts = [p.strip() for p in csv.split(",")]
    if len(parts) == 3:
        try:
            return Color.from_rgb(int(parts[0]), int(parts[1]), int(parts[2]))
        except (ValueError, TypeError):
            pass
    return Color(0, 0, 0, 0)


# --------------------------------------------------------------------------
# 功能卡片名统一三角形标志
# --------------------------------------------------------------------------
# 固定的三角形标志（► 黑色右指三角），在所有功能卡片名前统一注入。
# 它在「显示层」添加，不写入任何用户可编辑字段，因此改名 / 编辑都无法去掉它。
_CARD_MARK = "► "


def _card_mark_name(name: str) -> str:
    """给功能卡片名加一个固定的三角形标志。

    该标志仅在显示层注入，不属于任何可存储 / 可编辑的字段，所以用户对卡片
    改名、编辑都无法去掉它。先剥掉名字前导已有的三角 / 箭头符号与空白再统一
    加回，避免重复叠加（例如原本就带 ► 的区块不会变成 ►► 名字）。
    """
    s = (name or "").strip()
    # 前导的各类三角 / 箭头符号，剥掉后再加回统一标志，杜绝叠加。
    while s and s[0] in "►▶▲▼◀◂❯▸›":
        s = s[1:].lstrip("　 ")
    return _CARD_MARK + s


# --------------------------------------------------------------------------
# Toast adapters (thread-safe: Qt ops always run on the main thread)
# --------------------------------------------------------------------------
class _Label(QLabel):
    """Thread-safe text/colour adapter.

    ``text`` / ``fore_color`` may be assigned from any thread (e.g. the
    MetricSampler read thread via ``show_toast``).  Emitting a signal from a
    foreign thread auto-queues the slot to this widget's owner thread (the
    GUI thread), so the actual ``QLabel`` mutation always happens on GUI
    thread — unlike ``QTimer.singleShot`` which silently no-ops in a thread
    without an event loop.
    """

    _set_text = pyqtSignal(str)
    _set_fore = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self._color = None
        self._text = ""
        self._set_text.connect(self._apply_text)
        self._set_fore.connect(self._apply_fore)

    @pyqtSlot(str)
    def _apply_text(self, value: str) -> None:
        super().setText(value)

    @pyqtSlot(object)
    def _apply_fore(self, c: Any) -> None:
        # 同步写入缩小的字号，并显式去掉 label 自身的 border/background，
        # 避免在内层再画出一圈框（只保留 _Toast 外层那一个外框）。
        super().setStyleSheet(
            f"color: {_hex(c)}; font-size: {TOAST_FONT_PX}px; "
            "border: none; background: transparent; padding: 0; margin: 0;"
        )

    @property
    def text(self) -> str:
        return self._text

    @text.setter
    def text(self, value: str) -> None:
        self._text = str(value)
        self._set_text.emit(value)

    @property
    def fore_color(self) -> Any:
        return self._color

    @fore_color.setter
    def fore_color(self, c: Any) -> None:
        self._color = c
        self._set_fore.emit(c)


# 提示框（toast）文字字号：全局字体已冻结为 9pt，这里显式缩到 10px，
# 让「GPU/TDP 控制可能失效」等告警框文字更小、更不抢眼。
TOAST_FONT_PX = 10

# 提示框（toast）外框：失败/警告用橙色，成功用绿色；均为单线（solid），2px 加粗。
TOAST_BORDER_BAD = "2px solid rgba(255,150,40,0.95)"
TOAST_BORDER_OK = "2px solid rgba(0,190,100,0.9)"

# 提示框（toast）文字颜色：失败/警告红字，成功白字。
TOAST_FORE_BAD = Color.from_rgb(255, 80, 80)
TOAST_FORE_OK = Color.from_rgb(255, 255, 255)


class _Toast(QFrame):
    """In-panel toast — a child widget overlaid at the panel's bottom-center.

    Unlike a top-level window, this stays *inside* the HUD (matching the
    original PS1 ``Show-RtssToast`` which adds the toast panel to the block's
    card).  Geometry is driven by ``BlockPanel.resizeEvent``; we only own the
    show/hide + auto-hide timer + tone colour here.

    Thread-safety: ``set_tone`` / ``visible`` are called from the MetricSampler
    read thread (via ``show_toast``).  They only *emit* signals, whose slots run
    on this widget's owner (GUI) thread via Qt's auto-queued connection — so the
    actual ``setStyleSheet`` / ``show`` / ``setGeometry`` never execute off the
    GUI thread.
    """

    _set_tone = pyqtSignal(bool)
    _show = pyqtSignal()
    _hide = pyqtSignal()

    def __init__(self, label: _Label, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAutoFillBackground(False)
        # 确保 QFrame 自身不画默认内框，只由样式表的 border 画一个外框。
        self.setFrameStyle(QFrame.Shape.NoFrame)
        # 外框：成功框用绿色；失败/警告框用橙色；均为 2px 加粗。
        self._ok = f"rgba(20,24,33,0.95); border: {TOAST_BORDER_OK}"
        self._bad = f"rgba(20,24,33,0.95); border: {TOAST_BORDER_BAD}"
        self.setStyleSheet(f"background: {self._ok}; border-radius: 8px;")
        self._label = label
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 2, 8, 2)
        lay.setSpacing(0)
        lay.addWidget(label)
        # label 自身不画任何框/背景，只保留外层 _Toast 那一个外框。
        label.setStyleSheet(
            f"font-size: {TOAST_FONT_PX}px; border: none; "
            "background: transparent; padding: 0; margin: 0;"
        )
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self.hide()
        self._set_tone.connect(self._apply_tone)
        self._show.connect(self._apply_show)
        self._hide.connect(self._apply_hide)

    @pyqtSlot(bool)
    def _apply_tone(self, success: bool) -> None:
        self.setStyleSheet(f"background: {self._ok if success else self._bad}; border-radius: 8px;")
        # 同步设置文字颜色：失败/警告红字，成功白字。
        if self._label is not None:
            self._label.fore_color = TOAST_FORE_OK if success else TOAST_FORE_BAD

    @pyqtSlot()
    def _apply_show(self) -> None:
        parent = self.parentWidget()
        if parent is not None:
            w = min(280, max(160, parent.width() - 20))
            h = 30
            self.setGeometry((parent.width() - w) // 2, parent.height() - h - 8, w, h)
        self.show()
        self.raise_()
        self._timer.start(2600)

    @pyqtSlot()
    def _apply_hide(self) -> None:
        self.hide()

    def set_tone(self, success: bool) -> None:
        self._set_tone.emit(success)

    @property
    def visible(self) -> bool:  # noqa: D401 - mirrors block's ui contract
        return self.isVisible()

    @visible.setter
    def visible(self, v: bool) -> None:
        if v:
            self._show.emit()
        else:
            self._hide.emit()


def _show_block_toast(ui: Any, text: str, success: bool = True) -> None:
    """统一的 toast 显示入口 —— 所有 block 的 ``show_toast`` 都应委托于此。

    保证「外框颜色 + 文字颜色 + 字号 + 定位」在所有卡片（含未来新增卡片）
    完全一致：失败/警告橙框红字、成功绿框白字、字号 10px、定位到卡片底部居中。

    注意：本函数**同步**直接操作 Qt 控件，必须在 GUI 线程调用。跨线程转发由
    调用方负责——各 block 的 ``show_toast`` 已用 ``@_gui_method`` 装饰，会把调用
    投递回 GUI 线程，而非本函数内部走信号机制。调用前先探活 widget（窗口重建/
    翻页后旧控件被 deleteLater，其 C++ 对象可能已在事件循环中被销毁，直接访问
    会原生崩溃，无 Python traceback 可捕获）。
    """
    if not isinstance(ui, dict):
        return
    panel = ui.get("toast_panel")
    label = ui.get("toast_label")
    # 探活：窗口重建/翻页后旧控件被 deleteLater，其 C++ 对象可能已在事件循环
    # 中被销毁；触摸已销毁 widget（set_tone / setGeometry / parentWidget）会
    # 直接原生崩溃、无 Python traceback 可捕获。必须先判定存活再访问。
    if not (isinstance(panel, QWidget) and not sip.isdeleted(panel)):
        panel = None
    if not (isinstance(label, QWidget) and not sip.isdeleted(label)):
        label = None
    if panel is not None and hasattr(panel, "set_tone"):
        panel.set_tone(success)   # 统一设置外框颜色 + 文字颜色
        panel.visible = True      # 触发 _apply_show：定位到卡片底部居中并显示
    if label is not None:
        label.text = text


# --------------------------------------------------------------------------
# Base panel
# --------------------------------------------------------------------------
class BlockPanel(QFrame):
    def __init__(self, instance: Any, title: str, color: Any,
                 block_key: str | None = None, cfg: Any = None) -> None:
        super().__init__()
        self.instance = instance
        # Per-card colour override key (the expanded-order token, e.g. "procmon",
        # "tile" or "tile#1").  ``None`` → fall back to the theme accent /
        # registry colour.  The window fills these in via ``make_panel``.
        self.block_key = block_key
        self._cfg = cfg
        self._base_color = color
        self.accent = _hex(self._resolve_accent())
        self.setFrameStyle(QFrame.Shape.NoFrame)
        # Background fill + neon glow border are drawn in paintEvent (frosted
        # skin, mirroring the original RoundedPanel + DrawInnerGlow).
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(10, 8, 10, 8)
        self.root.setSpacing(6)

        # 功能卡片名前统一加一个固定的三角形标志（不可编辑 / 改名去掉）。
        hdr = QLabel(_card_mark_name(title))
        hdr.setStyleSheet(
            f"color: {_hex(self._resolve_title_color())}; font-size: 13px; font-weight: bold;"
        )
        # Word-wrap long card / tile titles so they never force the panel (and
        # thus the whole window) wider than the screen — a long name flows to a
        # second line instead of pushing the window off-screen.  Paired with the
        # scroll-area minimumWidth pin + resizeEvent width clamp in app_window.
        hdr.setWordWrap(True)
        self._hdr = hdr
        self._base_accent = self.accent
        # Per-card fill colour (card background), user-customisable via the
        # breathing-light editor.  Default mirrors the previous hardcoded
        # QColor(20, 24, 33, 165) so the look is unchanged until edited.
        self._card_fill = QColor(20, 24, 33)
        self._card_fill_alpha = 165

        # 右上角状态角标（取代各卡片底部「待应用 / 状态」行）：与标题同处一行
        # 右对齐，长文本在 max_w 处省略，绝不把 HUD 整体撑宽。
        self._corner_lbl = QLabel("")
        self._corner_lbl.setWordWrap(False)
        self._corner_lbl.setAlignment(Qt.AlignmentFlag.AlignRight
                                      | Qt.AlignmentFlag.AlignVCenter)
        self._corner_lbl.setStyleSheet("color: #9aa6b8; font-size: 11px;")
        self._corner_lbl.setMaximumWidth(240)
        self._corner_lbl.setVisible(False)
        _hdr_row = QHBoxLayout()
        _hdr_row.setContentsMargins(0, 0, 0, 0)
        _hdr_row.setSpacing(8)
        _hdr_row.addWidget(hdr, 1)
        _hdr_row.addWidget(self._corner_lbl, 0)
        self.root.addLayout(_hdr_row)

        self._install_toast()
        self.editor: Any = None
        self.controller: Any = None  # set by the window after construction
        self._nav_items: list = []
        # Gamepad/keyboard "selected card" state (two-layer navigation).  The
        # card shows a strong theme-colour border only while selected; it is
        # normally hidden.  Set by InteractionController.set_card_selected.
        self._card_selected: bool = False
        self.persist = lambda: None  # overridden by build_overlay -> app.save_all
        # 静态皮肤缓存（DPR 分辨率 QPixmap）：把圆角磨砂填充 + 静止外框预烘成位图，
        # 滚动/刷新时只 blit、不再每帧重画路径。见 paintEvent / _render_skin。
        self._bg_cache: Any = None
        self._bg_key: Any = None
        self._setup()
        self.sync()  # initial populate

    # Resting (unselected) card border alpha.  Kept deliberately low so the
    # always-on frame recedes into the background; raise this one number to
    # make every function-card border more prominent.  The selected-card
    # highlight (see below) is unaffected and stays strong.
    REST_BORDER_ALPHA = 75

    # -- frosted skin: rounded fill + thin accent border -----------------
    # 静态皮肤（磨砂填充 + 静止外框）只预烘一次进 DPR 分辨率位图（``_bg_cache``），
    # 每帧 paintEvent 只 blit，而不是每次滚动/刷新都重画圆角路径。这正是修掉 0829
    # 主窗「非常卡顿」的同一杠杆：把「每帧软件光栅」换成「缓存 blit」。选中态辉光是
    # 动态的，不进缓存，留在主画家上重画。
    def _render_skin(self, w: int, h: int, dpr: float,
                     accent: "QColor", fill: "QColor", fill_alpha: int) -> "QPixmap":
        """Pre-render the static card skin at *device* resolution.

        照搬主窗 ``_rebuild_bg_cache`` 的 idiom：按 w*dpr × h*dpr 分配并
        ``setDevicePixelRatio``，使 blit 1:1 落在设备像素（分数 DPR 如掌机
        1.5x 也清晰，不再被双线性放大发糊）。
        """
        pm = QPixmap(int(round(w * dpr)), int(round(h * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        pp = QPainter(pm)
        pp.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = 12
        path = QPainterPath()
        path.addRoundedRect(1, 1, w - 2, h - 2, r, r)
        pp.setPen(Qt.PenStyle.NoPen)
        pp.setBrush(QColor(fill.red(), fill.green(), fill.blue(), fill_alpha))
        pp.drawPath(path)
        pp.setPen(QColor(accent.red(), accent.green(), accent.blue(),
                         self.REST_BORDER_ALPHA))
        pp.drawPath(path)
        pp.end()
        return pm

    def _skin_key(self, w: int, h: int, dpr: float,
                  accent: "QColor", fill: "QColor", fill_alpha: int) -> tuple:
        """缓存失效 key：逻辑尺寸 + DPR + 填充色 + 外框色 + 静止边框透明度。

        显式比对（绝不写 ``cache.width() != w`` —— QPixmap.width() 是设备像素，
        DPR=1.5 时恒不等于逻辑宽，会每帧重建）。选中态辉光不进缓存，故 key 不含
        ``_card_selected``；选中切换只走「blit 皮肤 + 重画辉光」，依旧便宜。
        """
        return (int(w), int(h), round(float(dpr), 3),
                (fill.red(), fill.green(), fill.blue(), int(fill_alpha)),
                (accent.red(), accent.green(), accent.blue()),
                int(self.REST_BORDER_ALPHA))

    def paintEvent(self, ev: Any) -> None:  # noqa: N802 - Qt camelCase
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        if w <= 2 or h <= 2:
            p.end()
            return
        accent = QColor(self.accent)
        fill = self._card_fill
        fill_alpha = self._card_fill_alpha
        r = 12
        try:
            dpr = max(1.0, float(self.devicePixelRatio()))
        except Exception:
            dpr = 1.0
        key = self._skin_key(w, h, dpr, accent, fill, fill_alpha)
        if self._bg_key != key or self._bg_cache is None or self._bg_cache.isNull():
            self._bg_cache = self._render_skin(w, h, dpr, accent, fill, fill_alpha)
            self._bg_key = key
        p.drawPixmap(0, 0, self._bg_cache)

        # Selected-card highlight (two-layer navigation): strong theme-colour
        # border drawn on top of the cached skin, normally hidden.  Only shown
        # while this card is the gamepad/keyboard-selected function card.
        if self._card_selected:
            sel = QColor(accent.red(), accent.green(), accent.blue(), 255)
            path = QPainterPath()
            path.addRoundedRect(1, 1, w - 2, h - 2, r, r)
            p.save()
            # NOTE: QPainter.drawRoundedRect() strokes with the current pen
            # *and* fills with the current brush.  The frosted card fill set
            # above is still the active brush at this point, so every glow
            # pass used to paint an opaque dark rounded rect on top of the
            # card: stacked 3x it reached ~96% opacity and spanned
            # (-5,-5)..(w+5,h+5), filling the corner notches outside the
            # 12px radius with near-solid black -- the "dark rectangle
            # around the focused card" artefact.  Clear the brush first,
            # then clip to the card silhouette so the halo can never escape
            # the rounded corners.
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setClipPath(path)
            # Inward halo.  A widget can only paint inside its own rect, so
            # the old outward-expanding passes were always clipped away
            # (leaving nothing but the dark band); shrinking inward keeps
            # the glow visible and strictly inside the rounded corner.
            if w > 32 and h > 32:
                for i in range(3, 0, -1):
                    p.setPen(QColor(sel.red(), sel.green(), sel.blue(),
                                    90 - i * 20))
                    p.drawRoundedRect(1 + i * 2, 1 + i * 2,
                                      w - 2 - i * 4, h - 2 - i * 4,
                                      max(1, r - i), max(1, r - i))
            p.restore()
            # Bright 3px solid edge.  restore() reverts the clip *and*
            # re-arms the frosted fill brush, so NoBrush must be set again.
            p.setPen(QPen(sel, 3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(2, 2, w - 4, h - 4, r, r)
        p.end()

    def set_card_selected(self, selected: bool) -> None:
        """Toggle the gamepad/keyboard "selected card" theme border.

        Early-returns when unchanged so the per-second controller refresh does
        not repaint cards that are already in the right state.
        """
        sel = bool(selected)
        if sel == self._card_selected:
            return
        self._card_selected = sel
        self.update()

    def set_accent(self, hex_color: str) -> None:
        """Update the card accent (neon border / focus glow) and repaint.

        仅影响外框与聚焦高亮，不影响标题文字色——标题仍由
        ``_resolve_title_color`` / ``_apply_title_color`` 独立控制，保留每张卡
        自己设定的卡片色。

        健壮性：调用方曾误传 ``QColor`` 对象（如 ``set_accent(QColor(r,g,b))``），
        导致 ``self.accent`` 变成非字符串，下游 ``f"color:{self.accent}"`` 拼出
        ``color:QColor(...)`` 这种非法 CSS，Qt 静默丢弃整段样式表、标签文字
        失去配色（launch.err 中反复出现的 "Could not parse stylesheet of object
        QLabel" 即源于此）。这里对 ``QColor`` / ``Color`` 等非字符串输入做兜底
        转换，彻底堵住这类「类型传错→样式失效」的隐患。
        """
        if not isinstance(hex_color, str):
            try:
                if isinstance(hex_color, QColor):
                    hex_color = f"#{hex_color.red():02x}{hex_color.green():02x}{hex_color.blue():02x}"
                else:
                    hex_color = f"#{int(getattr(hex_color, 'r', 207)):02x}" \
                                f"{int(getattr(hex_color, 'g', 216)):02x}" \
                                f"{int(getattr(hex_color, 'b', 227)):02x}"
            except Exception:
                hex_color = "#cfd8e3"
        self.accent = hex_color
        self.update()

    def set_card_fill(self, color: Any, alpha: int) -> None:
        """Update this card's fill colour + opacity and repaint.

        ``color`` may be a ``QColor`` or an ``"rrggbb"`` hex string (defensive
        conversion mirrors ``set_accent``).  ``alpha`` is a 0-255 QColor alpha.
        Painted in ``paintEvent``; the window broadcasts this to every card
        when the global setting changes.
        """
        if isinstance(color, QColor):
            self._card_fill = QColor(color)
        elif isinstance(color, str):
            try:
                self._card_fill = QColor("#" + color.lstrip("#"))
            except Exception:
                self._card_fill = QColor(20, 24, 33)
        else:
            try:
                self._card_fill = QColor(int(getattr(color, "r", 20)),
                                         int(getattr(color, "g", 24)),
                                         int(getattr(color, "b", 33)))
            except Exception:
                self._card_fill = QColor(20, 24, 33)
        self._card_fill_alpha = max(0, min(255, int(alpha)))
        self.update()

    def _resolve_accent(self) -> Any:
        """Effective accent colour for this card.

        单旋钮架构：所有卡片（tile 或非 tile）一律跟随呼吸灯主题色
        ``get_theme_accent()``，这是唯一的颜色来源。不再读取
        ``config.card_colors`` 覆盖，也不再回退到区块注册表色（旧的
        ``_base_color``），因此主页所有功能卡片外框、标题、聚焦高亮都是
        同一种呼吸灯色，不会出现五颜六色。
        """
        return get_theme_accent()

    def _resolve_title_color(self) -> Any:
        """卡片标题文字颜色：保留每张卡自己设定的卡片色。

        单旋钮架构只对『外框』生效（``_resolve_accent`` 永远返回呼吸灯色）；
        标题则保留各块的身份色，优先级：
        ``config.card_colors[block_key]`` 覆盖 → tile 区块跟随呼吸灯主题色
        （tile 无注册表色）→ 其余区块回退注册表色 ``_base_color``。
        """
        if self.block_key and self._cfg is not None:
            csv = self._cfg.card_colors.get(self.block_key)
            if csv:
                c = _parse_color(csv)
                if not (c.r == 0 and c.g == 0 and c.b == 0 and c.a == 0):
                    return c
        if self.block_key == "tile" or (self.block_key or "").startswith("tile#"):
            return get_theme_accent()
        return self._base_color

    def _apply_title_color(self) -> None:
        """把 ``_resolve_title_color`` 的结果写到卡片标题 QLabel 上。"""
        c = self._resolve_title_color()
        self._hdr.setStyleSheet(
            f"color: {_hex(c)}; font-size: 13px; font-weight: bold;"
        )

    def _set_corner(self, text: str, color: str, max_w: int = 210) -> None:
        """在卡片标题右侧显示一行状态角标（取代原底部 status_lbl 行）。

        长文本在 ``max_w`` 像素处省略（ElideRight），避免把 HUD 整体撑宽。
        """
        lbl = self._corner_lbl
        if lbl.fontMetrics().horizontalAdvance(text) > max_w:
            text = lbl.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, max_w)
        lbl.setText(text)
        lbl.setStyleSheet(f"color: {color}; font-size: 11px;")
        lbl.setVisible(True)

    def _install_toast(self) -> None:
        if hasattr(self.instance, "ui"):
            self.toast_label = _Label()
            self.toast_panel = _Toast(self.toast_label, self)
            self.instance.ui["toast_label"] = self.toast_label
            self.instance.ui["toast_panel"] = self.toast_panel

    def resizeEvent(self, ev: Any) -> None:  # noqa: N802 - Qt camelCase
        super().resizeEvent(ev)
        tp = getattr(self, "toast_panel", None)
        if tp is not None and tp.isVisible():
            w = min(280, max(160, self.width() - 20))
            h = 30
            tp.setGeometry((self.width() - w) // 2, self.height() - h - 8, w, h)
        ed = getattr(self, "editor", None)
        if ed is not None and ed.isVisible():
            ed.setGeometry(0, 0, self.width(), self.height())

    # -- subclass hooks ----------------------------------------------------
    def _setup(self) -> None:  # pragma: no cover - overridden
        """Create child widgets + wire signals."""

    def sync(self) -> None:  # pragma: no cover - overridden
        """Cheap repaint from current block state (runs on UI thread)."""

    def heavy_refresh(self) -> Callable[[], None] | None:
        """Return the slow data-refresh callable, or ``None`` if cheap.

        The window runs this on a background thread so external-process work
        never blocks the UI.  ``refresh_panel`` / ``gpu_float_tick`` spawn
        subprocesses; ``set_lock`` / ``update_status`` are pure state paints.
        """
        refresh = getattr(self.instance, "refresh_panel", None)
        if callable(refresh):
            return refresh
        return None

    # -- backward-compatible tick (used by tests) -------------------------
    def tick(self) -> None:
        fn = self.heavy_refresh()
        if fn is not None:
            try:
                fn()
            except Exception:
                pass
        self.sync()

    # -- unified interaction ---------------------------------------------
    def nav_items(self) -> list:
        """Return this panel's interactive controls as unified ``NavItem`` s.

        Subclasses override this.  Static panels build the list once (in
        ``_setup``); dynamic panels rebuild it from current state on every
        call so the shared ``InteractionController`` always sees fresh items.
        The default returns an empty list (a panel with no interactive items).
        """
        return []

    def _bind(self, widget: Any, kind: str) -> None:
        """Wire a widget's primary activation signal to the unified controller.

        Mouse and keyboard thereby converge on ``controller.activate_widget``,
        so each panel no longer needs its own bespoke per-widget click lambda.
        """
        if self.controller is None:
            # The window assigns the controller after construction; the lambda
            # resolves it lazily at activation time, so connecting now is safe.
            pass
        if kind == "slider":
            widget.sliderPressed.connect(
                lambda: self.controller.activate_widget(widget, False)
            )
        elif kind == "input":
            widget.editingFinished.connect(
                lambda: self.controller.activate_widget(widget, True)
            )
        else:
            widget.clicked.connect(
                lambda: self.controller.activate_widget(widget, True)
            )


def _btn(text: str, accent: str, active: bool = False) -> QPushButton:
    # HintButton (subclass of QPushButton) — paints its own background + the
    # ABXY capsule, so gamepad shortcuts show on the button itself when the
    # card is selected.
    b = HintButton(text)
    b.set_style(accent, active)
    b._last = (text, active)  # type: ignore[attr-defined]
    return b


class _NoWheelComboBox(QComboBox):
    """QComboBox that ignores mouse-wheel events.

    By default Qt steps the current index when the wheel rolls over a combobox
    (and swallows the event so the page no longer scrolls).  That fights the
    HUD rule "wheel only scrolls the page" — so we call ``ev.ignore()`` to let
    the event bubble up to the outer QScrollArea and scroll the page instead.
    Changing the selection still works via click / keyboard arrows, exactly
    like the existing ``_NoScrollSlider`` / ``_NoWheelSpinBox`` widgets.
    """

    def wheelEvent(self, ev: Any) -> None:  # noqa: N802 - Qt camelCase
        ev.ignore()


class _NoScrollSlider(QSlider):
    """QSlider that ignores mouse-wheel events.

    Mirrors the original HUD where scrolling the wheel never nudges a slider
    (only arrow keys / dragging do) — so wheeling over a slider does not
    accidentally change TDP / GPU / FPS values.

    Optional ``snap_step`` snaps every ``setValue`` (incl. mouse drag) to the
    nearest multiple — used by the power-plan max-frequency slider so a mouse
    drag can never land on a non-100MHz value.  ``0`` (default) disables it,
    so the other sliders (TDP / GPU / PWM / max-state) are unaffected.
    """

    def __init__(self, orientation: "Qt.Orientation" = Qt.Orientation.Horizontal,
                 parent: Any = None) -> None:
        self._snap_step = 0
        super().__init__(orientation, parent)

    def set_snap_step(self, step: int) -> None:
        """Snap all values to the nearest multiple of *step* (0 = disabled)."""
        self._snap_step = int(step or 0)

    def setValue(self, v: int) -> None:  # noqa: N802 - Qt camelCase
        if self._snap_step > 1:
            v = int(round(v / self._snap_step)) * self._snap_step
        super().setValue(v)

    def wheelEvent(self, ev: Any) -> None:  # noqa: N802 - Qt camelCase
        ev.ignore()

    def set_focus_visual(self, focused: bool) -> None:
        """Persistent selected border driven by the InteractionController.

        The navigation layer calls ``set_control_focus_visual(widget, …)`` on
        every focus change; a plain QSlider has no such hook, so without this
        the focused slider would show NO visible border (the old block-level
        ``_update_slider_visual`` only drew one while actively adjusting, which
        the panel-driven nav never triggers).  We draw the border in the live
        breathing-light accent so it stays in sync with the single-accent
        colour system and repaints correctly on every focus move.
        """
        if focused:
            accent = _hex(get_theme_accent())
            self.setStyleSheet(
                f"QSlider{{border:2px solid {accent}; border-radius:6px; padding:0 2px;}}"
            )
        else:
            self.setStyleSheet("QSlider{border:none;}")
        self.update()


def _set(btn: QPushButton, text: str, accent: str, active: bool) -> None:
    """Update a button only when its visual state actually changed."""
    key = (text, active)
    if getattr(btn, "_last", None) == key:
        return
    btn._last = key  # type: ignore[attr-defined]
    btn.setText(text)
    btn.set_style(accent, active)


def _label(text: str, color: str = "#8b97aa", size: int = 13) -> QLabel:
    l = QLabel(text)
    l.setStyleSheet(f"color: {color}; font-size: {size}px;")
    return l


class _NoWrapElideLabel(QLabel):
    """性能监控值单元格专用：单行显示，绝不换行。

    文本超出当前列可用宽度时尾部省略（…），既保证「所有内容在同一行」，又
    不会因长文本（如 ``CO=-50 (-50未验证)`` / 长窗口名）撑破卡片或覆盖相邻列。
    原始实现用 ``setWordWrap(True)`` 折行，既丑又占纵向空间，已弃用。
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setWordWrap(False)
        self._full = text
        self._last_color = None
        # 左对齐，文本从列首开始，横向利用率最高。
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

    def setFullText(self, text: str) -> None:
        if self._full == text:
            return
        self._full = text
        self._apply_elide()

    def setText(self, text: str) -> None:  # noqa: N802 - Qt camelCase
        # 面板 _set/_setc 直接调 setText；这里改写为记录原文 + 按宽度省略。
        self.setFullText(text)

    def _apply_elide(self) -> None:
        avail = self.contentsRect().width()
        if avail <= 0:
            # 尚未完成布局（宽度未知）→ 暂存原文，resizeEvent 时再省略。
            super().setText(self._full)
            return
        elided = self.fontMetrics().elidedText(
            self._full, Qt.TextElideMode.ElideRight, avail
        )
        super().setText(elided)

    def resizeEvent(self, ev) -> None:  # noqa: N802 - Qt camelCase
        super().resizeEvent(ev)
        self._apply_elide()


class _MultiValLabel(QLabel):
    """性能监控多值单元格：允许显式换行（``\\n``）。

    用于 CPU负载（平均/峰值）、GPU频率（实际/锁定）、温度（CPU/GPU）、
    功耗（SOC/电池）这类「成对指标」，把两个子值分行显示，比单行用
    `` / `` 挤在一起更易读。``_full`` / ``_last_color`` 沿用 ``_NoWrapElideLabel``
    的契约，使基类 ``_set`` / ``_setc`` 的空刷去抖与颜色变更判定照常工作。
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setWordWrap(False)
        self._full = text
        self._last_color = None
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setStyleSheet(
            "color:#dfe6f0;background:transparent;margin:0;padding:0;font-size:12px;"
        )

    def setText(self, text: str) -> None:  # noqa: N802 - Qt camelCase
        self._full = text
        super().setText(text)


class _FitScrollArea(QScrollArea):
    """QScrollArea 默认 ``minimumSizeHint()`` 会跟随内部 widget 的内容宽度。

    当这个 scroll area 被放进卡片的竖向布局（如 LSPanel / ProcMonPanel 的预设
    / 进程列表）时，卡片的最小宽度就会被内部最长的一行（长预设名 / 长进程名）
    撑开，进而超过窗口宽度 —— 迫使外层 HUD 的 QScrollArea 出现横向滚动条，
    用户必须横向拖拽才能看全第一页内容。

    重写 ``minimumSizeHint`` 的**水平**分量，使其不再跟随内容（仅保留一个很
    小的水平下限），卡片宽度即可跟随窗口、内部内容改为在 scroll area 内横向
    滚动。竖向分量仍保留内容驱动（高度由 ``setMaximumHeight`` 封顶）。
    """

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt camelCase
        h = super().minimumSizeHint()
        return QSize(60, h.height())


class _HeightFitScrollArea(_FitScrollArea):
    """Scroll area whose preferred/minimum height strictly tracks its content.

    The stock QScrollArea sometimes under-reports its content height in HiDPI /
    stylsheet-heavy setups (the viewport frame/scrollbar margins are not always
    included in ``sizeHint()``), which makes short lists look clipped even though
    they should fit.  This subclass forces the reported height to be at least the
    inner widget's preferred height plus a small safety margin, so the parent
    layout allocates enough room for every visible row.
    """

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt camelCase
        sh = super().sizeHint()
        w = self.widget()
        if w is not None:
            sh.setHeight(max(sh.height(), w.sizeHint().height() + 4))
        return sh

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt camelCase
        mh = super().minimumSizeHint()
        w = self.widget()
        if w is not None:
            mh.setHeight(max(mh.height(), w.minimumSizeHint().height() + 4))
        return mh


def _scroll_box(scroll_cls: type[QScrollArea] = _FitScrollArea) -> tuple[QScrollArea, QVBoxLayout, QWidget]:
    """Return (scroll_area, inner_layout, inner_widget) for a list.

    The height is **content-driven** (the inner widget sizes itself) and only
    *capped* by a maximum — so an empty block shows no wasted blank space
    (the early complaint "没有任何项时显示这么长"), while a long list still
    scrolls instead of pushing the whole HUD down.
    """
    scroll = scroll_cls()
    scroll.setWidgetResizable(True)
    apply_thin_scroll(scroll, always_on=False)
    scroll.setMaximumHeight(180)
    inner = QWidget()
    layout = QVBoxLayout(inner)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    scroll.setWidget(inner)
    return scroll, layout, inner


# --------------------------------------------------------------------------
# rtss — frame limiter
# --------------------------------------------------------------------------
class RtssPanel(BlockPanel):
    # 调节帧率后延迟下发 RTSS 的毫秒数（约几秒）：拖动/连按时只在「最后一次调节」
    # 之后下发一次，避免每个中间帧率都调用一次 rtss-cli（subprocess）导致卡顿/掉帧。
    RTSS_APPLY_DEBOUNCE_MS = 2000
    # 手柄/键盘方向键单步调节帧数（鼠标拖动仍为连续值）。
    RTSS_FPS_STEP = 5

    def _setup(self) -> None:
        b = self.instance
        row = QHBoxLayout()
        self.toggle_btn = _btn("锁帧: 关", self.accent)
        self.fps_lbl = QLabel(f"{b.fps} FPS")
        self.fps_lbl.setStyleSheet("color: #dfe6f0; font-size: 13px;")
        self.auto_btn = _btn("自动锁帧: 关", self.accent)
        # 固定宽度：自动锁帧按钮文字在 关/开/已顶60/不可用 间切换，宽度会跳，
        # 一次性按最宽状态固定，避免切换时卡片布局左右晃动。
        self.auto_btn.reserve_width_for(
            ["自动锁帧: 关", "自动锁帧: 开", "自动: 已顶60", "自动锁帧: 不可用"])
        row.addWidget(self.toggle_btn)
        row.addWidget(self.fps_lbl)
        row.addStretch(1)
        row.addWidget(self.auto_btn)
        self.root.addLayout(row)

        self.slider = _NoScrollSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(b.FPS_MIN, b.FPS_MAX)
        self.slider.setValue(int(b.fps))
        # 键盘/手柄方向键单步 = 5 帧（鼠标拖动仍是连续值）。
        self.slider.setSingleStep(self.RTSS_FPS_STEP)
        self.root.addWidget(self.slider)

        # 「修复 RTSS」按钮：仅当 RTSS 缺失 / 部署失败时显示（grill 决策：状态与
        # 修复入口都放在锁帧卡片上，不合并进进程监控列表，避免被误停导致假成功）。
        # 点击跑完整自部署+拉起链路。默认隐藏，sync() 按状态决定显隐。
        self.repair_btn = _btn("修复 RTSS", "#ff6b6b", False)
        self.repair_btn.setVisible(False)
        self.repair_row = QHBoxLayout()
        self.repair_row.addStretch(1)
        self.repair_row.addWidget(self.repair_btn)
        self.root.addLayout(self.repair_row)

        # RTSS 运行状态显示：真正探测 RTSS 是否安装/运行（不再只看 ps1 文件），
        # 让用户一眼看到「锁帧到底有没有在用」。
        # RTSS 运行状态：改到卡片右上角角标显示，不再占用底部一行。
        self.status_lbl = self._corner_lbl

        # 升压上限 / GPU 负载阈值滑块已移至「锁帧控制 · 编辑项」页面
        # （RtssEditPanel），卡片主页不再展示，避免卡片过高、信息过载。卡片
        # 主页保留锁帧开关 + 帧率滑块 + 自动锁帧开关 + RTSS 状态。

        # 延迟下发定时器：单发，最后一次调节后 RTSS_APPLY_DEBOUNCE_MS 才真正
        # 调用 rtss-cli，期间状态栏显示「待应用」。
        self._apply_timer = QTimer(self)
        self._apply_timer.setSingleShot(True)
        self._apply_timer.setInterval(self.RTSS_APPLY_DEBOUNCE_MS)
        self._apply_timer.timeout.connect(self._flush_apply)
        # 标记「当前 valueChanged 是否来自手柄/键盘方向键微调」：来自微调时
        # 绝不自动下发（等用户「再摁一次 A / Enter」确认 _on_slider_confirm），
        # 只有鼠标拖动才走下方的防抖自动下发。
        self._nudging = False

        self._bind(self.toggle_btn, "toggle")
        self._bind(self.auto_btn, "toggle")
        self.slider.valueChanged.connect(self._on_slide)
        self._bind(self.slider, "slider")

    def _on_toggle(self) -> None:
        # 锁帧开关是一次性明确操作：取消待定的延迟下发，立即应用。
        self._apply_timer.stop()
        self.instance.toggle_lock()
        self.sync()

    def _on_toggle_auto(self) -> None:
        # 自动锁帧开关是一次性明确操作：取消待定的延迟下发，立即应用。
        self._apply_timer.stop()
        self.instance.toggle_auto_lock()
        self.sync()

    def _on_slide(self, v: int) -> None:
        self.instance.update_slider_ui(v)
        if self._nudging:
            # 手柄/键盘方向键微调：绝不自动下发，等用户「再摁一次 A / Enter」
            # 确认（_on_slider_confirm）才真正写 RTSS，避免误触即生效。
            self._apply_timer.stop()
        elif self.instance.lock_enabled:
            # 鼠标拖动：不立即下发 RTSS，等最后一次调节后延迟几秒再调，防卡顿。
            self._apply_timer.start()
        else:
            self._apply_timer.stop()
        self.sync()

    def _flush_apply(self) -> None:
        """延迟定时器到点：真正下发 RTSS 锁帧命令（仅锁帧开启时）。"""
        if self.instance.lock_enabled:
            self.instance.apply_lock()
        self.sync()

    def _on_slider_confirm(self) -> None:
        # 用户主动确认（手柄/键盘二次按下）：立即应用，取消待定的延迟下发。
        self._apply_timer.stop()
        if self.instance.lock_enabled:
            self.instance.apply_lock()
        self.sync()

    def _on_repair(self) -> None:
        # 手动修复：跑完整自部署 + 拉起 RTSS + 重发 profile + 重启守护。
        state, text = self.instance.deploy_and_repair()
        ok = state not in ("missing", "deployed_failed")
        self.instance.show_toast(f"RTSS: {text}", success=ok)
        self.sync()

    def _nudge_fps(self, delta: int) -> bool:
        s = self.slider
        # delta 为方向 ±1；手柄/键盘单步 = RTSS_FPS_STEP(5) 帧。
        nv = max(s.minimum(), min(s.maximum(), s.value() + delta * self.RTSS_FPS_STEP))
        if nv == s.value():
            return False
        # 标注「这是手柄/键盘方向键微调」，让 _on_slide 走「不自动下发」分支，
        # 等用户「再摁一次 A / Enter」确认后才真正写 RTSS。
        self._nudging = True
        try:
            s.setValue(nv)
        finally:
            self._nudging = False
        return True

    # 升压上限 / GPU 负载阈值的读写已移至 RtssEditPanel（锁帧控制编辑项页面），
    # 卡片主页不再持有这两个滑块及其回调。

    def nav_items(self) -> list:
        items = [
            NavItem(self.toggle_btn, "toggle", confirm=self._on_toggle),
            NavItem(self.slider, "slider",
                    confirm=self._on_slider_confirm, nudge=self._nudge_fps),
            NavItem(self.auto_btn, "toggle", confirm=self._on_toggle_auto, hint="Y"),
        ]
        # 修复按钮仅在其可见时加入导航（隐藏控件不应可被聚焦）。
        if self.repair_btn.isVisible():
            items.append(NavItem(self.repair_btn, "repair", confirm=self._on_repair))
        return items

    def sync(self) -> None:
        b = self.instance
        _set(self.toggle_btn, "锁帧: 开" if b.lock_enabled else "锁帧: 关",
             self.accent, b.lock_enabled)
        self.slider.blockSignals(True)
        self.slider.setValue(int(b.fps))
        self.slider.blockSignals(False)
        # 锁定开启但尚未下发（拖动/连按的延迟窗口内）显示「待应用」，
        # 让用户知道当前数字还没真正生效到 RTSS。
        self.fps_lbl.setText(
            f"{b.fps} FPS · 待应用" if (b.lock_enabled and not b.lock_applied)
            else f"{b.fps} FPS"
        )

        # Auto-lock button: greyed when the shared metric collector is unavailable.
        if not b.auto_lock_available():
            self.auto_btn.setEnabled(False)
            _set(self.auto_btn, "自动锁帧: 不可用", self.accent, False)
        else:
            self.auto_btn.setEnabled(True)
            if b.auto_active:
                # Boosted → gold highlight to read "currently 60".
                _set(self.auto_btn, "自动: 已顶60", "#ffd166", True)
            elif b.auto_lock_enabled:
                _set(self.auto_btn, "自动锁帧: 开", self.accent, True)
            else:
                _set(self.auto_btn, "自动锁帧: 关", self.accent, False)

        # RTSS 运行状态（每秒 tick 刷新）：绿=运行中 / 黄=已装未运行 / 红=未安装 /
        # 橙=已部署但拉起失败（第四态）。
        try:
            state, text = b.rtss_status()
        except Exception:
            state, text = ("unknown", "状态未知")
        _st_color = {
            "running": "#46d369",
            "installed": "#ffd166",
            "missing": "#ff6b6b",
            "deployed_failed": "#ff9f43",
            "unknown": "#9aa4b2",
        }.get(state, "#9aa4b2")
        self._set_corner(f"RTSS: {text}", _st_color)

        # 「修复 RTSS」仅在缺失 / 部署失败时显示（其它状态无需手动干预）。
        show_repair = state in ("missing", "deployed_failed")
        self.repair_btn.setVisible(show_repair)

        # 升压上限 / GPU 负载阈值：已移至「锁帧控制 · 编辑项」页面，卡片主页
        # 不再展示（见 RtssEditPanel）。

    def heavy_refresh(self) -> Callable[[], None] | None:
        b = self.instance
        # 自动锁帧状态机已挂到全局 MetricSampler（后台线程，失焦也运行），由采样
        # 回调独占驱动。仅当 MetricSampler 没在跑时才回退到 UI 慢定时器，避免
        # 双重驱动 + 失焦即停（和 power 块 GPU 浮动同方案）。
        if getattr(b, "auto_lock_enabled", False):
            try:
                if MetricSampler.instance().is_running:
                    return None
            except Exception:
                pass
            if b.auto_lock_available():
                return lambda: b.auto_lock_tick()
        return None


# --------------------------------------------------------------------------
# power — TDP + GPU clock
# --------------------------------------------------------------------------
class PowerPanel(BlockPanel):
    # 拖动 TDP/GPU 滑块后延迟下发 ryzenadj 的毫秒数：拖动/连按期间只在「最后一次
    # 调节」之后下发一次，避免每个中间值都启动一次 ryzenadj.exe（subprocess +
    # WinRing0 互斥占用，与降压/风扇卡片共享同一把互斥，频繁抢占会卡 UI）。
    # 700ms 既能吸收连续拖动（鼠标拖一下通常 1-2s），又不让用户感到"按完没反应"；
    # 想立即生效按「应用」按钮（_confirm_apply 取消防抖、立即下发）。
    POWER_APPLY_DEBOUNCE_MS = 700

    def _setup(self) -> None:
        b = self.instance
        tdp_row = QHBoxLayout()
        self.tdp_lbl = QLabel(f"{b.tdp} W")
        self.tdp_lbl.setStyleSheet("color: #dfe6f0; min-width: 56px; font-size: 13px;")
        self.tdp_lock = _btn("TDP锁", self.accent)
        tdp_row.addWidget(_label("TDP"))
        tdp_row.addWidget(self.tdp_lbl)
        tdp_row.addStretch(1)
        tdp_row.addWidget(self.tdp_lock)
        self.root.addLayout(tdp_row)

        self.tdp_slider = _NoScrollSlider(Qt.Orientation.Horizontal)
        self.tdp_slider.setRange(b.TDP_MIN, b.TDP_MAX)
        self.tdp_slider.setValue(int(b.tdp))
        self.root.addWidget(self.tdp_slider)

        gpu_row = QHBoxLayout()
        self.gpu_lbl = QLabel(f"{b.gpu} MHz")
        self.gpu_lbl.setStyleSheet("color: #dfe6f0; min-width: 64px; font-size: 13px;")
        self.gpu_lock = _btn("GPU锁", self.accent)
        gpu_row.addWidget(_label("GPU"))
        gpu_row.addWidget(self.gpu_lbl)
        gpu_row.addStretch(1)
        gpu_row.addWidget(self.gpu_lock)
        self.root.addLayout(gpu_row)

        self.gpu_slider = _NoScrollSlider(Qt.Orientation.Horizontal)
        self.gpu_slider.setRange(b.GPU_MIN, b.GPU_MAX)
        self.gpu_slider.setSingleStep(50)
        self.gpu_slider.setValue(int(b.gpu))
        self.root.addWidget(self.gpu_slider)

        # 待应用 / 未锁定状态：改到卡片右上角角标显示，不再占用底部一行。
        self.status_lbl = self._corner_lbl

        # --- 行动按钮：GPU浮动 / 应用 同一行（GPU监测已移至编辑项页面；自动降压已独立成「降压控制」卡片） ----
        act = QHBoxLayout()
        self.gpu_float = _btn("GPU浮动", self.accent)
        self.apply = _btn("应用", self.accent)
        # 固定宽度：GPU浮动 的文字会随状态切换（开=当前频率），若宽度跟着
        # 文字走，切换时这一行会被左右挤动。按最宽状态一次性固定宽度
        # （含手柄提示胶囊预留），见 HintButton.reserve_width_for。
        self.gpu_float.reserve_width_for(["GPU浮动", f"GPU浮动：{b.GPU_MAX}MHz"])
        # 应用按钮与 GPU浮动 等宽：GPU浮动 已 setFixedWidth（min==max 固定），
        # 直接把「应用」固定成同一宽度，两个按钮视觉一致、行内不跳动。
        self.apply.setFixedWidth(self.gpu_float.maximumWidth())
        act.addWidget(self.gpu_float)
        act.addWidget(self.apply)
        self.root.addLayout(act)

        self.tdp_slider.valueChanged.connect(self._on_tdp)
        self.gpu_slider.valueChanged.connect(self._on_gpu)
        self._bind(self.tdp_slider, "slider")
        self._bind(self.gpu_slider, "slider")
        self._bind(self.tdp_lock, "toggle")
        self._bind(self.gpu_lock, "toggle")
        self._bind(self.gpu_float, "toggle")
        self._bind(self.apply, "action")

        # 延迟下发定时器：单发，最后一次调节后 POWER_APPLY_DEBOUNCE_MS 才真正
        # 调用 ryzenadj。期间 UI 显示「待应用」，拖完松手到点自动下发。
        self._apply_timer = QTimer(self)
        self._apply_timer.setSingleShot(True)
        self._apply_timer.setInterval(self.POWER_APPLY_DEBOUNCE_MS)
        self._apply_timer.timeout.connect(self._flush_apply)
        # 标记「当前 valueChanged 是否来自手柄/键盘方向键微调」：来自微调时
        # 绝不自动下发（等用户「再摁一次 A / Enter」确认 _confirm_apply），
        # 只有鼠标拖动才走下方的防抖自动下发。
        self._nudging = False

        # 启动后延迟做一次 HVCI 环境预检：内存完整性开启会拦截 WinRing0x64.sys，
        # 导致 TDP/GPU 控制失效（管理员也救不了）。提前给明确指引，避免用户误以为
        # 是程序 bug 或反复「以管理员运行」。延迟到 UI 就绪后再弹，避免阻塞初始化。
        QTimer.singleShot(1800, self._warn_hvci)

    def _warn_hvci(self) -> None:
        """若 Windows 内存完整性(HVCI/Core Isolation)开启，提示根因与修复路径。

        仅提示一次，不阻塞。这是 TDP/GPU 频率控制「以管理员运行仍失败」的头号根因。
        """
        try:
            if hvci_enabled():
                self.instance.show_toast(
                    "⚠ TDP/GPU 控制可能失效：Windows「内存完整性」已开启，拦截了 "
                    "ryzenadj 的 ring0 驱动。请在「Windows 安全中心→设备安全性→"
                    "内核隔离」关闭「内存完整性」并重启。", success=False)
        except Exception:  # noqa: BLE001
            pass

    def _confirm_tdp_lock(self) -> None:
        # 锁开关是一次性明确操作：取消任何待定的延迟下发，避免关锁后定时器仍触发。
        self._apply_timer.stop()
        self.instance.toggle_tdp_lock()
        self.sync()

    def _confirm_gpu_lock(self) -> None:
        self._apply_timer.stop()
        self.instance.toggle_gpu_lock()
        self.sync()

    def _confirm_gpu_float(self) -> None:
        self._apply_timer.stop()
        self.instance.toggle_gpu_float()
        self.sync()

    def _confirm_apply(self) -> None:
        # 用户主动按「应用」：取消防抖定时器，立即下发一次 ryzenadj。
        self._apply_timer.stop()
        self.instance.apply_power()
        self.sync()

    # --- 自动降压相关逻辑已整体迁至 UndervoltPanel（独立的「降压控制」卡片） ----
    def _nudge_tdp(self, delta: int) -> bool:
        s = self.tdp_slider
        nv = max(s.minimum(), min(s.maximum(), s.value() + delta * s.singleStep()))
        if nv == s.value():
            return False
        # 标注「这是手柄/键盘方向键微调」，让 _on_tdp 走「不自动下发」分支。
        self._nudging = True
        try:
            s.setValue(nv)
        finally:
            self._nudging = False
        return True

    def _nudge_gpu(self, delta: int) -> bool:
        s = self.gpu_slider
        nv = max(s.minimum(), min(s.maximum(), s.value() + delta * s.singleStep()))
        if nv == s.value():
            return False
        # 标注「这是手柄/键盘方向键微调」，让 _on_gpu 走「不自动下发」分支。
        self._nudging = True
        try:
            s.setValue(nv)
        finally:
            self._nudging = False
        return True

    def nav_items(self) -> list:
        # TDP 滑块置顶：TDP 是高频调节项，进卡片时焦点直接落在滑块上，
        # 按 A 武装后方向键即可 ±TDP，省去从 TDP锁移到滑块的一步。
        # TDP锁紧随其后保持同组可达；GPU浮动=Y 为手柄直触快捷键（卡片高亮/
        # 进入时按钮显示对应徽章，按 Y 直接触发，无需先移动焦点）。
        # GPU监测已移至电源设置卡片的编辑项页面（PowerEditPanel）。
        return [
            NavItem(self.tdp_slider, "slider",
                    confirm=self._confirm_apply, nudge=self._nudge_tdp),
            NavItem(self.tdp_lock, "toggle", confirm=self._confirm_tdp_lock),
            NavItem(self.gpu_lock, "toggle", confirm=self._confirm_gpu_lock),
            NavItem(self.gpu_slider, "slider",
                    confirm=self._confirm_apply, nudge=self._nudge_gpu),
            NavItem(self.gpu_float, "toggle", confirm=self._confirm_gpu_float, hint="Y"),
            NavItem(self.apply, "action", confirm=self._confirm_apply),
        ]

    def _on_tdp(self, v: int) -> None:
        self.instance.tdp = max(self.instance.TDP_MIN, min(self.instance.TDP_MAX, v))
        self.instance.applied = False
        self.instance.update_slider_ui()
        if self._nudging:
            # 手柄/键盘方向键微调：绝不自动下发，等「再摁一次 A / Enter」确认
            # （_confirm_apply）才真正调 ryzenadj。
            self._apply_timer.stop()
        elif self.instance.tdp_lock:
            # 鼠标拖动：不立即下发，等最后一次调节后延迟 POWER_APPLY_DEBOUNCE_MS
            # 再调一次 ryzenadj，避免拖动期间每个中间值都启动一个 ryzenadj 进程。
            self._apply_timer.start()
        else:
            self._apply_timer.stop()
        self.sync()

    def _on_gpu(self, v: int) -> None:
        self.instance.gpu = max(self.instance.GPU_MIN, min(self.instance.GPU_MAX, v))
        self.instance.applied = False
        self.instance.update_slider_ui()
        if self._nudging:
            # 手柄/键盘方向键微调：绝不自动下发，等「再摁一次 A / Enter」确认。
            self._apply_timer.stop()
        elif self.instance.gpu_lock:
            self._apply_timer.start()
        else:
            self._apply_timer.stop()
        self.sync()

    def _flush_apply(self) -> None:
        """延迟定时器到点：真正下发 ryzenadj（仅 TDP 锁或 GPU 锁开启时）。

        两个锁都关闭时绝不下发——保持原 ``_on_*`` 的语义不变（关锁状态下滑动
        只更新 UI 显示的"待应用"，要等用户按「应用」按钮）。
        """
        if self.instance.tdp_lock or self.instance.gpu_lock:
            self.instance.apply_power()
        self.sync()

    def sync(self) -> None:
        b = self.instance
        self.tdp_slider.blockSignals(True)
        self.tdp_slider.setValue(int(b.tdp))
        self.tdp_slider.blockSignals(False)
        self.gpu_slider.blockSignals(True)
        self.gpu_slider.setValue(int(b.gpu))
        self.gpu_slider.blockSignals(False)
        self.tdp_lbl.setText(f"{b.tdp} W")
        self.gpu_lbl.setText(f"{b.gpu} MHz")
        _set(self.tdp_lock, "TDP锁", self.accent, b.tdp_lock)
        _set(self.gpu_lock, "GPU锁", self.accent, b.gpu_lock)
        # GPU 浮动开启时，按钮文字实时显示当前自动锁定的 GPU 频率
        # （b.gpu_current_freq 由 gpu_float_tick 决策并更新，_fast 每 1s 调
        #  sync 刷新，失焦冻结属预期）。关闭时回落为纯标题。
        if b.gpu_float_enabled:
            _set(self.gpu_float, f"GPU浮动：{b.gpu_current_freq}MHz",
                 self.accent, True)
        else:
            _set(self.gpu_float, "GPU浮动", self.accent, False)
        # 互斥长期占用告警：_gpu_busy_warn_pending 由后台采样线程置位（busy 连击≥5，
        # 即跨进程互斥被另一实例持续占用，GPU 浮动无法下发——卡死锁频上限的根因之一）。
        # 此处 1s 轮询捕获 → 主线程弹 toast，提示用户关闭重复实例。
        if getattr(b, "_gpu_busy_warn_pending", False):
            b._gpu_busy_warn_pending = False
            self.instance.show_toast(
                "⚠ ryzenadj 被另一个凝冰结魄实例占用（跨进程互斥持续超时），"
                "GPU 浮动无法下发。请关闭重复运行的实例后重试。", success=False)
        if b.applied:
            self._status("✓ 已应用", "#4be38a")
        elif b.tdp_lock or b.gpu_lock:
            self._status("待应用", "#ffd166")
        else:
            self._status("未锁定", "#9aa6b8")

    def _status(self, text: str, color: str) -> None:
        self._set_corner(text, color)

    def heavy_refresh(self) -> Callable[[], None] | None:
        # GPU 浮动锁频现在由共享 MetricSampler（全局性能脚本）的采样回调驱动，
        # 它在后台线程运行、不受窗口失焦影响——比这个 _slow 刷新定时器更可靠
        # （_pause_aux 会在失焦时停掉 _slow，导致一打游戏自动锁频就停）。
        # 仅当采样器因故没运行时，才回退到 _slow 定时器驱动（失焦时仍会停，
        # 但至少聚焦时可用）。
        b = self.instance
        if getattr(b, "gpu_float_enabled", False):
            try:
                from ..core.metrics import MetricSampler
                if not MetricSampler.instance().is_running:
                    return lambda: b.gpu_float_tick()
            except Exception:
                return lambda: b.gpu_float_tick()
        return None


# --------------------------------------------------------------------------
# fan — 风扇控制（MotionAssistant EC 移植）
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 风扇驱动自测 / 暴力测试（卡片与编辑项页共用，抽成模块函数）
# --------------------------------------------------------------------------
def run_fan_selftest_dialog(parent: QWidget) -> None:
    """驱动自测：诊断链路 + 寄存器探测，不改风扇状态。"""
    from ..platform import ec_io
    try:
        r = ec_io.self_test()
    except Exception as exc:  # noqa: BLE001
        QMessageBox.critical(parent, "自测异常", f"自测过程抛出异常：\n{exc}")
        return
    lines = []
    lines.append(f"管理员提权 : {'是' if r['is_admin'] else '否'}")
    lines.append(f"驱动文件   : {r['sys_path'] or '(未找到)'}")
    if r["driver_open"]:
        lines.append("驱动设备   : 已打开 \\.\\WinRing0_1_2_0 ✓")
    else:
        lines.append(f"驱动设备   : 打开失败 ✗（错误码 {r['error_code']}）")
        if r["error_msg"]:
            lines.append(f"失败原因   : {r['error_msg']}")
        QMessageBox.information(parent, "风扇驱动自测", "\n".join(lines))
        return
    rb = r["ec_readback"]
    if isinstance(rb, int):
        lines.append(f"EC 读回    : 寄存器 629 = {rb}（端口 I/O 链路通畅 ✓）")
    else:
        lines.append(f"EC 读回    : {rb}")

    probe = r.get("fan_probe", {})
    if probe:
        lines.append("")
        lines.append("═══ EC 寄存器探测（读所有已知 MA 风扇寄存器）═══")
        for proto_name in ("4e4f", "2e2f"):
            pdata = probe.get(proto_name, {})
            entries = []
            for label, info in pdata.items():
                if isinstance(info, dict):
                    v = info.get("value")
                    dev = info.get("device", "")
                    hx = info.get("hex", "")
                    if v is None:
                        entries.append(f"  {label} ({hx}) [{dev}] = 读取失败")
                    else:
                        entries.append(
                            f"  {label} ({hx}) [{dev}] = {v} (0x{v:02X})")
                else:
                    entries.append(f"  {label} = {info}")
            if entries:
                lines.append(f"  协议 {proto_name.upper()}:")
                lines.extend(entries)

    for test_name in ("wm2_write_test", "winmini_write_test", "win4_write_test"):
        wt = r.get(test_name)
        if not wt:
            continue
        label = {"wm2_write_test": "WM2 写回读(6153)",
                 "winmini_write_test": "WinMini 写回读(1146)",
                 "win4_write_test": "Win4 写回读(49937)"}.get(test_name,
                                                            test_name)
        if "error" in wt:
            lines.append(f"\n{label}: ✗ {wt['error']}")
        elif wt.get("match"):
            lines.append(f"\n{label}: ✓ 写入验证通过！"
                         f"(原={wt.get('original')}, 写=0x{wt['wrote']:02X}, "
                         f"读回=0x{wt['readback']:02X})")
        else:
            lines.append(f"\n{label}: ⚠ 写入了但读回不一致"
                         f"(原={wt.get('original')}, 写=0x{wt['wrote']:02X}, "
                         f"读回=0x{wt.get('readback', '?'):02X})"
                         f" → 该寄存器可能不支持写或只读")

    lines.append("")
    lines.append("请把此完整结果发给我，我会据此确定你的机型对应哪个寄存器。")
    QMessageBox.information(parent, "风扇驱动自测 - 凝冰结魄", "\n".join(lines))


# 暴力测试各方法 → 设备映射（quirk, protocol, enable_addr, pwm_addr, rpm_addr）
_BRUTE_METHOD_MAP = {
    "A": ("wm2",      "4e4f", 629,   6153,  6154),
    "B": ("win_mini", "4e4f", 0,     1146,  1147),
    "C": ("win4",     "2e2f", 0,     49937, 49938),
    # 标准 EC 端口（Zotac 类）卡片的 SIO 路径暂不支持，命中时只提示不持久化
    "D": ("std_74",   "std",  0x74,  0x75,  0),
    "E": ("std_64",   "std",  0x64,  0x65,  0),
}


def _get_fan_block(parent) -> object | None:
    """Best-effort fetch of the FanBlock instance from a panel/edit-overlay."""
    blk = getattr(parent, "instance", None)
    if blk is not None and hasattr(blk, "set_detected_mapping"):
        return blk
    getter = getattr(parent, "_block", None)
    if callable(getter):
        b = getter()
        if b is not None and hasattr(b, "set_detected_mapping"):
            return b
    return None


def _persist_parent(parent) -> None:
    for name in ("persist", "_persist"):
        fn = getattr(parent, name, None)
        if callable(fn):
            try:
                fn()
                return
            except Exception:  # noqa: BLE001
                pass


def _apply_brute_mapping(parent, key: str) -> None:
    """Persist the device mapping the user confirmed via the brute-force test."""
    blk = _get_fan_block(parent)
    if blk is None:
        QMessageBox.warning(parent, "无法应用",
                             "未找到风扇区块实例，请重启应用后再试。")
        return
    if key in ("D", "E"):
        QMessageBox.information(
            parent, "标准 EC 端口机型",
            "你选的方法走「标准 EC 端口」(0x74/0x75 或 0x64/0x65)。\n"
            "当前风扇卡片基于 SIO 寄存器窗口(4E4F/2E2F)，暂不支持这种机型。\n"
            "请把这个结果反馈给开发者，以便加入标准 EC 支持。")
        return
    quirk, proto, en, pwm, rpm = _BRUTE_METHOD_MAP[key]
    blk.set_detected_mapping(quirk, proto, en, pwm, rpm,
                             model_hint=f"暴力测试确认({key})")
    _persist_parent(parent)
    QMessageBox.information(
        parent, "已锁定设备映射",
        f"已保存并锁定设备映射：\n"
        f"  机型 = {quirk} · 协议 = {proto}\n"
        f"  使能寄存器 = {en} · 转速寄存器 = {pwm}\n\n"
        "今后风扇卡片会直接用这个寄存器，自动检测不再覆盖它。")


class _BruteChoiceButton(HintButton):
    """暴力测试候选按钮：悬停即预选，并显示方法字母 ABXY(E) 提示胶囊。"""

    def __init__(self, *args, dlg: "_BruteTestDialog" = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._dlg = dlg

    def enterEvent(self, ev: object) -> None:  # noqa: N802 - Qt camelCase
        super().enterEvent(ev)
        if self._dlg is not None:
            self._dlg._select_widget(self)


class _BruteTestDialog(QDialog):
    """凝冰风格暴力风扇测试对话框（替代原生 QMessageBox + QInputDialog）。

    解决原实现的三个问题：
      * 原生弹窗不美观、不可手柄/键盘导航；
      * 早期代码从 brute_force_fan_test() 读 ``access_method`` / ``ols_status``
        这两个字段其实从未被返回，于是一直显示「？」——这里改从 ec_io.diag_info()
        推断，并给出有意义的诊断文案；
      * 每次打开都重新跑 brute_force_fan_test() 拿最新结果，不复用残留状态。
    支持键盘方向键移动预选 + 回车确认 + Esc 取消；默认预选 Win Mini（B，寄存器
    1146），让 GPD Win Mini / Duo 用户无需逐个试也能直接确认生效。
    """

    _METHOD_ITEMS = [
        ("A", "WM2 (Win Max 2)", "寄存器 629 / 6153"),
        ("B", "Win Mini / Duo", "寄存器 1146"),
        ("C", "Win 4", "寄存器 49937"),
        ("D", "标准 EC 端口", "0x74 / 0x75"),
        ("E", "标准 EC 端口", "0x64 / 0x65"),
    ]

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("风扇暴力测试 - 凝冰结魄")
        self.setModal(True)
        self.setStyleSheet(
            "QDialog{background: rgba(20,24,33,235);"
            " border:1px solid rgba(255,255,255,0.12); border-radius: 14px;}"
            "QLabel{background: transparent;}")
        self._choices: list[tuple] = []   # (key_or_None, button)
        self._sel = 1                      # 默认预选 B（Win Mini）
        self._result_key: str | None = None
        self._build_ui()
        self._run_test()
        self._highlight()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(22, 22, 22, 22)

        title = QLabel("风扇控制 · 暴力测试")
        title.setStyleSheet("color:#dfe6f0; font-weight:bold; font-size:16px;")
        root.addWidget(title)

        # 测试结果（访问方式 / OLS 状态 / 各方法写入结果）
        self._result_lbl = QLabel("")
        self._result_lbl.setWordWrap(True)
        self._result_lbl.setStyleSheet(
            "color:#dfe6f0; font-size:12px; background: rgba(127,209,255,0.08);"
            " border-radius:8px; padding:8px 10px;")
        root.addWidget(self._result_lbl)

        hint = QLabel(
            "上面哪种方法让风扇转速明显变化 / 拉满？选中后该寄存器映射会被永久"
            "保存，风扇卡片此后直接用对的地址。\n若是 GPD Win Mini / Duo，直接按"
            " Enter 确认 B 即可。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#aeb9c9; font-size:12px;")
        root.addWidget(hint)

        # 候选按钮（A-E + 跳过），2 列网格
        grid = QGridLayout()
        grid.setSpacing(8)
        cols = 2
        for i, (key, name, reg) in enumerate(self._METHOD_ITEMS):
            btn = _BruteChoiceButton(f"{name}\n{reg}", dlg=self)
            btn.set_gamepad_hint(key)
            btn.set_style("#7fd1ff", False)
            btn.setMinimumHeight(54)
            btn.clicked.connect(lambda _=False, k=key: self._confirm_choice(k))
            self._choices.append((key, btn))
            r, c = divmod(i, cols)
            grid.addWidget(btn, r, c)
        skip = _BruteChoiceButton("都不行 / 跳过", dlg=self)
        skip.set_gamepad_hint("X")
        skip.set_style("#e68080", False)
        skip.setMinimumHeight(40)
        skip.clicked.connect(lambda _=False: self._confirm_choice(None))
        self._choices.append((None, skip))
        r = (len(self._METHOD_ITEMS) + cols - 1) // cols
        grid.addWidget(skip, r, 0, 1, cols)
        root.addLayout(grid)

        # 底部确认 / 取消（键盘 Enter / Esc 也可）
        foot = QHBoxLayout()
        foot.addStretch(1)
        ok = HintButton("确认 (Enter)")
        ok.set_style("#62e0a8", True)
        ok.clicked.connect(lambda _=False: self._confirm_choice(self._current_key()))
        cancel = HintButton("取消 (Esc)")
        cancel.set_style("#e68080", False)
        cancel.clicked.connect(lambda _=False: self.reject())
        foot.addWidget(ok)
        foot.addWidget(cancel)
        root.addLayout(foot)
        self.setMinimumWidth(440)
        self.adjustSize()

    def _run_test(self) -> None:
        """每次打开都重新跑测试，并据 diag_info() 推断访问方式 / OLS 状态。"""
        from ..platform import ec_io
        try:
            r = ec_io.brute_force_fan_test()
        except Exception as exc:  # noqa: BLE001
            r = {"methods": [], "note": f"测试过程抛出异常：{exc}"}
        d = ec_io.diag_info()
        method = d.get("method") or ""
        ols = d.get("ols_available")
        is_admin = d.get("is_admin")

        bits: list[str] = []
        bits.append(f"访问方式：{method.upper() if method else '未返回'}")
        if ols:
            bits.append("OLS 状态：可用")
        elif method == "scm":
            bits.append("OLS 状态：SCM 可用")
        else:
            bits.append("OLS 状态：未加载")
        # 给有意义的诊断，而不是显示「？」
        if not (ols or method):
            if is_admin:
                bits.append("驱动未加载：WinRing0x64.sys 缺失或被拦截"
                             "（试试关 Core Isolation / 内存完整性）")
            else:
                bits.append("请先以管理员身份运行本程序，驱动才能加载")
        if d.get("last_error"):
            bits.append(f"根因：{d['last_error']}")

        lines = ["=== 风扇控制暴力测试 ===", "", " · ".join(bits), ""]
        for m in r.get("methods", []):
            label = m.get("label", "?")
            ok = m.get("write_ok", False)
            err = m.get("error")
            if ok:
                lines.append(f"  ✓ {label} — 写入成功，请确认风扇是否加速！")
            else:
                lines.append(f"  ✗ {label} — 失败({err})")
        note = r.get("note")
        if note:
            lines.append("")
            lines.append(note)
        self._result_lbl.setText("\n".join(lines))

    # -- 选择 / 高亮 ------------------------------------------------
    def _current_key(self) -> str | None:
        if 0 <= self._sel < len(self._choices):
            return self._choices[self._sel][0]
        return None

    def _select_widget(self, w: QWidget) -> None:
        for i, (_, btn) in enumerate(self._choices):
            if btn is w:
                self._sel = i
                self._highlight()
                return

    def _move(self, delta: int) -> None:
        if not self._choices:
            return
        self._sel = (self._sel + delta) % len(self._choices)
        self._highlight()

    def _highlight(self) -> None:
        for i, (_, btn) in enumerate(self._choices):
            btn.set_style("#7fd1ff" if i != self._sel else "#62e0a8",
                          active=(i == self._sel))

    def _confirm_choice(self, key: str | None) -> None:
        self._result_key = key
        self.accept()

    # -- 键盘导航 ----------------------------------------------------
    def keyPressEvent(self, ev: object) -> None:  # noqa: N802 - Qt camelCase
        from PyQt6.QtCore import Qt as _Qt
        k = getattr(ev, "key", lambda: -1)()
        if k in (_Qt.Key.Key_Left, _Qt.Key.Key_Up):
            self._move(-1)
        elif k in (_Qt.Key.Key_Right, _Qt.Key.Key_Down):
            self._move(1)
        elif k in (_Qt.Key.Key_Return, _Qt.Key.Key_Enter, _Qt.Key.Key_Space):
            self._confirm_choice(self._current_key())
        elif k == _Qt.Key.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(ev)  # type: ignore[call-arg]


def run_fan_brutetest_dialog(parent: QWidget) -> None:
    """暴力风扇测试：逐一尝试所有方法，让用户确认哪个真转了，并据此锁定映射。"""
    from ..platform import ec_io
    dlg = _BruteTestDialog(parent)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        # 取消：仍恢复自动，避免风扇被钉死在探测值
        try:
            ec_io.brute_force_restore()
        except Exception:  # noqa: BLE001
            pass
        blk = _get_fan_block(parent)
        if blk is not None and getattr(blk, "mode", "auto") != "auto":
            try:
                blk.apply()
            except Exception:  # noqa: BLE001
                pass
        return

    key = dlg._result_key
    if key:
        _apply_brute_mapping(parent, key)

    # 恢复自动（清零所有探测地址），若已锁定映射则把卡片当前配置重新下发
    ec_io.brute_force_restore()
    blk = _get_fan_block(parent)
    if blk is not None and getattr(blk, "mode", "auto") != "auto":
        try:
            blk.apply()
        except Exception:  # noqa: BLE001
            pass



class FanPanel(BlockPanel):
    """「风扇控制」卡片 UI（参考 PowerPanel 结构）。

    最小可用布局（用户决策方案 C-q2）：
      - 行 1：4 个模式按钮 Auto/Manual/Curve/Silent
      - 行 2：转速滑块（manual 模式下激活，0-255）
      - 行 3：状态文字（实时温度 / 当前转速百分比） + Apply 按钮

    曲线模式（curve）由 FanBlock 的 MetricSampler 回调 1Hz 后台驱动，
    失焦也运行（与 GPU 浮动同款）。silent 模式有过热保护：温度 ≥85℃
    时拒绝写入 0。本面板只渲染状态，不写 EC——EC 写入全在 FanBlock 里。
    """
    def _setup(self) -> None:
        b = self.instance
        # 行 1：4 个模式按钮
        mode_row = QHBoxLayout()
        self.auto_btn = _btn("Auto", self.accent)
        self.manual_btn = _btn("Manual", self.accent)
        self.curve_btn = _btn("Curve", self.accent)
        self.silent_btn = _btn("Silent", self.accent)
        mode_row.addWidget(self.auto_btn)
        mode_row.addWidget(self.manual_btn)
        mode_row.addWidget(self.curve_btn)
        mode_row.addWidget(self.silent_btn)
        self.root.addLayout(mode_row)

        # 行 2：转速滑块（manual 模式调手动转速，silent 模式调静音固定转速）
        pwm_row = QHBoxLayout()
        self.pwm_title = _label("手动转速")
        self.pwm_lbl = QLabel(f"{b.pwm_to_pct(b.manual_pct)}%")
        self.pwm_lbl.setStyleSheet("color: #dfe6f0; min-width: 70px; font-size: 13px;")
        pwm_row.addWidget(self.pwm_title)
        pwm_row.addWidget(self.pwm_lbl)
        pwm_row.addStretch(1)
        self.root.addLayout(pwm_row)

        self.pwm_slider = _NoScrollSlider(Qt.Orientation.Horizontal)
        self.pwm_slider.setRange(b.PWM_MIN, b.PWM_MAX)
        self.pwm_slider.setSingleStep(5)
        self.pwm_slider.setValue(int(b.manual_pct))
        self.root.addWidget(self.pwm_slider)

        # 设备识别状态（自动检测到的机型 + 协议 + 寄存器映射，常驻小字）
        self.device_lbl = QLabel("")
        self.device_lbl.setWordWrap(True)
        self.device_lbl.setStyleSheet("color: #7fd1ff; font-size: 10px;")
        self.root.addWidget(self.device_lbl)

        # 行 3：状态角标（右上角）+ Apply。状态文字改到卡片右上角角标显示。
        self.status_lbl = self._corner_lbl

        act = QHBoxLayout()
        self.apply = _btn("应用", self.accent)
        self.apply.set_gamepad_hint("Y")  # 手柄 Y = 应用
        act.addWidget(self.apply)
        self.root.addLayout(act)

        # 信号连接：模式按钮 / 滑块 / Apply（驱动自测 / 暴力测试已移至编辑项）
        self.pwm_slider.valueChanged.connect(self._on_pwm)
        self.auto_btn.clicked.connect(lambda: self._set_mode("auto"))
        self.manual_btn.clicked.connect(lambda: self._set_mode("manual"))
        self.curve_btn.clicked.connect(lambda: self._set_mode("curve"))
        self.silent_btn.clicked.connect(lambda: self._set_mode("silent"))
        self.apply.clicked.connect(self._confirm_apply)
        # 导航绑定（手柄/键盘焦点流转）
        self._bind(self.pwm_slider, "slider")
        self._bind(self.auto_btn, "toggle")
        self._bind(self.manual_btn, "toggle")
        self._bind(self.curve_btn, "toggle")
        self._bind(self.silent_btn, "toggle")
        self._bind(self.apply, "action")

        # 鼠标拖动防抖自动下发定时器（单发）。手柄/键盘方向键微调不走此定时器，
        # 等「再摁一次 A / Enter」确认（_confirm_apply）才真正写 EC，避免误触即生效
        # 与「拖动每帧都写 EC」导致的卡顿/抖动。
        self._apply_timer = QTimer(self)
        self._apply_timer.setSingleShot(True)
        self._apply_timer.setInterval(400)
        self._apply_timer.timeout.connect(self._flush_apply)
        # 标记「当前 valueChanged 是否来自手柄/键盘方向键微调」。
        self._nudging = False

        # EC 诊断行（常驻小字，让用户一眼看到「是否管理员 / GetDllStatus」，
        # 直接定位 WinRing0 加载失败的根因，而不是笼统的「加载失败」）
        self.diag_lbl = QLabel("")
        self.diag_lbl.setWordWrap(True)
        self.diag_lbl.setStyleSheet("color: #ff9a9a; font-size: 10px;")
        self.root.addWidget(self.diag_lbl)
        self._refresh_diag()

    # -- EC 诊断（指向 WinRing0 加载失败的根因）----------------------
    def _refresh_diag(self) -> None:
        try:
            from ..platform import ec_io
        except Exception:  # noqa: BLE001
            return
        d = ec_io.diag_info()
        admin = d.get("is_admin")
        st = d.get("getdllstatus")
        method = d.get("method", "")
        # OLS 成功 / SCM 成功 → 清诊断
        if (method == "ols" and st in (0, 2)) or method == "scm":
            self.diag_lbl.setText("")
            self.diag_lbl.setVisible(False)  # 空文字不占位，避免 Apply 下方留白
            return
        bits = [f"管理员:{'是' if admin else '否'}"]
        if method:
            bits.append(f"方式:{method.upper()}")
        if st is not None:
            bits.append(f"GetDllStatus={st}")
        if not d.get("dll_path"):
            bits.append("WinRing0x64.sys 缺失")
        txt = " · ".join(bits)
        if admin is not True:
            txt += " — 请右键 exe「以管理员身份运行」；源码模式需在管理员终端启动 python run.py"
        if d.get("last_error"):
            txt += " · " + d["last_error"]
        self.diag_lbl.setText("EC 诊断：" + txt)
        self.diag_lbl.setVisible(True)  # 有诊断信息才显示，否则隐藏不占位

    # -- 模式切换（不立即写 EC，等 Apply）----------------------------
    def _set_mode(self, mode: str) -> None:
        self.instance.set_mode(mode)
        self.sync()

    # -- Apply 确认 --------------------------------------------------
    def _confirm_apply(self) -> None:
        ok = self.instance.apply()
        if not ok:
            # 失败时弹窗级明确报错，而不是只靠小字 toast（用户常忽略 → 以为无效果）
            from ..platform import ec_io
            d = ec_io.diag_info()
            msg = "风扇控制未生效。\n\n"
            if not d.get("is_admin"):
                msg += "• 程序未以管理员身份运行：请右键 exe →「以管理员身份运行」。\n"
            if d.get("last_error"):
                msg += f"• 根因：{d['last_error']}\n"
            else:
                msg += ("• 驱动已加载但 EC 写入未生效：可能机型/寄存器不匹配，"
                        "或需在风扇编辑项里点「驱动自测」确认链路。\n")
            msg += f"\n（管理员:{d.get('is_admin')} · 错误码:{d.get('last_error_code')}）"
            QMessageBox.warning(self, "风扇控制失败", msg)
        self.sync()

    # -- 驱动自测 / 暴力测试 已移至编辑项（FanEditPanel），卡片只保留应用 --

    # -- 滑块微调（方向键 ±5）----------------------------------------
    def _nudge_pwm(self, delta: int) -> bool:
        s = self.pwm_slider
        nv = max(s.minimum(), min(s.maximum(), s.value() + delta * s.singleStep()))
        if nv == s.value():
            return False
        # 标注「这是手柄/键盘方向键微调」，让 _on_pwm 走「不自动写 EC」分支。
        self._nudging = True
        try:
            s.setValue(nv)
        finally:
            self._nudging = False
        return True

    # -- 导航矩阵：NavItem（手动模式含转速滑块，其余模式隐藏）--------
    def nav_items(self) -> list:
        # 4 个模式按钮 + Apply 常驻；转速滑块仅在 manual 模式显示时纳入导航
        # （auto/curve/silent 模式整行隐藏，滑块不可聚焦、confirm 也 guard 跳过）。
        # 手柄导航按 A 进入 + 方向键移焦 + X 切模式 + Y 直接应用。
        items = [
            NavItem(self.auto_btn, "toggle", confirm=lambda: self._set_mode("auto")),
            NavItem(self.manual_btn, "toggle", confirm=lambda: self._set_mode("manual")),
            NavItem(self.curve_btn, "toggle", confirm=lambda: self._set_mode("curve")),
            NavItem(self.silent_btn, "toggle", confirm=lambda: self._set_mode("silent")),
            NavItem(self.apply, "action", confirm=self._confirm_apply, hint="Y"),
        ]
        if self.instance.mode == "manual":
            items.insert(
                0,
                NavItem(self.pwm_slider, "slider",
                        confirm=self._confirm_apply, nudge=self._nudge_pwm),
            )
        return items

    # -- 滑块响应 ---------------------------------------------------
    def _on_pwm(self, v: int) -> None:
        b = self.instance
        if b.mode == "silent":
            b.set_silent_pct(v)
        else:
            b.set_manual_pct(v)
        if self._nudging:
            # 手柄/键盘方向键微调：绝不自动写 EC，等用户「再摁一次 A / Enter」
            # 确认（_confirm_apply）才真正下发。
            self._apply_timer.stop()
        elif b.applied and b.mode in ("manual", "silent"):
            # 鼠标拖动：已 applied 且 manual/silent 模式下防抖自动写 EC。
            self._apply_timer.start()
        else:
            self._apply_timer.stop()
        self.sync()

    def _flush_apply(self) -> None:
        """防抖定时器到点：真正写 EC（仅已 applied 且 manual/silent 模式）。"""
        b = self.instance
        if b.applied and b.mode in ("manual", "silent"):
            b.apply()
        self.sync()

    # -- 状态刷新（被 _fast 1Hz 调用）-------------------------------
    def sync(self) -> None:
        b = self.instance
        # 转速滑块行（标题 + 数值 + 滑块）仅在 manual 模式显示；auto/curve/
        # silent 模式直接隐藏整行（silent 转速改在编辑项里调）。
        show_pwm = (b.mode == "manual")
        self.pwm_title.setVisible(show_pwm)
        self.pwm_lbl.setVisible(show_pwm)
        self.pwm_slider.setVisible(show_pwm)
        active_pct = b.silent_pct if b.mode == "silent" else b.manual_pct
        self.pwm_slider.blockSignals(True)
        self.pwm_slider.setValue(int(active_pct))
        self.pwm_slider.blockSignals(False)
        if b.mode == "silent":
            self.pwm_title.setText("静音转速")
            self.pwm_lbl.setText(f"{b.pwm_to_pct(b.silent_pct)}%")
        else:
            self.pwm_title.setText("手动转速")
            self.pwm_lbl.setText(f"{b.pwm_to_pct(b.manual_pct)}%")
        _set(self.auto_btn, "Auto", self.accent, b.mode == "auto")
        _set(self.manual_btn, "Manual", self.accent, b.mode == "manual")
        _set(self.curve_btn, "Curve", self.accent, b.mode == "curve")
        _set(self.silent_btn, "Silent", self.accent, b.mode == "silent")
        _set(self.apply, "应用", self.accent, b.applied)
        # 设备识别状态（自动检测到的机型 + 协议 + 寄存器映射）
        self._refresh_device()
        # 实时状态文字（curve 模式显示温度 + 实际转速 RPM，统一用百分比单位）
        rpm_txt = b.fan_rpm_display()
        if b.applied:
            if b.mode == "auto":
                self._status("✓ BIOS 自动控制", "#4be38a")
            elif b.mode == "manual":
                self._status(f"✓ 手动 {b.pwm_to_pct(b.manual_pct)}% · {rpm_txt}",
                             "#4be38a")
            elif b.mode == "curve":
                t = b.last_temp_c
                if t is not None:
                    self._status(
                        f"✓ 曲线 · CPU {t:.0f}℃ · {rpm_txt}",
                        "#4be38a")
                else:
                    self._status("✓ 曲线 · 温度读取中", "#9aa6b8")
            elif b.mode == "silent":
                if b.silent_pct <= 0:
                    self._status("✓ 静音=BIOS控制", "#4be38a")
                else:
                    self._status(
                        f"✓ 静音 {b.pwm_to_pct(b.silent_pct)}% · {rpm_txt}",
                        "#4be38a")
            self.diag_lbl.setText("")  # 已成功应用 → 清掉诊断提示
            self.diag_lbl.setVisible(False)  # 空文字不占位，避免 Apply 下方留白
        else:
            err = getattr(self.instance, "last_ec_error", "")
            if err:
                self._status("✗ " + err, "#e68080")
            else:
                self._status("待应用", "#ffd166")
            self._refresh_diag()  # 未应用（含失败）→ 刷新 EC 诊断

    def _refresh_device(self) -> None:
        """刷新设备识别状态小字（自动检测结果，只读展示）。"""
        b = self.instance
        quirk = getattr(b, "board_quirk", "") or ""
        proto = "2E2F" if quirk == "win4" else "4E4F"
        enable = getattr(b, "ec_enable_addr", 0)
        pwm = getattr(b, "ec_pwm_addr", 0)
        model = getattr(b, "detected_model", "") or ""
        if model:
            self.device_lbl.setText(
                f"机型 {model} · {quirk} · {proto} · 使能={enable} 转速={pwm}")
        else:
            self.device_lbl.setText(
                f"映射 {quirk} · {proto} · 使能={enable} 转速={pwm}")

    def _status(self, text: str, color: str) -> None:
        self._set_corner(text, color)

    # -- 慢定时器钩子：刷新温度读数（curve 模式必备）----------------
    def heavy_refresh(self) -> "Callable[[], None] | None":
        b = self.instance
        # curve 模式由 MetricSampler 后台驱动；非 curve 模式仅在窗口聚焦时
        # 用 _slow 定时器刷新温度读数显示（不写 EC，只读显示）。
        if b.mode in ("curve", "manual", "silent"):
            return lambda: b.update_live_readout()
        return None


# --------------------------------------------------------------------------
# undervolt — 降压控制（负载门控 Curve Optimizer）
# --------------------------------------------------------------------------
class UndervoltPanel(BlockPanel):
    """独立的「降压控制」卡片（与 power 卡片解耦）。

    真正的状态机由主窗口的 UndervoltController 驱动；本卡片只负责交互：
      - 自动降压（状态切换按钮，手柄提示 Y）：切换 UndervoltController.enabled
      - 关闭降压：关自动 + 立即把电压钉在 -20（键盘安全的静态档，一次性）
      - 降压 -30mV（状态按钮）：开启后钉 -30 并持久化，开机/休眠恢复自动重钉；
        关闭即归零并清持久态（与自动降压互斥）
    录制标定逻辑已迁至降压控制设置项（UndervoltEditPanel）。
    """

    def _win(self):
        w = self.window()
        if w is not None and hasattr(w, "undervolt_status"):
            return w
        return None

    def _install_toast(self) -> None:
        """本卡片对应的 UndervoltBlock 是虚拟块（无 .ui / show_toast），
        故自行构建卡片级 toast（toast_label + toast_panel + _uv_ui），
        使「录制 / 固定 / 错误」等反馈走统一的右上角 toast，与全局风格一致。
        """
        self.toast_label = _Label()
        self.toast_panel = _Toast(self.toast_label, self)
        self._uv_ui = {
            "toast_label": self.toast_label,
            "toast_panel": self.toast_panel,
        }

    def _toast(self, text: str, success: bool = True) -> None:
        _show_block_toast(self._uv_ui, text, success=success)

    def _setup(self) -> None:
        # 三个按钮同一排、均匀分布（三等分）：
        #   - 自动降压：状态切换，手柄提示 Y（原「开始录制」的 Y 空出后归它）
        #   - 关闭降压：一键钉 -20（一次性，不算持久状态）
        #   - 降压 -30mV：状态按钮，开启后开机/休眠恢复自动重钉（持久状态）
        row = QHBoxLayout()
        row.setSpacing(8)
        self.toggle_btn = _btn("自动降压", self.accent)
        self.pin_btn = _btn("关闭降压", self.accent)
        self.m30_btn = _btn("降压 -30mV", self.accent)
        row.addWidget(self.toggle_btn, 1)
        row.addWidget(self.pin_btn, 1)
        row.addWidget(self.m30_btn, 1)
        self.root.addLayout(row)
        # 单一激活路径：经 _bind + nav_items.confirm。
        # 注意：不要再加 clicked.connect(_on_xxx) 直接连接——_bind 已把 clicked
        # 接到 activate_widget→confirm，双连接会让状态按钮被翻两次（净无变化）。
        self._bind(self.toggle_btn, "toggle")
        self._bind(self.pin_btn, "action")
        self._bind(self.m30_btn, "action")

        # 实时状态角标（右上角）
        self.status_lbl = self._corner_lbl

        self.sync()

    # -- 交互 ------------------------------------------------------------
    def _on_toggle(self) -> None:
        win = self._win()
        if win is None:
            return
        en = not bool(win.undervolt_status().get("enabled"))
        win.undervolt_set_flag("enabled", en)
        self.sync()

    def _on_pin(self) -> None:
        win = self._win()
        if win is None:
            return
        win.undervolt_pin_static(-20)
        self._set_status("已固定 CO=-20（一次性，非持久状态）", "#4be38a")
        self.sync()

    def _on_pin_m30(self) -> None:
        """降压 -30mV 状态按钮：开/关切换。

        激活（开）：钉 -30 + 持久化（开机 / 休眠恢复自动重钉），并关掉自动降压。
        关闭：立即归零 + 清持久态（见 win.undervolt_clear_pinned）。
        """
        win = self._win()
        if win is None:
            return
        st = win.undervolt_status()
        if st.get("pinned_mv"):
            win.undervolt_clear_pinned()
            self._set_status("已关闭 -30mV 固定档，CO 归零", "#9aa6b8")
        else:
            win.undervolt_set_pinned(-30)
            self._set_status("已固定 CO=-30（开机/休眠恢复自动重钉）", "#4be38a")
        self.sync()

    def _set_status(self, text: str, color: str = "#9aa6b8") -> None:
        # 卡片右上角角标只保留降压浮动/忙闲状态（见 sync()）；其余反馈
        # （录制/固定/错误等）统一走 toast，避免角标被瞬时消息覆盖。
        success = not (color.startswith("#ff") or color.startswith("#e6"))
        self._toast(text, success=success)

    # -- 实时刷新 --------------------------------------------------------
    def sync(self) -> None:
        win = self._win()
        if win is None:
            return
        try:
            st = win.undervolt_status()
        except Exception:
            return
        en = bool(st.get("enabled"))
        pinned = bool(st.get("pinned_mv"))
        # 三按钮同排、均匀分布：激活态用填充色区分开关（避免「：开/关」长文本挤占
        # 1/3 宽度，掌机 1080p@150% 下更清晰）。
        _set(self.toggle_btn, "自动降压", self.accent, en)
        _set(self.pin_btn, "关闭降压", self.accent, False)
        _set(self.m30_btn, "降压 -30mV", self.accent, pinned)
        # 右上角角标：固定档优先显示持久化的 -30，否则按自动降压实时状态
        state = st.get("state", "idle")
        mv = int(st.get("mv", 0))
        if pinned:
            float_tag, busy_tag, color = "固定", "闲", "#7fd1ff"
            mv = int(st.get("pinned_mv", mv))
        elif not en:
            float_tag, busy_tag, color = "关闭", "闲", "#9aa6b8"
        elif state in ("light", "heavy"):
            float_tag, busy_tag, color = "浮动", "忙", "#4be38a"
        else:
            float_tag, busy_tag, color = "固定", "闲", "#9aa6b8"
        self._set_corner(f"{float_tag}·{busy_tag} CO={mv}", color)

    def nav_items(self) -> list:
        return [
            # 自动降压：状态切换，手柄提示 Y（原「开始录制」的 Y 空出后归它）
            NavItem(self.toggle_btn, "toggle",
                    confirm=self._on_toggle, hint="Y"),
            NavItem(self.pin_btn, "action", confirm=self._on_pin),
            # 降压 -30mV：状态按钮（单一激活路径，防 clicked 双触发翻两次）
            NavItem(self.m30_btn, "action", confirm=self._on_pin_m30),
        ]


# --------------------------------------------------------------------------
# freeze — suspend / resume
# --------------------------------------------------------------------------
class FreezePanel(BlockPanel):
    CAND_KEYS = {0: 0, 9: 1, 8: 2, 7: 3, 6: 4}
    FROZ_KEYS = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4}
    # slot index -> digit shown on the button / used as the hotkey
    CAND_SLOT_TO_KEY = {v: k for k, v in CAND_KEYS.items()}
    FROZ_SLOT_TO_KEY = {v: k for k, v in FROZ_KEYS.items()}

    def _setup(self) -> None:
        self.cand_btns: list[QPushButton] = []
        self.froz_btns: list[QPushButton] = []
        # 手柄 Y 双击判定：单次 Y 延迟 _y_dbl_window_s 执行「加入」，给双击留
        # 判定窗口；若窗口内再次按 Y（InteractionController 触发 action_y_dbl）
        # 则取消待执行的加入、改为移出白名单（否则会「先加后删」净无操作）。
        # 这与 interaction.py 的 _Y_DOUBLE_S=0.4 对齐。
        self._y_dbl_window_s = 0.4
        self._y_pending_timer = None
        self._y_pending_proc = None
        cap = getattr(self.instance, "LIST_CAP", 5)

        self.cand_label = _label("凝冰结魄", color="#8fd3ff")
        self.root.addWidget(self.cand_label)
        # 用 _FitScrollArea 承载候选列表：其 minimumSizeHint 水平分量被钉死为
        # 60，卡片最小宽度不再被超长进程名（如 Microsoft.ServiceHub.Controller
        # .exe）撑开，从而在小分辨率（720p 窗口仅 360px）下也不会把整张卡片/
        # 窗口顶宽，超长名改为在卡片内部裁切（与 ProcMon/LS 列表一致）。
        # Use _HeightFitScrollArea so the list height tracks the number of
        # visible process rows; a plain QScrollArea can under-report the height
        # and clip rows under HiDPI scaling.
        self.cand_scroll, self.cand_box, _ = _scroll_box(_HeightFitScrollArea)
        self.cand_box.setSpacing(4)
        # 候选列表（可冻结进程）外框已去除（按需求去掉浅灰色边框），仅保留透明容器包裹所有进程项。
        self.cand_frame = QFrame()
        self.cand_frame.setFrameStyle(QFrame.Shape.NoFrame)
        self.cand_frame.setStyleSheet(
            "QFrame { border: none; background: transparent; }")
        _cand_fb = QVBoxLayout(self.cand_frame)
        _cand_fb.setContentsMargins(0, 0, 0, 0)
        _cand_fb.setSpacing(0)
        _cand_fb.addWidget(self.cand_scroll)
        self.root.addWidget(self.cand_frame)
        for _ in range(cap):
            btn = _btn("", self.accent)
            btn._left_align = True  # 序号贴左缘，便于「按数字键」一目了然
            btn.set_hint_side("right")  # Y 提示固定在按钮右侧（仅选中时显示）
            btn.setVisible(False)
            self.cand_box.addWidget(btn)
            self.cand_btns.append(btn)

        # 候选列表为空时的占位提示：居中淡灰小字，替换原本空着的一大片高度。
        self.cand_empty = _label("（今天也是空闲的一天呢……）", color="#6b7689", size=12)
        self.cand_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cand_empty.setVisible(False)
        # 插在候选标题与已冻结标题之间（root 中 cand_label=0、cand_scroll=1）。
        self.root.insertWidget(2, self.cand_empty)

        self.froz_label = _label("霜华满地", color="#ffc08a")
        self.root.addWidget(self.froz_label)
        # 与候选列表同理：_FitScrollArea 防止超长进程名撑宽卡片/窗口。
        self.froz_scroll, self.froz_box, _ = _scroll_box(_HeightFitScrollArea)
        self.froz_box.setSpacing(4)
        # 已冻结列表外框已去除（按需求去掉浅灰色边框），仅保留透明容器包裹所有已冻结进程项。
        self.froz_frame = QFrame()
        self.froz_frame.setFrameStyle(QFrame.Shape.NoFrame)
        self.froz_frame.setStyleSheet(
            "QFrame { border: none; background: transparent; }")
        _froz_fb = QVBoxLayout(self.froz_frame)
        _froz_fb.setContentsMargins(0, 0, 0, 0)
        _froz_fb.setSpacing(0)
        _froz_fb.addWidget(self.froz_scroll)
        self.root.addWidget(self.froz_frame)

        # Freeze lists are bounded by LIST_CAP (<=5 rows), so they can never
        # grow unbounded.  The shared _scroll_box() caps every list at 180px,
        # which breaks this card two ways:
        #   (a) it clips the last candidate row under HiDPI scaling — the
        #       buttons use Fixed + setMinimumHeight(29), so at 150% they are
        #       ~43px and 5 rows exceed 180px and get cut off;
        #   (b) _FitScrollArea.minimumSizeHint() is non-zero (~70px) even when
        #       every child is hidden, so an empty list still reserves blank
        #       space.
        # Lift the cap (page scroll handles any overflow) and let the lists size
        # to their content.  The empty frozen list is hidden entirely in sync().
        for _sc in (self.cand_scroll, self.froz_scroll):
            _sc.setMinimumHeight(0)
            _sc.setMaximumHeight(100000)
        for _ in range(cap):
            btn = _btn("", self.accent)
            btn._left_align = True  # 序号贴左缘
            btn.set_hint_side("right")  # Y 提示固定在按钮右侧（仅选中时显示）
            btn.setVisible(False)
            self.froz_box.addWidget(btn)
            self.froz_btns.append(btn)

        # 底部常驻手柄快捷键说明（不随导航状态开关，永远显示）：仅在冻结卡内
        # 列出专属手柄键，避免和「进入/返回」这类通用导航键重复。功能逻辑见
        # nav_items 的 action_x / action_y*，移除光标选中时的 X 徽章不影响它们。
        self.gp_hint_bar = GamepadHintBar([
            ("X", "关闭进程"),
            ("Y", "标为游戏·双击移出"),
        ])
        self.root.addWidget(self.gp_hint_bar)

        for i, btn in enumerate(self.cand_btns):
            self._bind(btn, "action")
        for i, btn in enumerate(self.froz_btns):
            self._bind(btn, "action")
        for btn in (*self.cand_btns, *self.froz_btns):
            btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            btn.customContextMenuRequested.connect(
                lambda pos, b=btn: self._ctx_menu(pos, b))

    def sync(self) -> None:
        b = self.instance
        # 游戏白名单（冻结卡片「标为游戏」实时维护）。用于在本列表里给已标记为
        # 游戏的进程加 🎮 特殊标记，让用户一眼看出哪些进程正被当游戏对待。
        wl_set: set[str] = set()
        win = self.window()
        if win is not None:
            try:
                wl_set = {_norm_exe(x) for x in win.undervolt_get_whitelist()}
            except Exception:
                wl_set = set()
        get_exe = None
        if wl_set:
            try:
                from ..core.game_detect import get_exe_by_pid
                get_exe = get_exe_by_pid
            except Exception:
                get_exe = None

        def _game_tag(pid: int) -> str:
            if not (wl_set and get_exe and pid):
                return ""
            try:
                if _norm_exe(get_exe(pid)) in wl_set:
                    return "  🎮"
            except Exception:
                pass
            return ""

        for i, btn in enumerate(self.cand_btns):
            key = self.CAND_SLOT_TO_KEY.get(i, "")
            if i < len(b.candidates):
                p = b.candidates[i]
                name = getattr(p, "name", "?")
                mem = int(getattr(p, "memory_mb", 0) or 0)
                tag = _game_tag(int(getattr(p, "pid", 0) or 0))
                btn.setVisible(True)
                _set(btn, f"{key}  ❄ {name}{tag}  {mem}MB", self.accent, False)
                btn._proc = p
            else:
                btn.setVisible(False)
        # Candidate section: header always visible; when empty, hide the (now
        # zero-height) scroll list and show a faint centered placeholder instead
        # of leaving a tall blank gap.
        self.cand_label.setVisible(True)
        cand_empty = not b.candidates
        self.cand_scroll.setVisible(not cand_empty)
        self.cand_frame.setVisible(not cand_empty)
        self.cand_empty.setVisible(cand_empty)

        for i, btn in enumerate(self.froz_btns):
            key = self.FROZ_SLOT_TO_KEY.get(i, "")
            if i < len(b.frozen):
                e = b.frozen[i]
                mem = f"  {e.mem}MB" if e.mem else ""
                extra = f"  (+{len(getattr(e, 'pids', [e.pid])) - 1})" if len(getattr(e, "pids", [e.pid])) > 1 else ""
                tag = _game_tag(int(getattr(e, "pid", 0) or 0))
                btn.setVisible(True)
                _set(btn, f"{key}  🔥 {e.name or e.pid}{extra}{tag}{mem}", self.accent, False)
                btn._proc = e
            else:
                btn.setVisible(False)
        # Collapse the whole frozen section when nothing is frozen, so it no
        # longer reserves blank space on the card ("好多空间").  When a process
        # is frozen later, sync() flips these back on automatically.  Navigation
        # already skips hidden buttons, so hiding is safe.
        frozen_empty = not b.frozen
        self.froz_label.setVisible(not frozen_empty)
        self.froz_scroll.setVisible(not frozen_empty)
        self.froz_frame.setVisible(not frozen_empty)

        # Explicitly resize the lists to match the visible row count.  The stock
        # QScrollArea sizeHint can lag behind visibility changes (especially
        # under HiDPI / button-style presets), leaving the last row clipped.
        self._resize_lists()

        # Highlight the slot selected by the first press (two-press flow).
        self._apply_highlight()

    def _resize_lists(self) -> None:
        """Set each list's minimum height from its visible row count.

        This forces the layout to allocate enough vertical space even when
        ``QScrollArea.sizeHint()`` under-reports the inner widget's height.
        """
        self._set_list_height(
            self.cand_btns, self.cand_box, self.cand_scroll,
            len(getattr(self.instance, "candidates", [])))
        self._set_list_height(
            self.froz_btns, self.froz_box, self.froz_scroll,
            len(getattr(self.instance, "frozen", [])))

    def _set_list_height(self, btns: list, box: QVBoxLayout, scroll: QScrollArea,
                         count: int) -> None:
        cap = getattr(self.instance, "LIST_CAP", 5)
        n = min(max(count, 0), cap)
        if n == 0:
            scroll.setMinimumHeight(0)
            scroll.updateGeometry()
            return
        # row height: use the button's own minimum (already includes padding /
        # minimumHeight) so we match the real drawn height at any DPI.
        row_h = max(btns[0].minimumSizeHint().height(), 29)
        h = n * row_h + max(0, n - 1) * box.spacing() + 4
        scroll.setMinimumHeight(h)
        scroll.updateGeometry()

    def _apply_highlight(self) -> None:
        b = self.instance
        hk = getattr(b, "highlight_key", None)
        # 区块层 ``highlight_key`` 存的是「按键值」（候选键 0/9/8/7/6，已冻结键
        # 1/2/3/4/5），必须用 KEY→SLOT 的正向映射去定位高亮行；反向的
        # slot→key 字典（rev_*）会把候选键 0 误映射到已冻结槽 1、且候选槽 1~4
        # （键 9/8/7/6）根本不在 slot 索引字典里 → 高亮错位 / 漏亮。这是上一版
        # 就存在的隐藏 bug，借 Y 提示只亮选中行才暴露出来。
        # 注意：选中行不再绘制 X 手柄徽章（已移到卡底常驻提示栏），但 X 的
        # 关闭进程功能由 nav_items 的 action_x 保留，快捷键不受影响。
        for i, btn in enumerate(self.cand_btns):
            active = hk is not None and hk in self.CAND_KEYS and self.CAND_KEYS[hk] == i
            if btn.isVisible():
                _set(btn, btn.text(), self.accent, active)
        for i, btn in enumerate(self.froz_btns):
            active = hk is not None and hk in self.FROZ_KEYS and self.FROZ_KEYS[hk] == i
            if btn.isVisible():
                _set(btn, btn.text(), self.accent, active)

    def set_card_selected(self, selected: bool) -> None:
        prev = bool(getattr(self, "_card_selected", False))
        super().set_card_selected(selected)
        # 卡片选中态变化后立刻刷新手柄提示：离开冻结卡时清掉残留的 Y 徽章，
        # 进入时按当前选中行恢复——避免依赖每秒定时 sync 才清除的约 1 秒残影。
        if prev != bool(selected):
            self._apply_highlight()

    def nav_items(self) -> list:
        items: list = []
        b = self.instance
        rev_cand = {i: k for k, i in self.CAND_KEYS.items()}
        rev_froz = {i: k for k, i in self.FROZ_KEYS.items()}

        def _focus_cb(focused: bool, key: int) -> None:
            # Leaving a candidate/frozen slot cancels any pending two-press
            # (the block manages highlight_key on its own via handle_hotkey).
            if not focused:
                try:
                    setattr(self.instance, "highlight_key", None)
                    self.sync()
                except Exception:
                    pass

        for i, btn in enumerate(self.cand_btns):
            if not btn.isVisible():
                continue
            k = rev_cand.get(i)
            if k is None:
                continue
            p = b.candidates[i]
            items.append(NavItem(
                btn, "action",
                confirm=lambda k=k: (self.instance.handle_hotkey(k), self.sync()),
                on_focus=lambda f, k=k: _focus_cb(f, k),
                action_y=lambda p=p: self._toggle_game(p),
                action_y_gp=lambda p=p: self._y_single(p),
                action_y_dbl=lambda p=p: self._y_double(p),
                action_x=lambda p=p: self._close_process(p),
                hint="",
                no_bulk_hint=True,
            ))
        for i, btn in enumerate(self.froz_btns):
            if not btn.isVisible():
                continue
            k = rev_froz.get(i)
            if k is None:
                continue
            e = b.frozen[i]
            items.append(NavItem(
                btn, "action",
                confirm=lambda k=k: (self.instance.handle_hotkey(k), self.sync()),
                on_focus=lambda f, k=k: _focus_cb(f, k),
                action_y=lambda e=e: self._toggle_game(e),
                action_y_gp=lambda e=e: self._y_single(e),
                action_y_dbl=lambda e=e: self._y_double(e),
                action_x=lambda e=e: self._close_process(e),
                hint="",
                no_bulk_hint=True,
            ))
        return items

    # -- 标为游戏：加入 / 移出 降压白名单（toggle）-----------------------
    def _find_uv_win(self):
        """沿父控件链找到带 undervolt_*_whitelist 的主窗口。

        FreezePanel 继承 BlockPanel，基类不提供 _win()；显式父链解析，
        同时覆盖右键模态菜单里 self.window() 可能取不到顶层窗口的情况。
        """
        obj = self.window()
        while obj is not None and not hasattr(obj, "undervolt_add_whitelist"):
            obj = obj.parent()
        return obj

    def _proc_exe(self, proc) -> str:
        """取进程 exe（优先按 PID 取真实 exe，与白名单匹配口径一致；取不到
        再退回进程名）。返回归一化前的原始 exe 串（主窗口 API 会再归一化）。"""
        pid = int(getattr(proc, "pid", 0) or 0)
        exe = None
        if pid:
            try:
                from ..core.game_detect import get_exe_by_pid
                exe = get_exe_by_pid(pid)
            except Exception:
                exe = None
        if not exe:
            exe = (getattr(proc, "name", "") or "").strip().lower()
        return exe or ""

    def _is_whitelisted(self, proc) -> bool:
        """该进程当前是否已在游戏白名单内。"""
        exe = self._proc_exe(proc)
        if not exe:
            return False
        win = self._find_uv_win()
        if win is None:
            return False
        try:
            wl = win.undervolt_get_whitelist()
        except Exception:
            return False
        return _norm_exe(exe) in {_norm_exe(x) for x in wl}

    def _mark_game(self, proc) -> None:
        """把某进程标为游戏：取其 exe 加入 undervolt 游戏白名单并落盘。

        优先用 PID 取真实 exe（与白名单匹配口径一致）；取不到再退回进程名。
        幂等：已在白名单时弹「已在白名单」而非静默无反应。
        """
        exe = self._proc_exe(proc)
        if not exe:
            self.instance.show_toast("无法取得进程名，未加入白名单", success=False)
            return
        pid = int(getattr(proc, "pid", 0) or 0)
        win = self._find_uv_win()
        if win is None:
            self.instance.show_toast("找不到主窗口（无法调用 undervolt API）", success=False)
            return
        try:
            ok = win.undervolt_add_whitelist(exe)
        except Exception as ex:
            # 把完整 traceback 落盘，便于精确定位缺失的属性/方法
            try:
                _log = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "..", "logs", "mark_game_err.log")
                with open(_log, "a", encoding="utf-8") as fh:
                    fh.write("=== %s ===\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
                    fh.write("exe=%r pid=%d\n" % (exe, pid))
                    fh.write(traceback.format_exc() + "\n")
            except Exception:
                pass
            self.instance.show_toast(f"加入白名单失败：{ex}", success=False)
            return
        if ok:
            self.instance.show_toast(f"已加入游戏白名单：{exe}", success=True)
        else:
            self.instance.show_toast(f"已在白名单：{exe}", success=False)
        self.sync()

    def _unmark_game(self, proc) -> None:
        """把某进程移出游戏白名单并落盘（与 _mark_game 对称）。"""
        exe = self._proc_exe(proc)
        if not exe:
            self.instance.show_toast("无法取得进程名，未移出白名单", success=False)
            return
        win = self._find_uv_win()
        if win is None:
            self.instance.show_toast("找不到主窗口（无法调用 undervolt API）", success=False)
            return
        try:
            ok = win.undervolt_remove_whitelist(exe)
        except Exception as ex:
            self.instance.show_toast(f"移出白名单失败：{ex}", success=False)
            return
        if ok:
            self.instance.show_toast(f"已移出游戏白名单：{exe}", success=True)
        else:
            self.instance.show_toast(f"不在白名单：{exe}", success=False)
        self.sync()

    def _toggle_game(self, proc) -> None:
        """键盘 W / 通用 toggle：在白名单内则移出、不在则加入。"""
        if self._is_whitelisted(proc):
            self._unmark_game(proc)
        else:
            self._mark_game(proc)

    # -- 手柄 X / 键盘退格：关闭选中的进程（候选或已冻结）------------
    def _close_process(self, proc) -> None:
        """手柄 X / 键盘 退格：关闭当前选中的进程。

        委托给 FreezeBlock.close_process（业务层，含受保护系统进程防护与
        已冻结单元的整体移除）。关闭是异步的（taskkill /F），结果由块的
        完成回调刷新面板；这里同步触发一次 repaint 仅作即时反馈。"""
        self.instance.close_process(proc)
        try:
            self.sync()
        except Exception:
            pass

    # -- 手柄 Y：单次加入（延迟以留双击窗口）/ 双击移出 ----------------
    def _y_cancel_pending(self) -> None:
        t = getattr(self, "_y_pending_timer", None)
        if t is not None:
            try:
                t.stop()
            except Exception:
                pass
        self._y_pending_timer = None
        self._y_pending_proc = None

    def _y_single(self, proc) -> None:
        """手柄 Y 单次：延迟 _y_dbl_window_s 执行「加入」，给双击留判定窗口。
        若窗口内再次按 Y（InteractionController 触发 action_y_dbl）会取消此次
        加入并改为移出，避免「先加后删」净无操作。
        若本就已在白名单，立即提示「再次双击 Y 移出」，让移除手势可被发现。"""
        if self._is_whitelisted(proc):
            self.instance.show_toast(
                f"再次双击 Y 移出白名单：{self._proc_exe(proc)}", success=False)
        self._y_cancel_pending()
        self._y_pending_proc = proc
        self._y_pending_timer = QTimer(self)
        self._y_pending_timer.setSingleShot(True)
        self._y_pending_timer.timeout.connect(
            lambda: self._y_commit_add(proc))
        self._y_pending_timer.start(int(self._y_dbl_window_s * 1000))

    def _y_commit_add(self, proc) -> None:
        self._y_pending_timer = None
        self._y_pending_proc = None
        if self._is_whitelisted(proc):
            return  # 已在则不加（幂等，避免重复 toast）
        self._mark_game(proc)

    def _y_double(self, proc) -> None:
        """手柄 Y 双击：取消待执行的「加入」，改为移出（若已在白名单）。
        未加入白名单时双击无意义，给提示而非静默。"""
        self._y_cancel_pending()
        if self._is_whitelisted(proc):
            self._unmark_game(proc)
        else:
            self.instance.show_toast(
                f"尚未加入白名单：{self._proc_exe(proc)}", success=False)

    def _ctx_menu(self, pos, btn) -> None:
        """右键候选/已冻结进程 → 按当前状态给出「加入」或「移出」白名单。"""
        proc = getattr(btn, "_proc", None)
        if proc is None:
            return
        menu = QMenu(self)
        if self._is_whitelisted(proc):
            act = QAction("移出游戏白名单", self)
            act.triggered.connect(
                lambda _checked=False, p=proc: self._unmark_game(p))
        else:
            act = QAction("加入游戏白名单（标为游戏）", self)
            act.triggered.connect(
                lambda _checked=False, p=proc: self._mark_game(p))
        menu.addAction(act)
        menu.exec(btn.mapToGlobal(pos))


# --------------------------------------------------------------------------
# procmon — monitored scripts
# --------------------------------------------------------------------------
class ProcMonPanel(BlockPanel):
    """进程监控：仅展示运行态开关列表。脚本的增删改移到
    「布局编辑」里该区块的「编辑项」子对话框，不再内联在主卡片上。"""

    def _setup(self) -> None:
        self.scroll, self.box, _ = _scroll_box()
        self.root.addWidget(self.scroll)
        self.btns: list[QPushButton] = []

        self.empty_hint = _label(
            "（暂无监控项 · 进入「布局编辑」可添加）",
            color="#6b7689", size=11,
        )
        self.empty_hint.setWordWrap(True)
        self.root.addWidget(self.empty_hint)

    def _ensure(self, n: int) -> None:
        while len(self.btns) < n:
            btn = _btn("", self.accent)
            self.btns.append(btn)
            self.box.addWidget(btn)
            self._bind(btn, "action")
        while len(self.btns) > n:
            btn = self.btns.pop()
            self.box.removeWidget(btn)
            btn.deleteLater()

    def sync(self) -> None:
        cfgs = self.instance.configs
        self._ensure(len(cfgs))
        for i, cfg in enumerate(cfgs):
            running = bool(cfg.get("Running"))
            _set(self.btns[i], f"{'●' if running else '○'} {cfg.get('Name', '?')}",
                 self.accent, running)
        self.scroll.setVisible(bool(cfgs))
        self.empty_hint.setVisible(not cfgs)

    def nav_items(self) -> list:
        cfgs = self.instance.configs
        items = []
        for i, btn in enumerate(self.btns):
            if i >= len(cfgs):
                break
            idx = i
            items.append(NavItem(
                btn, "action",
                confirm=lambda idx=idx: (
                    self.instance.toggle_config(idx), self.sync()
                ),
            ))
        return items


# --------------------------------------------------------------------------
# ls — Lossless Scaling presets
# --------------------------------------------------------------------------
class LSPanel(BlockPanel):
    def _setup(self) -> None:
        # 把 BlockPanel 已建的标题标签从 root 取出，改成「标题 + 右对齐按钮」一行：
        # 左侧标题、右侧 [▶ 启动 LS]（卡片右上角）。
        self.root.removeWidget(self._hdr)
        self._hdr.setParent(None)
        hdr_row = QHBoxLayout()
        hdr_row.setContentsMargins(0, 0, 0, 0)
        hdr_row.setSpacing(6)
        hdr_row.addWidget(self._hdr)
        hdr_row.addStretch(1)
        self.launch_btn = _btn("▶ 启动 LS", self.accent)
        hdr_row.addWidget(self.launch_btn)
        self.root.insertLayout(0, hdr_row)

        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet("color: #9aa6b8; font-size: 13px;")
        # 状态行是一长串「LS 预设 · 缩放: · 帧生成: · 性能:」中文，QLabel
        # 默认不换行 → minimumSizeHint 按整行宽度算，会把整张卡片（乃至第一页）
        # 撑到超过窗口宽度，迫使外层 HUD 出现横向滚动条。开启自动换行让长行
        # 折到第二行，宽度不再撑开卡片（与 header 标题 / 各卡片标题的 wordWrap
        # 同理）。
        self.status_lbl.setWordWrap(True)
        self.root.addWidget(self.status_lbl)

        self.scroll, self.box, _ = _scroll_box()
        self.root.addWidget(self.scroll)
        self.btns: list[QPushButton] = []

        self.empty_hint = _label(
            "（暂无 LS 预设 · 进入「布局编辑」可管理）",
            color="#6b7689", size=11,
        )
        self.empty_hint.setWordWrap(True)
        self.root.addWidget(self.empty_hint)

        act = QHBoxLayout()
        self.load_btn = _btn("加载", self.accent)
        self.save_btn = _btn("保存", self.accent)
        self.del_btn = _btn("删除", self.accent)
        act.addWidget(self.load_btn)
        act.addWidget(self.save_btn)
        act.addWidget(self.del_btn)
        self.root.addLayout(act)

        self.selected = 0
        self._bind(self.load_btn, "action")
        self._bind(self.save_btn, "action")
        self._bind(self.del_btn, "action")
        self._bind(self.launch_btn, "action")

    def _ensure(self, n: int) -> None:
        while len(self.btns) < n:
            btn = _btn("", self.accent)
            self.btns.append(btn)
            self.box.addWidget(btn)
            self._bind(btn, "action")
        while len(self.btns) > n:
            btn = self.btns.pop()
            self.box.removeWidget(btn)
            btn.deleteLater()

    def sync(self) -> None:
        b = self.instance
        scale = b._status.get("ScalingType", "关闭")
        fg = b._status.get("FrameGeneration", "关闭")
        perf = b._status.get("PerfMode", "关闭")
        self.status_lbl.setText(
            f"LS 预设 · 缩放: {scale} · 帧生成: {fg} · 性能: {perf}"
        )
        # 启动按钮固定为「▶ 启动 LS」——点击即朴素地打开 exe，不做运行态切换。
        self.launch_btn.setText("▶ 启动 LS")
        self._ensure(len(b.presets))
        if self.selected >= len(b.presets):
            self.selected = 0
        for i, name in enumerate(b.presets):
            _set(self.btns[i], f"F{i + 1}  {name}", self.accent,
                 name == b._profile)
        self.scroll.setVisible(bool(b.presets))
        self.empty_hint.setVisible(not b.presets)

    def _confirm_load(self) -> None:
        if 0 <= self.selected < len(self.instance.presets):
            self.instance._load(self.selected)
        self.sync()

    def _confirm_launch(self) -> None:
        # 点击即启动：launch() 内部「有效路径直接用 / 失效则本次临时探测」，
        # 临时探测绝不写回，故点击永远不会覆盖用户在掌机上手动设的路径。
        # 仅当彻底找不到任何路径时，launch_ls 提示去设置——按钮始终有反应。
        self.instance.launch_ls()
        self.sync()

    def _open_settings(self) -> None:
        opener = getattr(self, "open_ls_settings", None)
        if callable(opener):
            opener()

    def _confirm_save(self) -> None:
        if self.instance.presets:
            self.instance._save()
        self.sync()

    def _confirm_delete(self) -> None:
        if 0 <= self.selected < len(self.instance.presets):
            self.instance._delete(self.selected)
        self.sync()

    def nav_items(self) -> list:
        b = self.instance
        items: list = []
        for i, btn in enumerate(self.btns):
            if i >= len(b.presets):
                break
            idx = i
            items.append(NavItem(
                btn, "select",
                confirm=lambda idx=idx: (
                    setattr(self, "selected", idx),
                    self.instance._load(idx),
                    self.sync(),
                ),
                on_focus=lambda f, idx=idx: (
                    setattr(self, "selected", idx) if f else None
                ),
            ))
        items.append(NavItem(self.load_btn, "action", confirm=self._confirm_load))
        items.append(NavItem(self.save_btn, "action", confirm=self._confirm_save,
                             hint="Y"))
        items.append(NavItem(self.del_btn, "action", confirm=self._confirm_delete,
                             hint="X"))
        items.append(NavItem(self.launch_btn, "action", confirm=self._confirm_launch,
                             hint="L"))
        return items


# --------------------------------------------------------------------------
# tile — launch grid
# --------------------------------------------------------------------------
def layout_tile_grid(grid: QGridLayout, tiles: list[Any], btns: list[Any],
                     max_cols: int) -> None:
    """Place ``tiles`` into ``grid`` using ``btns`` as the widget per tile.

    Shared by the base tile block AND every ``ExtraTileBlock`` proxy panel
    (report §6.2-2: unify the previously duplicated main/extra grid code into a
    single reusable function).  Clears the grid, then for each tile adds the
    matching button at ``(Row, Col)`` spanning ``Height × Width`` (1×1..2×2),
    clamping every coordinate to the grid bounds.
    """
    while grid.count():
        item = grid.takeAt(0)
        w = item.widget()
        if w is not None:
            grid.removeWidget(w)
    if not tiles:
        for c in range(max_cols):
            grid.setColumnStretch(c, 1)
        return
    max_row = 0
    for i, t in enumerate(tiles):
        if i >= len(btns):
            break
        btn = btns[i]
        w = max(1, min(max_cols, int(t.width)))
        h = max(1, min(3, int(t.height)))
        col = max(0, min(int(t.col), max_cols - w))
        row = max(0, int(t.row))
        grid.addWidget(btn, row, col, h, w)
        max_row = max(max_row, row + h)
    for c in range(max_cols):
        grid.setColumnStretch(c, 1)
    for r in range(max_row):
        grid.setRowMinimumHeight(r, 54)


class TilePanel(BlockPanel):
    """磁贴主页面：4 列等宽二维矩阵，磁贴按 Col/Row 定位、按 Width/Height 跨格
    （1×1~2×2），并显示图标/缩略图（自定义图片优先，否则自动取目标系统图标）。
    对应原版 ``Layout-TileGrid`` / ``TileButton.SetTileImage``。"""

    MAX_COLS = 5

    def _setup(self) -> None:
        self.grid = QGridLayout()
        self.grid.setSpacing(6)
        self.grid.setContentsMargins(0, 0, 0, 0)
        for c in range(self.MAX_COLS):
            self.grid.setColumnStretch(c, 1)
        self.btns: list[TileButton] = []
        self.root.addLayout(self.grid)

        self.empty_hint = _label(
            "（暂无磁贴 · 进入「布局编辑」可添加）",
            color="#6b7689", size=11,
        )
        self.empty_hint.setWordWrap(True)
        self.root.addWidget(self.empty_hint)

    def _rebuild(self) -> None:
        b = self.instance
        n = len(b.tiles)
        while len(self.btns) < n:
            btn = TileButton()
            self.btns.append(btn)
            self._bind(btn, "action")
        while len(self.btns) > n:
            btn = self.btns.pop()
            btn.deleteLater()
        self._apply_tile_colors()
        self._sync_block_title()  # update header title/accent per tile metadata

    def _accent_rgb(self) -> tuple[int, int, int]:
        hexc = self.accent.lstrip("#")
        return int(hexc[0:2], 16), int(hexc[2:4], 16), int(hexc[4:6], 16)

    def _apply_tile_colors(self) -> None:
        r, g, b = self._accent_rgb()
        accent = Color(r, g, b)
        tiles = self.instance.tiles if self.instance else []
        for i, btn in enumerate(self.btns):
            # 边框默认跟随呼吸灯主题色；背景默认用磁贴底色。
            # 若磁贴单独设置了 border_color / bg_color，则优先用其覆盖；
            # 边框 'none' = 透明（无边框，边框色取背景色，视觉无描边）。
            back_col = TILE_BG
            border_col = accent
            if i < len(tiles):
                t = tiles[i]
                bgc = getattr(t, "bg_color", "") or ""
                if bgc:
                    try:
                        cr, cg, cb = (int(x) for x in bgc.split(","))
                        back_col = Color(cr, cg, cb)
                    except Exception:
                        pass
                bc = getattr(t, "border_color", "") or ""
                if bc == "none":
                    border_col = back_col
                elif bc:
                    try:
                        cr, cg, cb = (int(x) for x in bc.split(","))
                        border_col = Color(cr, cg, cb)
                    except Exception:
                        pass
            btn.set_colors(
                back_col, TILE_BG_HOVER,
                border_col, TILE_FOCUS_BORDER,
            )

    def _sync_block_title(self) -> None:
        """Apply the (first tile's) block name / show-title / accent to the card
        header and neon accent.

        The accent now follows the single theme colour — the breathing-light
        colour (``get_theme_accent``) — instead of a per-block ``block_theme_color``
        preset, so every card shares one coherent source.
        """
        b = self.instance
        if b.tiles:
            first = b.tiles[0]
            block_name = (first.block_name or "").strip()
            show_title = bool(first.show_block_title)
        else:
            block_name, show_title = "", True
        self._hdr.setVisible(show_title)
        # 统一三角形标志（显示层注入，不可编辑去掉）。
        self._hdr.setText(_card_mark_name(block_name or "磁贴"))
        # 外框跟随呼吸灯（单旋钮）；标题保留各卡自己设定的卡片色。
        self.set_accent(_hex(self._resolve_accent()))
        self._apply_title_color()

    def _layout_grid(self) -> None:
        # Delegate to the shared layout helper (base + every ExtraTileBlock
        # proxy use the exact same code path — report §6.2-2).
        layout_tile_grid(self.grid, self.instance.tiles, self.btns, self.MAX_COLS)
        self._fit_square_cells()

    def resizeEvent(self, ev: Any) -> None:  # noqa: N802 - Qt camelCase
        super().resizeEvent(ev)
        self._fit_square_cells()

    def _fit_square_cells(self) -> None:
        """Keep every tile cell square (1×1 / 2×2) regardless of panel width.

        Columns are equally stretched, so a column's width *is* the cell size.
        We mirror that width onto each row's height (and the TileButton's min
        height) so 1×1 and 2×2 tiles render as squares instead of flat bars.
        """
        grid = getattr(self, "grid", None)
        btns = getattr(self, "btns", None)
        if grid is None or not btns:
            return
        spacing = grid.spacing()
        cols = self.MAX_COLS
        # Grid width ≈ panel width minus the root layout's left/right margins.
        gw = self.width() - 20
        if gw <= 0:
            return
        cell = int((gw - spacing * (cols - 1)) / cols)
        if cell <= 0:
            return
        for r in range(grid.rowCount()):
            grid.setRowMinimumHeight(r, cell)
        for btn in btns:
            idx = grid.indexOf(btn)
            if idx < 0:
                btn.setMinimumHeight(cell)
                btn.setMaximumHeight(cell)
                continue
            _row, _col, rspan, _cspan = grid.getItemPosition(idx)
            h = rspan * cell + (rspan - 1) * spacing
            btn.setMinimumHeight(h)
            btn.setMaximumHeight(h)

    def heavy_refresh(self) -> Callable[[], None] | None:
        # Tiles are a STATIC launcher (no external/volatile data — no CPU/GPU
        # polling, no process list).  They must NOT sit on the periodic refresh
        # rotation.  Rebuilding/recolourising them every 1-2s is pure churn and
        # is the root cause of the intermittent "磁贴全黑刷新" flash.  Tiles only
        # change when the user edits them, which the editor already drives via
        # rebuild() + tile.load_config() — not via the refresh timer.
        return None

    def sync(self) -> None:
        b = self.instance
        self._sync_block_title()
        count_changed = len(self.btns) != len(b.tiles)
        if count_changed:
            self._rebuild()  # _rebuild() also applies tile colours once
        for i, t in enumerate(b.tiles):
            btn = self.btns[i]
            btn.show_tile_name = bool(t.show_tile_name)
            btn.set_tile_name(t.name or "")
            btn.setToolTip(f"{t.launch_type or ''}: {t.launch_path or ''}")
            # Icon — only (re)load when the source actually changes, so we don't
            # re-extract the shell icon (expensive) or reload the pixmap every
            # single second.
            if t.icon_path and os.path.exists(t.icon_path):
                if getattr(btn, "_icon_src", None) != t.icon_path:
                    btn.set_icon(t.icon_path)
                    btn._icon_src = t.icon_path
            elif (t.launch_type or "").upper() == "IMG" and t.launch_path \
                    and os.path.exists(t.launch_path):
                if getattr(btn, "_icon_src", None) != t.launch_path:
                    btn.set_icon(t.launch_path)
                    btn._icon_src = t.launch_path
            elif t.launch_path and not t.launch_path.lower().startswith("http") \
                    and os.path.exists(t.launch_path):
                if getattr(btn, "_icon_src", None) != t.launch_path:
                    btn.set_file_icon(t.launch_path)
                    btn._icon_src = t.launch_path
            else:
                if getattr(btn, "_icon_src", None) is not None:
                    btn.clear_icon()
                    btn._icon_src = None
        self._layout_grid()
        self.empty_hint.setVisible(not b.tiles)
        # Only repaint colours when something that affects them changed
        # (tile count, panel accent/theme, or a per-tile colour override).
        # This ends the per-cycle set_colors()→update() storm that caused the
        # black flash.
        sig = (
            count_changed,
            str(self.accent),
            tuple(
                (getattr(t, "border_color", ""), getattr(t, "bg_color", ""),
                 bool(t.show_tile_name))
                for t in b.tiles
            ),
        )
        if sig != getattr(self, "_tile_color_sig", None):
            self._apply_tile_colors()
            self._tile_color_sig = sig

    def nav_items(self) -> list:
        b = self.instance
        items: list = []
        for i, t in enumerate(b.tiles):
            if i >= len(self.btns):
                break
            idx = i
            items.append(NavItem(
                self.btns[i], "action",
                confirm=lambda idx=idx: (
                    self.instance.launch(self.instance.tiles[idx]), self.sync()
                ),
            ))
        return items


# --------------------------------------------------------------------------
# perfmon — 只读性能监控
# --------------------------------------------------------------------------
class PerfMonPanel(BlockPanel):
    """性能监控卡片（矩阵遥测重设计）：只读实时展示遥测 + 控制状态。

    数据全部来自主窗口 ``perfmon_snapshot()``。帧率来源：HWiNFO「呈现帧率(avg)」
    优先（全局、不依赖前台 PID，规避 HUD/NBJ 置顶时误读 NBJ 自身帧率），RTSS
    全局帧率兜底。卡片分两区：上方 2×4 遥测方块（FPS 高亮），底部控制状态药丸。
    """

    def _win(self):
        prov = getattr(self, "_snapshot_provider", None)
        if prov is not None and hasattr(prov, "perfmon_snapshot"):
            return prov
        w = self.window()
        if w is not None and hasattr(w, "perfmon_snapshot"):
            return w
        return None

    def _on_popout_clicked(self) -> None:
        top = self.window()
        if getattr(top, "_is_perfmon_popup", False):
            top.close()
        elif hasattr(top, "toggle_perfmon_popup"):
            top.toggle_perfmon_popup()

    def _setup(self) -> None:
        # 标题行右侧：弹出按钮（来源标签已移除，节省空间）
        hdr = self._hdr
        self.root.removeWidget(hdr)
        hb = QHBoxLayout()
        hb.setContentsMargins(0, 0, 0, 0)
        hb.setSpacing(6)
        hb.addWidget(hdr)
        hb.addStretch(1)
        self._pop_btn = QPushButton("\u29c9")
        self._pop_btn.setFixedSize(22, 22)
        self._pop_btn.setToolTip("弹出独立监控窗口（再次点击关闭）")
        self._pop_btn.setStyleSheet(
            "QPushButton{color:#9fb0c8;background:rgba(255,255,255,0.06);"
            "border:1px solid rgba(255,255,255,0.18);border-radius:5px;"
            "font-size:14px;}"
            "QPushButton:hover{color:#dfe6f0;border-color:rgba(127,209,255,0.7);}")
        self._pop_btn.clicked.connect(self._on_popout_clicked)
        hb.addWidget(self._pop_btn)
        self.root.insertLayout(0, hb)
        self.root.setSpacing(6)

        # 遥测方块网格（2 列 × 4 行）
        self._tiles = {}
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setVerticalSpacing(6)
        grid.setHorizontalSpacing(6)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        tile_defs = [
            ("fps",   "帧率实测", True),
            ("cpu",   "CPU 负载", False),
            ("gpu",   "GPU 占用", False),
            ("gfreq", "GPU 频率", False),
            ("tcpu",  "温度 CPU", False),
            ("tgpu",  "温度 GPU", False),
            ("tdp",   "功耗 SoC", False),
            ("batt",  "电池",     False),
        ]
        for i, (key, label, hl) in enumerate(tile_defs):
            tile, val, bar = self._mk_tile(label, hl)
            self._tiles[key] = (val, bar)
            r, c = divmod(i, 2)
            grid.addWidget(tile, r, c)
        self.root.addLayout(grid)

        # 底部控制状态药丸
        self._pills = {}
        pill_row = QHBoxLayout()
        pill_row.setContentsMargins(0, 4, 0, 0)
        pill_row.setSpacing(5)
        for key, txt in (("load", "负载"), ("uv", "降压"), ("frame", "锁帧")):
            pill = self._mk_pill(txt)
            self._pills[key] = pill
            pill_row.addWidget(pill)
        pill_row.addStretch(1)
        self.root.addLayout(pill_row)

        self._base_font_px = 14
        pf = self.font()
        pf.setPixelSize(self._base_font_px)
        pf.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
        self.setFont(pf)
        try:
            self._install_toast()
        except Exception:
            pass

    def _mk_tile(self, label, hl):
        tile = QFrame()
        tile.setObjectName("perfTileHL" if hl else "perfTile")
        tile.setStyleSheet(
            "QFrame#perfTileHL{border:1px solid rgba(95,208,255,0.45);"
            "border-radius:9px;background:rgba(95,208,255,0.05);}"
            "QFrame#perfTile{border:1px solid rgba(255,255,255,0.10);"
            "border-radius:9px;background:rgba(255,255,255,0.03);}")
        v = QVBoxLayout(tile)
        v.setContentsMargins(9, 5, 9, 6)
        v.setSpacing(4)
        # 顶部一行：左标签 + 右实测值（实测值落方块右上角，方块更矮更紧凑）
        thead = QHBoxLayout()
        thead.setContentsMargins(0, 0, 0, 0)
        thead.setSpacing(4)
        k = QLabel(label)
        k.setStyleSheet("color:#8b97aa;background:transparent;font-size:11px;")
        thead.addWidget(k)
        thead.addStretch(1)
        val = QLabel("—")
        val.setStyleSheet("color:#dfe6f0;background:transparent;font-size:14px;"
                         "font-family:ui-monospace,Consolas,monospace;")
        thead.addWidget(val)
        v.addLayout(thead)
        bar = QProgressBar()
        bar.setTextVisible(False)
        bar.setRange(0, 100)
        bar.setFixedHeight(4)
        bar.setValue(0)
        bar.setStyleSheet(
            "QProgressBar{background:rgba(255,255,255,0.08);border:none;"
            "border-radius:3px;}"
            "QProgressBar::chunk{background:%s;border-radius:3px;}" % self.accent)
        v.addWidget(bar)
        return tile, val, bar

    def _mk_pill(self, text):
        pill = QLabel(text)
        pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pill.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        pill.setStyleSheet(
            "QLabel{color:#9aa6b8;background-color:rgba(255,255,255,0.05);"
            "border:1px solid rgba(255,255,255,0.14);border-radius:12px;"
            "padding:3px 10px;font-size:11px;min-height:20px;}")
        return pill

    def _set_tile(self, key, text, pct=None, color=None):
        t = self._tiles.get(key)
        if t is None:
            return
        val, bar = t
        if val.text() != text:
            val.setText(text)
        if color and val.styleSheet().find(color) < 0:
            fs = 15 if key == "fps" else 14
            val.setStyleSheet(
                f"color:{color};background:transparent;font-size:{fs}px;"
                f"font-family:ui-monospace,Consolas,monospace;")
        if pct is not None:
            bar.setValue(max(0, min(100, int(pct))))

    def _set_pill(self, key, text, color):
        p = self._pills.get(key)
        if p is None:
            return
        if p.text() != text:
            p.setText(text)
        if p.styleSheet().find(color) < 0:
            p.setStyleSheet(
                f"QLabel{{color:{color};background-color:rgba(255,255,255,0.05);"
                f"border:1px solid rgba(255,255,255,0.14);border-radius:12px;"
                f"padding:3px 10px;font-size:11px;min-height:20px;}}")
            p.setAlignment(Qt.AlignmentFlag.AlignCenter)
            p.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    @staticmethod
    def _fps_pct(fa, tf):
        cap = tf if (tf and tf > 0) else 240
        return min(100, fa / cap * 100)

    def _install_toast(self) -> None:
        self.toast_label = _Label()
        self.toast_panel = _Toast(self.toast_label, self)

    def _install_toast(self) -> None:
        self.toast_label = _Label()
        self.toast_panel = _Toast(self.toast_label, self)

    def sync(self) -> None:
        win = self._win()
        if win is None:
            return
        top = self.window()
        if top is not None and not getattr(top, "_is_perfmon_popup", False):
            owner = getattr(self, "_snapshot_provider", None) or top
            if getattr(owner, "_perfmon_popup", None) is not None:
                return
        try:
            s = win.perfmon_snapshot()
        except Exception:
            return

        # 帧率实测
        fa = s.get("fps_actual")
        tf = s.get("fps_target")
        if fa is not None:
            txt = f"{fa:.0f} FPS"
            if tf and fa < tf * 0.8:
                self._set_tile("fps", txt, self._fps_pct(fa, tf), "#ff8a5c")
            else:
                self._set_tile("fps", txt, self._fps_pct(fa, tf), "#dfe6f0")
        else:
            self._set_tile("fps", "—", 0, "#5a6b7c")

        # CPU 负载 avg/peak
        ca, cm = s.get("cpu_avg"), s.get("cpu_max")
        if ca is not None or cm is not None:
            a = f"{ca:.0f}" if ca is not None else "—"
            m = f"{cm:.0f}" if cm is not None else "—"
            self._set_tile("cpu", f"{a}/{m}%", cm if cm is not None else 0, "#dfe6f0")
        else:
            self._set_tile("cpu", "—", 0, "#5a6b7c")

        # GPU 占用
        g = s.get("gpu")
        if g is not None:
            self._set_tile("gpu", f"{g:.0f}%", g, "#dfe6f0")
        else:
            self._set_tile("gpu", "—", 0, "#5a6b7c")

        # GPU 频率 实际/锁定
        ga = s.get("gpu_freq_actual")
        tg = s.get("gpu_locked_freq")
        if ga is not None:
            txt = f"{ga:.0f}" + (f" /{tg:.0f}" if tg else "")
            self._set_tile("gfreq", txt, min(100, ga / 33.0), "#dfe6f0")
        else:
            self._set_tile("gfreq", "—", 0, "#5a6b7c")

        # 温度 CPU / GPU
        tc = s.get("temp_cpu")
        if tc is not None:
            self._set_tile("tcpu", f"{tc:.0f}°", min(100, tc),
                           "#ff8a5c" if tc >= 85 else "#dfe6f0")
        else:
            self._set_tile("tcpu", "—", 0, "#5a6b7c")
        tcg = s.get("temp_gpu")
        if tcg is not None:
            self._set_tile("tgpu", f"{tcg:.0f}°", min(100, tcg),
                           "#ff8a5c" if tcg >= 85 else "#dfe6f0")
        else:
            self._set_tile("tgpu", "—", 0, "#5a6b7c")

        # 功耗 SoC
        ta = s.get("tdp_actual")
        if ta is not None:
            self._set_tile("tdp", f"{ta:.1f}W", min(100, ta / 65.0 * 100), "#dfe6f0")
        else:
            self._set_tile("tdp", "—", 0, "#5a6b7c")

        # 电池充放电
        br = s.get("batt_charge_rate")
        if br is not None:
            self._set_tile("batt", f"{'放电' if br < 0 else '充电'} {abs(br):.1f}W",
                           min(100, abs(br)), "#dfe6f0")
        else:
            self._set_tile("batt", "电池 —", 0, "#5a6b7c")

        # 底部状态药丸
        load = s.get("load", "idle")
        load_label = s.get("load_label", "空闲")
        lc = {"idle": "#9aa6b8", "light": "#ffd166",
              "heavy": "#ff8a5c", "pinned": "#7fd1ff"}.get(load, "#dfe6f0")
        self._set_pill("load", f"● {load_label}", lc)
        mv = int(s.get("undervolt_mv", 0))
        if s.get("undervolt_enabled"):
            self._set_pill("uv", f"降压 CO=-{mv}", "#4be38a")
        else:
            self._set_pill("uv", "未降压", "#9aa6b8")
        if s.get("frame_unlocked"):
            self._set_pill("frame", "未锁帧", "#9aa6b8")
        else:
            self._set_pill("frame", s.get("frame_state", "已锁帧"), "#4be38a")

        # 帧率偏差告警 toast（上升沿一次）
        for key, msg in (("fps_dev_alert", "锁帧未生效"),):
            armed = bool(s.get(key))
            if armed and not getattr(self, f"_last_{key}", False):
                try:
                    self.toast_panel.visible = True
                    self.toast_label.text = f"⚠ {msg}：目标 vs 实测偏差过大"
                    self.toast_panel.set_tone(False)
                except Exception:
                    pass
            setattr(self, f"_last_{key}", armed)

    def nav_items(self) -> list:
        return []


class PerfMonPopup(QWidget):
    """独立浮动性能监控窗口。

    内容与性能监控卡片完全一致（复用 :class:`PerfMonPanel`），作为【无边框】的
    顶层窗口（无系统标题栏 / 最小化 / 关闭按钮）并置顶显示。即使主程序最小化，
    本窗口仍保持可见与实时刷新（自带 1s 定时器，数据源直接取自主窗口
    ``perfmon_snapshot``，不依赖主窗口的显示刷新定时器）。

    行为：
      - 全局唯一：主窗口持有 ``_perfmon_popup`` 引用，重复点击按钮只关闭已有实例。
      - 位置：打开时定位到性能监控卡片在屏幕上的当前位置（像卡片分裂出来）。
      - 尺寸：打开时大小 / 比例与性能监控卡片完全一致；内容即卡片本身，不缩放、
        不拉伸、无纵向空白带。
      - 拖动：按住窗口任意位置（含角落按钮区域）即可拖动窗口。
      - 关闭：焦点在本窗口时按 ``Delete`` 直接关闭；主程序退出时由 aboutToQuit 关闭。
    """

    def __init__(self, source, parent=None, initial_size=None):
        super().__init__(
            parent,
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        # 辅助浮窗不应触发 app 退出：本弹窗是无 parent 的顶层 Qt.Window，
        # Qt 默认给它 WA_QuitOnClose=True；当主窗口已 hide 到托盘时，关闭本弹窗
        # 会被 Qt 当成"最后一个可见窗口关闭" → 触发 lastWindowClosed → app.quit()，
        # 导致"关掉性能监控浮窗 = 整个程序退出"。显式关闭此属性即可根治。
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self._is_perfmon_popup = True
        self._owner = source
        self.setWindowTitle("性能监控")
        # 不透明窗口（保证下方深色背景实心铺满，避免 frameless 方角露出系统色）；
        # 刻意【不使用 setMask 圆角裁剪】——frameless + 复杂子控件树在 Windows
        # DWM 真实渲染时会原生段错误（0xC0000005 / 离屏 exit 127）。弹窗改用
        # 「深色窗口底 + 卡片自绘圆角」：窗口与卡片同为深色，方角在深色底上不可见，
        # 视觉等同圆角卡片，但彻底避开崩溃。
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setObjectName("PerfMonPopup")
        self.setStyleSheet("#PerfMonPopup { background: #0e1117; }")

        # 直接把 PerfMonPanel 当子控件嵌入，不经过 QGraphicsView 缩放。这样弹窗就是
        # 卡片的精确复刻：同一套布局、同一行距、同一比例，无任何缩放失真。
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        panel = PerfMonPanel(None, "性能监控", get_theme_accent())
        panel._snapshot_provider = source
        self._panel = panel
        lay.addWidget(panel)

        self._timer = QTimer(self)
        self._timer.timeout.connect(panel.sync)
        self._timer.start(1000)
        panel.sync()

        # 尺寸策略：优先用原卡片的实际尺寸，让弹窗一出现就和卡片完全一样；
        # 如果拿不到，再退回到面板的自然尺寸。
        panel.adjustSize()
        nat_w, nat_h = panel.width(), panel.height()
        if nat_w <= 0 or nat_h <= 0:
            sh = panel.sizeHint()
            nat_w, nat_h = sh.width(), sh.height()
        if initial_size is not None and initial_size.width() > 0 \
                and initial_size.height() > 0:
            w, h = int(initial_size.width()), int(initial_size.height())
        else:
            w, h = nat_w, nat_h
        # 固定为卡片尺寸：保证「大小和比例与性能监控功能卡片完全一致」
        self.setFixedSize(w, h)

        self._drag_start_global = None
        self._drag_start_pos = None
        self._dragging = False
        self._dwm_rounded_done = False
        self.installEventFilter(self)

    def keyPressEvent(self, ev):
        # 焦点在本窗口时按 Delete 直接关闭
        if ev.key() == Qt.Key.Key_Delete:
            self.close()
            return
        super().keyPressEvent(ev)

    def showEvent(self, ev):
        super().showEvent(ev)
        # 显示后再调用 DWM 圆角：FramelessWindowHint + 不透明窗口在 Windows 11
        # 下也能有和页面卡片一致的圆角描边效果，避免 setMask 导致的原生崩溃。
        self._apply_dwm_rounded_corners()

    def _apply_dwm_rounded_corners(self) -> None:
        """通过 Windows DWM 把弹窗四角设为圆角（Windows 11 生效）。"""
        if self._dwm_rounded_done or sys.platform != "win32":
            return
        try:
            import ctypes
            hwnd = int(self.winId())
            DWMWA_WINDOW_CORNER_PREFERENCE = 33
            DWMWCP_ROUND = 2
            value = ctypes.c_uint(DWMWCP_ROUND)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd,
                DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
            self._dwm_rounded_done = True
        except Exception:
            # 旧版 Windows 或 DWM 不可用时静默回退到直角
            pass

    def eventFilter(self, obj, ev):
        # 按住窗口任意位置拖动。鼠标事件先经过子控件（标题、标签、按钮），
        # 过滤器在弹窗层级统一拦截，实现「整个卡片都能拖」。
        t = ev.type()
        if t == QEvent.Type.MouseButtonPress and \
                ev.button() == Qt.MouseButton.LeftButton:
            self._drag_start_global = ev.globalPosition().toPoint()
            self._drag_start_pos = self.pos()
            self._dragging = False
            # 仍返回 False，让按钮获得按下视觉；一旦移动距离超过阈值就进入拖动，
            # 并在 MouseButtonRelease 里吃掉释放事件，避免误触发关闭。
            return False
        if t == QEvent.Type.MouseMove and self._drag_start_global is not None:
            if ev.buttons() & Qt.MouseButton.LeftButton:
                d = ev.globalPosition().toPoint() - self._drag_start_global
                if not self._dragging and d.manhattanLength() > 4:
                    self._dragging = True
                    btn = getattr(self._panel, "_pop_btn", None)
                    if btn is not None:
                        btn.setDown(False)
                if self._dragging:
                    self.move(self._drag_start_pos + d)
            return False
        if t == QEvent.Type.MouseButtonRelease:
            was_dragging = self._dragging
            self._drag_start_global = None
            self._dragging = False
            if was_dragging:
                return True  # 吞掉释放事件，避免落在角落按钮上误触发关闭
        return False

    def closeEvent(self, ev):
        if self._owner is not None:
            self._owner._perfmon_popup = None
        self._timer.stop()
        super().closeEvent(ev)


# --------------------------------------------------------------------------
# smt — 热核隔离（CPU 集合：游戏兄弟隔离 / 其余进程整核让出）
# --------------------------------------------------------------------------
# (CoreBarChart / CoreSampler 已迁移至 .smt_chart)


class SmtPanel(BlockPanel):
    """SMT 热核隔离卡片：四按钮 + 状态行 + 可切换的实时柱状图。

      - SMT自动 ：开启/关闭持续热核检测 + 自动施加 CPU 集合（手柄 Y）
      - SMT锁定 ：复原全核 -> 3s 采样 -> 施加一次（不持续，无快捷标签）
      - SMT复原 ：所有进程复原为全核（手柄 X）
      - 热核视图：点击在卡片中显示/隐藏实时柱状图，并启停采样线程
                  （默认隐藏且完全不采样，避免切页时线程未停导致原生崩溃）
      - 采样线程生命周期由 SmtChartWidget 显式管理；面板销毁前 release()
        会先同步停止线程，杜绝 'QThread: Destroyed while thread still running'。
    """

    def _setup(self) -> None:
        blk = self.instance
        self._view_on = False

        # ---- 第一行（顶部）：热核视图按钮独占一行（热和隔离）----
        view_row = QHBoxLayout()
        view_row.setSpacing(6)
        self.view_btn = _btn("热核视图：关", self.accent)
        view_row.addWidget(self.view_btn)

        # ---- 第二行：SMT自动 / SMT锁定 / SMT复原 ----
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self.auto_btn = _btn("SMT自动：关", self.accent)
        self.lock_btn = _btn("SMT锁定", self.accent)
        self.restore_btn = _btn("SMT复原", self.accent)
        btn_row.addWidget(self.auto_btn)
        btn_row.addWidget(self.lock_btn)
        btn_row.addWidget(self.restore_btn)

        # ---- 柱状图（默认隐藏 + 不采样；点击「热核视图」才显示并采样）----
        self.chart = SmtChartWidget(blk)
        self.chart.setVisible(False)

        # 正文顺序：柱状图（上方，仅显示时占空间）-> 热核视图行 -> 三按钮行 ->
        # 状态角标（右上角，取代原底部状态行）。
        self.root.addWidget(self.chart, 1)
        self.root.addLayout(view_row)
        self.root.addLayout(btn_row)
        self.status_lbl = self._corner_lbl

        # 统一经 _bind 接入导航（鼠标/键盘/手柄汇聚到 activate_widget -> confirm），
        # 不再额外绑 clicked，避免重复触发。
        self._bind(self.auto_btn, "toggle")
        self._bind(self.lock_btn, "action")
        self._bind(self.restore_btn, "action")
        self._bind(self.view_btn, "toggle")

        # 把区块状态变更回调接到本卡片刷新（用 QTimer 投到 GUI 线程，
        # 因为区块在后台线程（锁定/复原/自动 tick）里通知，直接刷新 widget 不安全）
        if blk is not None and hasattr(blk, "set_status_callback"):
            blk.set_status_callback(lambda: QTimer.singleShot(0, self.sync))

        # 应用退出时优雅停掉采样：确保 SmtChartWidget 终止内部 PowerShell 监视器，
        # 避免每次退出都泄漏孤儿进程。
        app = QApplication.instance()
        if app is not None:
            try:
                app.aboutToQuit.connect(self._stop_chart)
            except Exception:
                pass

        # 窗口最小化时暂停采样（不显示、不空转）；仅当视图已打开时才生效。
        self._win_state_conn = False
        QTimer.singleShot(0, self._connect_window_state)

    # -- 窗口状态 --------------------------------------------------------
    def _connect_window_state(self) -> None:
        # 延迟到事件循环启动后再连接顶层窗口，避免 _setup 时 window() 为 None。
        win = self.window()
        if win is None:
            return
        try:
            win.windowStateChanged.connect(self._on_win_state)
            self._win_state_conn = True
        except Exception:
            pass

    def _on_win_state(self, state: Any) -> None:
        minimized = bool(state & Qt.WindowState.WindowMinimized)
        # 仅当热核视图已打开时才暂停/恢复采样；视图关闭时图表不采样。
        if self._view_on:
            self.chart.setPaused(minimized)

    def _stop_chart(self) -> None:
        try:
            self.chart.stop()
        except Exception:
            pass

    # -- 热核视图（点击切换显隐 + 启停采样）---------------------------
    def _on_toggle_view(self) -> None:
        self._view_on = not self._view_on
        self.chart.setVisible(self._view_on)
        if self._view_on:
            self.chart.start()
        else:
            self.chart.stop()
        self.sync()

    def release(self) -> None:
        """面板被销毁（切页 / 关闭窗口）前的显式释放。

        必须先把仍在运行的采样线程同步停等，否则 _clear_inner_layout 的
        deleteLater 会连带销毁运行中的 QThread，触发原生崩溃
        'QThread: Destroyed while thread still running'。
        """
        try:
            self.chart.stop()
        except Exception:
            pass
        # 断开窗口状态监听，避免面板反复重建时累积无效连接。
        win = self.window()
        if win is not None and getattr(self, "_win_state_conn", False):
            try:
                win.windowStateChanged.disconnect(self._on_win_state)
            except Exception:
                pass
            self._win_state_conn = False

    # -- 业务按钮（经 nav_items confirm 触发）---------------------------
    def _persist(self) -> None:
        p = getattr(self, "persist", None)
        if callable(p):
            try:
                p()
            except Exception:
                pass

    def _on_auto(self) -> None:
        blk = self.instance
        if blk is None:
            return
        running = bool(blk.status().get("auto_running", False))
        blk.set_auto(not running)
        self._persist()
        self.sync()

    def _on_lock(self) -> None:
        blk = self.instance
        if blk is None:
            return
        blk.lock_once()
        self._set_status("SMT锁定：采样 3s 中…", "#ffd166")

    def _on_restore(self) -> None:
        blk = self.instance
        if blk is None:
            return
        blk.restore_all()
        self._set_status("SMT复原：全核绑定中…", "#4be38a")

    def _set_status(self, text: str, color: str) -> None:
        self._set_corner(text, color)

    def nav_items(self) -> list:
        return [
            NavItem(self.view_btn, "toggle", confirm=self._on_toggle_view),
            NavItem(self.auto_btn, "toggle", confirm=self._on_auto, hint="Y"),
            NavItem(self.lock_btn, "action", confirm=self._on_lock, hint="A"),
            NavItem(self.restore_btn, "action", confirm=self._on_restore, hint="X"),
        ]

    def sync(self) -> None:
        blk = self.instance
        if blk is None:
            return
        st = blk.status()
        running = bool(st.get("auto_running", False))
        _set(self.auto_btn,
             "SMT自动：开" if running else "SMT自动：关",
             self.accent, running)
        _set(self.view_btn,
             "热核视图：开" if self._view_on else "热核视图：关",
             self.accent, self._view_on)
        mode = st.get("mode")
        if mode == "auto":
            hot = st.get("hot_lp")
            if hot is not None:
                self._set_status(
                    f"自动监测中 · 最热核 LP{hot} {st.get('hot_val', 0):.0f}%"
                    f" · {'已隔离' if st.get('is_bottleneck') else '均衡'}",
                    "#9aa6b8")
            else:
                self._set_status("自动监测中…", "#9aa6b8")
        elif mode == "lock":
            err = st.get("error")
            if err:
                self._set_status(f"锁定失败：{err}", "#e64b4b")
            else:
                tag = "瓶颈锁定" if st.get("is_bottleneck") else "强制锁定"
                self._set_status(
                    f"锁定完成 · LP{st.get('hot_lp')} {st.get('hot_val', 0):.0f}%"
                    f" · 已隔离（{tag}）· 施加 {st.get('applied', 0)} 进程",
                    "#9aa6b8")
        elif mode == "restore":
            self._set_status(f"已复原 {st.get('restored', 0)} 个进程为全核", "#4be38a")
        else:
            self._set_status("待命（SMT自动 / 锁定 / 复原）", "#9aa6b8")


# --------------------------------------------------------------------------
# tasklist — 任务列表（主页直接增删 / agent 自动填写）
# --------------------------------------------------------------------------
class TaskListPanel(BlockPanel):
    """任务列表卡片：主页内联输入框 + 添加按钮 + 可增删的任务列表。

    不依赖编辑项页面——添加/删除直接在主卡完成（模仿 FreezePanel 的主页交互）。
    agent 完成任务后通过 ``blocks.tasklist.add_task(source="agent")`` 追加，
    这里实时反映。
    """

    def _setup(self) -> None:

        # 输入行：文本框 + 添加按钮（主页直接添加，无需进编辑项）。
        add_row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("输入任务后回车 / 点「添加」…")
        self.input.setStyleSheet(
            "QLineEdit{border:1px solid #3a4150; border-radius:6px;"
            "padding:4px 8px; color:#e6ebf2; background:rgba(0,0,0,0.25);"
            "font-size:12px;}}"
            "QLineEdit:focus{border:1px solid %s;}" % self.accent
        )
        self.add_btn = _btn("添加", self.accent)
        add_row.addWidget(self.input, 1)
        add_row.addWidget(self.add_btn)
        self.root.addLayout(add_row)

        self.input.returnPressed.connect(self._on_add)
        # 注意：不要再用 _bind(self.input, "input") —— 那会把 editingFinished
        # 也接到 activate_widget→confirm(_on_add)，与上面的 returnPressed 在回车
        # 时造成「一次回车添加两条」的重复。回车添加仅由 returnPressed 负责。
        # 输入行仅作为可聚焦项存在于 nav_items（confirm=None），手柄/键盘聚焦
        # 它用于打字，不自动添加；要添加就回车或点「添加」按钮。
        self._bind(self.add_btn, "action")

        # 任务列表（动态行，_ensure 按需创建/销毁）。
        self.scroll, self.list_box, _ = _scroll_box()
        self.root.addWidget(self.scroll)
        self.rows: list[QWidget] = []  # 每行容器 widget

        # 底部操作：清空已完成。
        foot = QHBoxLayout()
        self.clear_btn = _btn("清空已完成", self.accent)
        self.count_lbl = _label("（0 项）", color="#6b7689", size=11)
        foot.addWidget(self.clear_btn)
        foot.addWidget(self.count_lbl)
        foot.addStretch(1)
        self.root.addLayout(foot)
        self._bind(self.clear_btn, "action")

        self.empty_hint = _label(
            "（暂无任务 · 在上方输入即可添加；agent 完成任务也会自动填入）",
            color="#6b7689", size=11,
        )
        self.empty_hint.setWordWrap(True)
        self.root.addWidget(self.empty_hint)

    # -- 动态行管理（参考 ProcMonPanel._ensure） -----------------------
    def _ensure(self, n: int) -> None:
        while len(self.rows) < n:
            row = QWidget()
            rlay = QHBoxLayout(row)
            rlay.setContentsMargins(0, 0, 0, 0)
            rlay.setSpacing(6)
            chk = _btn("▢", self.accent)
            chk.setFixedWidth(34)
            txt = _btn("", self.accent)
            txt._left_align = True
            del_btn = _btn("✕", self.accent)
            del_btn.setFixedWidth(34)
            rlay.addWidget(chk)
            rlay.addWidget(txt, 1)
            rlay.addWidget(del_btn)
            # 把三个控件显式挂到 row，避免 sync 时用 findChildren 顺序不确定。
            row.chk = chk
            row.txt = txt
            row.del_btn = del_btn
            self.rows.append(row)
            self.list_box.addWidget(row)
            # 行内三个控件都作为可导航项。
            self._bind(chk, "action")
            self._bind(txt, "action")
            self._bind(del_btn, "action")
        while len(self.rows) > n:
            row = self.rows.pop()
            self.list_box.removeWidget(row)
            row.deleteLater()

    def _on_add(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        try:
            self.instance.add_task(text, source="manual")
        except Exception:
            return
        self.input.clear()
        self.instance.save_config()
        self.persist()
        self.sync()

    def _remove_at(self, idx: int) -> None:
        items = self.instance.items
        if 0 <= idx < len(items):
            self.instance.remove_task(items[idx].id)
            self.instance.save_config()
            self.persist()
            self.sync()

    def _toggle_at(self, idx: int) -> None:
        items = self.instance.items
        if 0 <= idx < len(items):
            self.instance.toggle_done(items[idx].id)
            self.instance.save_config()
            self.persist()
            self.sync()

    def sync(self) -> None:
        b = self.instance
        items = list(b.items)
        self._ensure(min(len(items), 200))  # 渲染上限保护
        shown = items[:200]
        for i, row in enumerate(self.rows):
            if i >= len(shown):
                row.setVisible(False)
                continue
            it = shown[i]
            row.setVisible(True)
            chk, txt = row.chk, row.txt
            mark = "▣" if it.done else "▢"
            tag = " 🤖" if (it.source == "agent" and b.show_agent_tag) else ""
            label = f"{it.text}{tag}"
            # 完成态用 active=True 弱化配色（遵循 _set 约定，避免破坏按钮自绘）。
            _set(chk, mark, self.accent, it.done)
            _set(txt, label, self.accent, it.done)

        pending = len(b.pending)
        total = len(items)
        self.count_lbl.setText(f"（待办 {pending} / 共 {total} 项）")
        self.scroll.setVisible(bool(items))
        self.empty_hint.setVisible(not items)

    def nav_items(self) -> list:
        b = self.instance
        items = list(b.items)[:200]
        nav = [
            NavItem(self.input, "input", confirm=None),
            NavItem(self.add_btn, "action",
                    confirm=lambda: self._on_add()),
        ]
        for i, row in enumerate(self.rows):
            if i >= len(items):
                break
            chk, txt, del_btn = row.chk, row.txt, row.del_btn
            idx = i
            nav.append(NavItem(
                chk, "action",
                confirm=lambda idx=idx: self._toggle_at(idx),
                hint="A",
            ))
            nav.append(NavItem(
                txt, "action",
                confirm=lambda idx=idx: self._toggle_at(idx),
            ))
            nav.append(NavItem(
                del_btn, "action",
                confirm=lambda idx=idx: self._remove_at(idx),
                hint="X",
                action_x=lambda idx=idx: self._remove_at(idx),
            ))
        nav.append(NavItem(self.clear_btn, "action",
                           confirm=lambda: (self.instance.clear_done(),
                                            self.instance.save_config(),
                                            self.persist(), self.sync())))
        return nav


# --------------------------------------------------------------------------
# changelog — 更新内容（上一次打包 -> 本次打包之间的差异）
# --------------------------------------------------------------------------
class ChangelogPanel(BlockPanel):
    """更新内容卡片：只读展示随包 changelog.json 的条目。

    新版本（version 与已读版本不符）时高亮「NEW」徽标；查看后调用 mark_seen
    把 last_seen_version 记为当前版本。无编辑项——纯展示 + 标记已读。
    """

    def _setup(self) -> None:
        head = QHBoxLayout()
        self.ver_lbl = QLabel("")
        self.ver_lbl.setStyleSheet("color:#cdd6e4; font-size:12px; font-weight:bold;")
        self.new_badge = QLabel("NEW")
        self.new_badge.setStyleSheet(
            "QLabel{color:#0e0f13; background:%s; border-radius:4px;"
            "padding:1px 6px; font-size:10px; font-weight:bold;}" % self.accent
        )
        self.new_badge.setVisible(False)
        head.addWidget(self.ver_lbl)
        head.addWidget(self.new_badge)
        head.addStretch(1)
        self.root.addLayout(head)

        self.time_lbl = QLabel("")
        self.time_lbl.setStyleSheet("color:#6b7689; font-size:12px;")
        self.root.addWidget(self.time_lbl)

        self.scroll, self.list_box, _ = _scroll_box()
        self.scroll.setMaximumHeight(150)
        self.list_box.setSpacing(8)  # 更新项之间留白，避免拥挤
        self.root.addWidget(self.scroll)
        self.entry_rows: list[tuple[QWidget, QLabel, QLabel]] = []

        self.empty_hint = _label(
            "（暂无更新说明 · 打包时由 make_installer 生成 changelog.json）",
            color="#6b7689", size=11,
        )
        self.empty_hint.setWordWrap(True)
        self.root.addWidget(self.empty_hint)

        foot = QHBoxLayout()
        self.seen_btn = _btn("标记为已读", self.accent)
        foot.addWidget(self.seen_btn)
        foot.addStretch(1)
        self.root.addLayout(foot)
        self._bind(self.seen_btn, "action")

    def _ensure(self, n: int) -> None:
        while len(self.entry_rows) < n:
            # 每条更新项：左列固定宽圆点 + 右列可换行文字，
            # 文字换行后整体缩进到圆点右侧，避免第二行顶格错乱。
            row = QWidget()
            hb = QHBoxLayout(row)
            hb.setContentsMargins(0, 0, 0, 0)
            hb.setSpacing(6)
            dot = QLabel("•")
            dot.setStyleSheet(f"color:{self.accent}; font-size:12px;")
            dot.setFixedWidth(12)
            dot.setAlignment(Qt.AlignmentFlag.AlignTop)
            txt = _label("", color="#cdd6e4", size=12)
            txt.setWordWrap(True)
            hb.addWidget(dot)
            hb.addWidget(txt, 1)
            self.entry_rows.append((row, dot, txt))
            self.list_box.addWidget(row)
        while len(self.entry_rows) > n:
            row, _, _ = self.entry_rows.pop()
            self.list_box.removeWidget(row)
            row.deleteLater()

    def _on_seen(self) -> None:
        self.instance.mark_seen()
        self.persist()
        self.sync()

    def sync(self) -> None:
        b = self.instance
        ver = b.version or "（未知版本）"
        scope = ""
        if b.prev_version:
            scope = f" （{b.prev_version} → {b.version}）"
        self.ver_lbl.setText(f"v{ver}{scope}" if b.version else ver)
        self.time_lbl.setText(f"打包时间：{b.build_time}" if b.build_time else "")
        self.new_badge.setVisible(bool(b.is_new))
        self._ensure(len(b.entries))
        for i, (row, dot, txt) in enumerate(self.entry_rows):
            if i < len(b.entries):
                row.setVisible(True)
                txt.setText(b.entries[i])
            else:
                row.setVisible(False)
        self.scroll.setVisible(bool(b.entries))
        self.empty_hint.setVisible(not b.entries)
        self.seen_btn.setVisible(bool(b.is_new))

    def nav_items(self) -> list:
        return [
            NavItem(self.seen_btn, "action", confirm=lambda: self._on_seen()),
        ]


# --------------------------------------------------------------------------
# 配置预设（PresetBlock 的卡片）
# --------------------------------------------------------------------------
class PresetPanel(BlockPanel):
    """配置预设卡片：预设列表 + 「新建预设」按钮。

    每行 = 预设名（点击套用）+ ✕（删除）。底部按钮把当前各卡片组合存成新预设。
    列表行用按需增删的固定池，nav_items 每帧重建以反映最新列表与索引，焦点按控件
    身份保留（interaction 层每帧用 widget identity 维持选中，不会因列表重建掉焦）。
    """

    def _setup(self) -> None:
        hint = _label("在各卡片调好设置后点「新建预设」自动保存；点列表项即套用。",
                      color="#9aa6b8", size=11)
        hint.setWordWrap(True)
        self.root.addWidget(hint)

        self.scroll, self.list_box, _ = _scroll_box()
        self.scroll.setMaximumHeight(170)
        self.root.addWidget(self.scroll)

        # 每行固定池：(row 容器, 套用按钮, 删除按钮)。按预设数量增删。
        self.preset_rows: list[tuple[QWidget, QPushButton, QPushButton]] = []

        self.empty_hint = _label("（暂无预设，点「新建预设」保存当前配置）",
                                 color="#6b7689", size=11)
        self.empty_hint.setWordWrap(True)
        self.root.addWidget(self.empty_hint)

        foot = QHBoxLayout()
        self.new_btn = _btn("新建预设", self.accent)
        foot.addWidget(self.new_btn)
        foot.addStretch(1)
        self.root.addLayout(foot)
        self._bind(self.new_btn, "action")

    def _ensure(self, n: int) -> None:
        while len(self.preset_rows) < n:
            row = QWidget()
            hb = QHBoxLayout(row)
            hb.setContentsMargins(0, 0, 0, 0)
            hb.setSpacing(6)
            apply_btn = _btn("", self.accent)
            apply_btn.setStyleSheet(
                "QPushButton{text-align:left; padding-left:8px;}")
            del_btn = _btn("✕", self.accent)
            del_btn.setFixedWidth(32)
            hb.addWidget(apply_btn, 1)
            hb.addWidget(del_btn, 0)
            self.list_box.addWidget(row)
            self.preset_rows.append((row, apply_btn, del_btn))
        while len(self.preset_rows) > n:
            row, apply_btn, del_btn = self.preset_rows.pop()
            self.list_box.removeWidget(row)
            row.deleteLater()

    def sync(self) -> None:
        b = self.instance
        presets = b.config.presets_config.presets
        n = len(presets)
        self._ensure(n)
        for i, spec in enumerate(presets):
            row, apply_btn, del_btn = self.preset_rows[i]
            row.setVisible(True)
            apply_btn.setText(spec.name)
        for i in range(n, len(self.preset_rows)):
            self.preset_rows[i][0].setVisible(False)
        self.scroll.setVisible(n > 0)
        self.empty_hint.setVisible(n == 0)

    def nav_items(self) -> list:
        items = []
        presets = self.instance.config.presets_config.presets
        for i in range(len(presets)):
            if i < len(self.preset_rows):
                row, apply_btn, del_btn = self.preset_rows[i]
                items.append(NavItem(apply_btn, "action",
                                     confirm=lambda i=i: self._apply(i)))
                items.append(NavItem(del_btn, "action",
                                     confirm=lambda i=i: self._remove(i)))
        items.append(NavItem(self.new_btn, "action", confirm=lambda: self._new()))
        return items

    def _new(self) -> None:
        self.instance.add_preset_from_current()
        self.sync()

    def _apply(self, i: int) -> None:
        self.instance.apply_preset(i)
        self.sync()

    def _remove(self, i: int) -> None:
        self.instance.remove_preset(i)
        self.sync()


# --------------------------------------------------------------------------
# 电源计划（PowerPlanBlock 的卡片）
# --------------------------------------------------------------------------
# 电源计划卡片「最大频率」滑块量程(MHz)。处理器最大频率单位为 MHz，0 在系统里=最高/不限制，
# 这里直接用具体 MHz 映射（拖到顶端即接近不限制）。
_FREQ_MIN = 1000
_FREQ_MAX = 5500
_FREQ_STEP = 100  # 最大频率调整步长：100MHz
# 「最大处理器状态」(PROCTHROTTLEMAX) 滑块：百分比 0-100，100=不限。
_STATE_MIN = 0
_STATE_MAX = 100
_STATE_STEP = 5  # 处理器最大状态调整步长：5%
# 「睿频」按钮按提升模式取值着色的状态色（填充块）。色阶随激进程度递进：
# 已禁用=红（睿频关）→ 高效=绿（推荐）→ 高效激进=黄绿 → 激进=橙（最费电/最热）。
# 档位中文名/循环顺序的单一真相源在 blocks.powerplan（BOOST_MODE_LABELS /
# BOOST_MODE_CYCLE），本模块只负责取色与显示，避免两处硬编码打架。
_BOOST_COLORS = {0: "#ff7a7a", 2: "#5fe08a", 3: "#a8e05f", 4: "#ffb35c"}
_BOOST_COLOR_OTHER = "#9aa6b8"  # 1 / 5 / 6 及任何未知值：中性灰
# 「待应用」标记：点击睿频按钮只改本地待应用值、不写电源计划（与最大频率/最大
# 处理器状态两个滑块一致，统一由「应用」按钮下发），未下发期间在按钮文字尾部加
# 这个星号提示用户"还没生效，去按应用"。
_BOOST_PENDING_MARK = " *"


class PowerPlanPanel(BlockPanel):
    """电源计划卡片：显示当前计划 + 计划列表（高亮运行中）+ 点击切换。

    预设计划（YMSilent / YMPerformance / YMMedium）常驻列表且不可移除；用户计划由
    卡片编辑项导入。列表行点击即切换（powercfg /setactive，普通用户即可）。运行中
    的计划按钮高亮 + 前缀 ● 标记。非管理员且预设计划未导入时给出提示。
    """

    def _setup(self) -> None:
        b = self.instance
        # 顶部只保留「系统实际」一行（正在运行计划在列表里以 ● + accent 高亮标识，
        # 无需再单列「你的选择」）。
        self.cur_lbl = QLabel("")
        self.cur_lbl.setStyleSheet(
            "color:#cdd6e4; font-size:12px; font-weight:bold;")
        self.root.addWidget(self.cur_lbl)

        self.admin_lbl = QLabel("")
        self.admin_lbl.setStyleSheet("color:#ffb454; font-size:12px;")
        self.admin_lbl.setVisible(False)
        self.root.addWidget(self.admin_lbl)

        self.scroll, self.list_box, _ = _scroll_box()
        self.scroll.setMaximumHeight(150)
        # 计划列表外框已去除（按需求去掉浅灰色边框），仅保留透明容器包裹所有电源计划项。
        self.list_frame = QFrame()
        self.list_frame.setFrameStyle(QFrame.Shape.NoFrame)
        self.list_frame.setStyleSheet(
            "QFrame { border: none; background: transparent; }")
        _list_fb = QVBoxLayout(self.list_frame)
        _list_fb.setContentsMargins(0, 0, 0, 0)
        _list_fb.setSpacing(0)
        _list_fb.addWidget(self.scroll)
        self.root.addWidget(self.list_frame)

        # 每行固定池：(row 容器, 切换按钮, 运行中标记)。
        self.rows: list[tuple[QWidget, QPushButton, QLabel]] = []

        self.empty_hint = _label("（系统中暂无可用电源计划）",
                                 color="#6b7689", size=11)
        self.empty_hint.setWordWrap(True)
        self.root.addWidget(self.empty_hint)

        # --- 频率控制（仅改当前活动计划；拖拽设目标，按 Y 应用）---
        # 2 个滑块：① 处理器最大频率 (MHz, 75b0ae3f) ② 最大处理器状态 (%,bc5038f7)。
        # 后者是 AMD Ryzen 上真正可靠的调频旋钮，故一并纳入。
        self.freq_lbl = QLabel("")
        self.freq_lbl.setStyleSheet("color:#cdd6e4; font-size:12px;")
        # 「睿频」状态按钮：检测并显示当前活动计划的「处理器性能提升模式」，
        # 点击沿 已禁用(0) → 高效(2) → 高效激进(3) → 激进(4) → 已禁用 循环切换。
        self.boost_btn = _btn("睿频：…", self.accent)
        # 固定宽度：按钮文字随档位切换（已禁用/高效/高效激进/激进）长短不一，
        # 不锁宽的话每次切换都会把同一行的「最大频率」标签左右挤动。一次性按
        # 「所有可能文案中最宽的一条 + 待应用星号」锁死，切换时宽度恒定不跳。
        self.boost_btn.reserve_width_for(self._boost_text_candidates())
        _freq_row = QWidget()
        _freq_hb = QHBoxLayout(_freq_row)
        _freq_hb.setContentsMargins(0, 0, 0, 0)
        _freq_hb.setSpacing(8)
        _freq_hb.addWidget(self.freq_lbl, 1)
        _freq_hb.addWidget(self.boost_btn, 0)
        self.root.addWidget(_freq_row)

        self.freq_slider = _NoScrollSlider(Qt.Orientation.Horizontal)
        self.freq_slider.setRange(_FREQ_MIN, _FREQ_MAX)
        self.freq_slider.setSingleStep(_FREQ_STEP)
        # 鼠标拖拽最大频率时最小步长=100MHz（setValue 取整，键盘/程序读取同步对齐）。
        self.freq_slider.set_snap_step(_FREQ_STEP)
        self.root.addWidget(self.freq_slider)

        self.state_lbl = QLabel("")
        self.state_lbl.setStyleSheet("color:#cdd6e4; font-size:12px;")
        self.root.addWidget(self.state_lbl)

        self.state_slider = _NoScrollSlider(Qt.Orientation.Horizontal)
        self.state_slider.setRange(_STATE_MIN, _STATE_MAX)
        self.state_slider.setSingleStep(_STATE_STEP)
        self.root.addWidget(self.state_slider)

        self.freq_apply = _btn("应用", self.accent)
        self.root.addWidget(self.freq_apply)

        # 滑块初值：优先用已保存的目标值，否则读当前活动计划的实际值
        # （MHz 维度读不到回退到上限；% 维度读不到回退到 100=不限）。
        # 平台未暴露 MHz 设置时（freq_setting_available()==False）绝不回退到量程
        # 上限——那会让每一个计划都显示 5500 MHz，看起来像「滑块永不跟随计划变化」。
        if self._sync_freq_availability():
            init_f = 0
            try:
                init_f = int(getattr(b.config.powerplan_config,
                                     "max_freq_mhz", 0) or 0)
            except Exception:
                init_f = 0
            if init_f < _FREQ_MIN or init_f > _FREQ_MAX:
                try:
                    init_f = b.read_max_freq() or _FREQ_MAX
                except Exception:
                    init_f = _FREQ_MAX
            init_f = max(_FREQ_MIN, min(_FREQ_MAX, init_f))
            self.freq_slider.setValue(init_f)
            self._update_freq_label(init_f)

        init_s = 100
        try:
            init_s = int(getattr(b.config.powerplan_config,
                                 "max_state_pct", 100) or 100)
        except Exception:
            init_s = 100
        if init_s < _STATE_MIN or init_s > _STATE_MAX:
            try:
                init_s = b.read_max_state() or 100
            except Exception:
                init_s = 100
        init_s = max(_STATE_MIN, min(_STATE_MAX, init_s))
        self.state_slider.setValue(init_s)
        self._update_state_label(init_s)

        # 信号绑定：拖拽更新标签 + 防抖保存；应用由 nav_items 的 confirm 统一负责。
        self.freq_slider.valueChanged.connect(self._on_freq)
        self.state_slider.valueChanged.connect(self._on_state)
        self.freq_slider.sliderReleased.connect(self._flush_save)
        self.state_slider.sliderReleased.connect(self._flush_save)
        self._bind(self.freq_slider, "slider")
        self._bind(self.state_slider, "slider")
        self._bind(self.freq_apply, "action")
        self._bind(self.boost_btn, "action")

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._flush_save)

        # 记录「上一次同步时的活动计划 GUID」，用于检测系统电源计划是否发生更变。
        # 计划更变时主动从系统重新读取该计划的「最大频率 / 最大处理器状态」并同步
        # 滑块，避免滑块一直卡在旧计划的某个固定值（见 sync()）。
        self._last_plan_guid = None
        # 睿频（提升模式）的「待应用」值：点击睿频按钮只改它，不写电源计划；
        # 由「应用」按钮/手柄 Y 与两个频率维度一起下发。None = 无待应用改动。
        self._pending_boost: "int | None" = None

    def _boost_text_candidates(self) -> "list[str]":
        """睿频按钮所有可能出现的文案（含带「待应用」星号的变体），用于锁死按钮宽度。

        档位名经 block 实例代理取（唯一真相源 BOOST_MODE_LABELS 在 blocks.powerplan，
        本模块顶层 import 它会循环导入）；取不到时退回一组硬编码档位名兜底，
        最坏情况是宽度差几像素，不会让布局跳。
        """
        try:
            names = [str(self.instance.boost_mode_label(v)) for v in range(0, 7)]
        except Exception:
            names = ["已禁用", "已启用", "高效", "高效激进", "激进",
                     "高效激进(保证)", "高效(保证)"]
        # 读不到(None) / 超出已知档位(如 7)时的兜底文案也要算进最宽值。
        names += ["未知", "值7"]
        return ([f"睿频：{n}" for n in names]
                + [f"睿频：{n}{_BOOST_PENDING_MARK}" for n in names])

    def _ensure(self, n: int) -> None:
        while len(self.rows) < n:
            row = QWidget()
            hb = QHBoxLayout(row)
            hb.setContentsMargins(0, 0, 0, 0)
            hb.setSpacing(6)
            btn = _btn("", self.accent)
            btn.setStyleSheet("QPushButton{text-align:left; padding-left:8px;}")
            # 鼠标点击必须走统一的 InteractionController：否则 clicked 不会触发
            # activate_widget，计划项既收不到焦点矩形（看起来“点不动/选不中”），
            # 也不会调用 nav_items 里注册的 _switch（切换永不发生）。
            # HintButton 自身是 NoFocus（键盘焦点交给窗口统一导航），所以这条
            # _bind 是鼠标路径唯一的入口，缺了它鼠标就完全选不了计划。
            self._bind(btn, "action")
            mark = QLabel("●")
            mark.setFixedWidth(14)
            mark.setStyleSheet(f"color:{self.accent}; font-size:12px;")
            hb.addWidget(btn, 1)
            hb.addWidget(mark, 0)
            self.list_box.addWidget(row)
            self.rows.append((row, btn, mark))
        while len(self.rows) > n:
            row, btn, mark = self.rows.pop()
            self.list_box.removeWidget(row)
            row.deleteLater()

    def sync(self) -> None:
        b = self.instance
        plans = b.plans()
        active = b.active_guid
        diverged = bool(getattr(b, "diverged", False))
        self.cur_lbl.setText(f"系统实际：{b.active_name or '（未知）'}")
        self.cur_lbl.setStyleSheet(
            "color:#ffb454; font-size:12px; font-weight:bold;" if diverged
            else "color:#cdd6e4; font-size:12px; font-weight:bold;")
        if not b.admin and b.builtin_missing:
            self.admin_lbl.setText(
                "⚠ 预设计划未导入：请以管理员身份运行本程序")
            self.admin_lbl.setVisible(True)
        else:
            self.admin_lbl.setText("")
            self.admin_lbl.setVisible(False)
        # 「当前计划与上次选择不一致」提醒：挂到卡片标题「电源计划」右侧角标
        # （基类 _set_corner 标准接口，与标题同一行右对齐）；未不一致时隐藏角标、
        # 不留占位高度。
        if diverged:
            self._set_corner("⚠ 当前计划与上次选择不一致", "#ff6b6b")
        else:
            self._corner_lbl.setVisible(False)
        n = len(plans)
        self._ensure(n)
        for i, p in enumerate(plans):
            row, btn, mark = self.rows[i]
            row.setVisible(True)
            btn.setText(p["name"])
            is_active = (p["guid"] == active)
            # 运行中计划：accent 实心高亮；其余 frosted。
            btn.set_style(self.accent, is_active)
            mark.setVisible(is_active)
        for i in range(n, len(self.rows)):
            self.rows[i][0].setVisible(False)
        self.scroll.setVisible(n > 0)
        self.empty_hint.setVisible(n == 0)

        # 电源计划发生更变时，从系统重新读取当前活动计划的实际「最大频率 / 最大
        # 处理器状态」并同步滑块与标签，而不是一直卡在旧计划的某个固定值。
        # （首次同步 active_guid 与 _last_plan_guid(None) 不一致也会触发一次初始读取。）
        if b.active_guid and b.active_guid != self._last_plan_guid:
            self._refresh_freq_state_from_plan()
            self._last_plan_guid = b.active_guid
        elif not b.active_guid:
            # 系统暂时未探测到活动计划：重置标记，下次出现计划时再读。
            self._last_plan_guid = None

        # 睿频（处理器性能提升模式）状态按钮：读当前活动计划的实际值并刷新。
        self._update_boost_btn()

    def nav_items(self) -> list:
        items = []
        plans = self.instance.plans()
        for i in range(len(plans)):
            if i < len(self.rows):
                btn = self.rows[i][1]
                guid = plans[i]["guid"]
                items.append(NavItem(
                    btn, "action",
                    confirm=lambda guid=guid: self._switch(guid)))
        # 焦点顺序：列表最后一项之后先进入「睿频」按钮，再进入最大频率滑块。
        # 睿频按钮保留鼠标/Enter 可点击，但不再绑定专属手柄键（无 hint）。
        items.append(NavItem(self.boost_btn, "action",
                            confirm=self._toggle_boost))
        # 两个频率滑块：方向键微调(nudge)，按 A/Enter 直接应用(confirm)。
        items.append(NavItem(self.freq_slider, "slider",
                            confirm=self._apply_freq, nudge=self._nudge_freq))
        items.append(NavItem(self.state_slider, "slider",
                            confirm=self._apply_freq, nudge=self._nudge_state))
        # 应用按钮：手柄/键盘 Y 快捷键快速应用当前两个滑块目标值到活动计划。
        items.append(NavItem(self.freq_apply, "action",
                            confirm=self._apply_freq, hint="Y"))
        return items

    def _update_freq_label(self, v: int) -> None:
        # 滑块拉到量程顶（5500MHz）即代表系统「0=不限制」，明确写成「未限制」
        # 而非假的 "5500 MHz"，避免用户误以为自己设了 5500 上限。
        if v >= _FREQ_MAX:
            self.freq_lbl.setText("最大频率：未限制（无限）")
        else:
            self.freq_lbl.setText(f"最大频率：{v} MHz")

    def _update_state_label(self, v: int) -> None:
        self.state_lbl.setText(f"最大处理器状态：{v}%")

    def _on_freq(self, v: int) -> None:
        self._update_freq_label(v)
        self._save_timer.start()  # 防抖保存，满足「拖拽条配置能保存」

    def _on_state(self, v: int) -> None:
        self._update_state_label(v)
        self._save_timer.start()

    def _flush_save(self) -> None:
        fv = self.freq_slider.value()
        sv = self.state_slider.value()
        try:
            self.instance.config.powerplan_config.max_freq_mhz = fv
            self.instance.config.powerplan_config.max_state_pct = sv
            app = self.instance.app
            if app is not None and hasattr(app, "save_all"):
                app.save_all()
        except Exception:
            pass

    def _nudge_freq(self, delta: int) -> bool:
        # delta 为方向符号(±1)，乘步长得 ±_FREQ_STEP(100MHz)。
        nv = max(_FREQ_MIN, min(_FREQ_MAX,
                                self.freq_slider.value() + delta * _FREQ_STEP))
        self.freq_slider.setValue(nv)
        self._on_freq(nv)
        return True

    def _nudge_state(self, delta: int) -> bool:
        # delta 为方向符号(±1)，乘步长得 ±_STATE_STEP(5%)。
        nv = max(_STATE_MIN, min(_STATE_MAX,
                                 self.state_slider.value() + delta * _STATE_STEP))
        self.state_slider.setValue(nv)
        self._on_state(nv)
        return True

    def _read_boost_raw(self) -> "int | None":
        """读当前活动计划的「处理器性能提升模式」原始值（0/3/…），读不到返回 None。"""
        try:
            return self.instance.read_boost_mode()
        except Exception:
            return None

    def _boost_name(self, bm: "int | None") -> str:
        """把提升模式原始值翻成中文档位名。

        经 block 实例代理取（``blocks.powerplan`` 反向 import 本模块，顶层直接
        import 会循环导入）；block 缺席/异常时退化为原始数字，绝不让卡片崩。
        """
        try:
            return str(self.instance.boost_mode_label(bm))
        except Exception:
            return "未知" if bm is None else str(bm)

    def _toggle_boost(self) -> None:
        """点击「睿频」按钮：沿 已禁用(0) → 高效(2) → 高效激进(3) → 激进(4)
        → 回到已禁用 逐档循环，但**只改本地待应用值，绝不写电源计划**。

        与「最大频率 / 最大处理器状态」两个滑块同规则：拖动/切换都只是本地
        选择，统一由「应用」按钮（手柄 Y / Enter）真正下发——否则想试一档就
        会把当前活动计划直接改掉，无法反悔。待应用期间按钮文字尾部带
        ``_BOOST_PENDING_MARK``（" *"）提示"还没生效"。

        旧行为是「0 ↔ 3 二元切换」，于是从 2（本机高性能计划默认值）出发想关
        睿频必须点两次、中间还要先经过更激进的 3。改成循环后每一档都能正向走
        到，当前值不在循环内（1 / 5 / 6 / 读不到）时下一跳统一落到 0，
        最多点 4 次一定能回到「已禁用」。
        """
        # 有未下发的待应用值时，下一跳从它继续（连点 = 连续往后翻档），
        # 否则从系统当前值起步。
        cur = self._pending_boost
        if cur is None:
            cur = self._read_boost_raw()
        try:
            target = int(self.instance.next_boost_mode(cur))
        except Exception:
            target = 0
        self._pending_boost = target
        # 只刷按钮：不写电源计划，也就无需 sync() 去跑一轮 powercfg 重读列表。
        self._update_boost_btn()

    def _update_boost_btn(self) -> None:
        """根据当前提升模式刷新「睿频」按钮文字与状态色（填充状态块）。

        每档都显示真实中文名（已禁用 / 高效 / 高效激进 / 激进），不再把
        0、3 之外的值笼统显示成「其他(N)」——那会让用户以为读到的是异常值。

        有未下发的待应用值时，显示的是**待应用值**并加尾标 " *"，颜色也按
        待应用值着色，让用户一眼看到"即将变成什么样"。
        """
        bm = self._read_boost_raw()
        pending = self._pending_boost
        shown = pending if pending is not None else bm
        mark = _BOOST_PENDING_MARK if pending is not None else ""
        self.boost_btn.setText(f"睿频：{self._boost_name(shown)}{mark}")
        try:
            color = _BOOST_COLORS.get(int(shown), _BOOST_COLOR_OTHER)
        except Exception:
            color = _BOOST_COLOR_OTHER
        self.boost_btn.set_style(color, True)

    def _sync_freq_availability(self) -> bool:
        """按平台是否暴露 MHz 频率设置，决定频率滑块的启用态与文案。

        平台支持判定已改为看**全局设置定义**是否存在（见
        ``PowerPlanBlock.freq_setting_available`` / ``_global_setting_defined``）：
        在任意带处理器电源管理的 Windows（含 HX370/8945HS 掌机）上，
        「处理器最大频率」(75b0ae3f…e100) 的全局定义都存在，故滑块**始终可用**，
        不再因「当前活动方案没物化 e100 覆盖键」而误报「本平台不支持」。

        读不到具体方案值（e100 未在该方案设过）时，由 ``read_max_freq`` 回退为 0
        （不限），滑块显示「未限制」而非假 5500 或错误提示。

        返回 True 表示可用（调用方应继续读取并显示真实 MHz 值）。
        """
        b = self.instance
        try:
            supported = bool(b.freq_setting_available())
        except Exception:
            supported = False
        self.freq_slider.setEnabled(supported)
        # 不支持时整个滑块隐藏：留一个永远拉不动、又显示着假数值(5500)的控件，
        # 只会让人以为「调了没生效」。改为只留一行说明，指明真正的限频旋钮。
        self.freq_slider.setVisible(supported)
        if not supported:
            self.freq_slider.setValue(_FREQ_MAX)
            self.freq_lbl.setText("最大频率：本平台不支持（请用最大处理器状态限频）")
        return supported

    def _refresh_freq_state_from_plan(self) -> None:
        """从系统当前活动计划重新读取「最大频率 / 最大处理器状态」并同步滑块+标签。

        频率维度仅在平台确实暴露了该设置时才读取（见 _sync_freq_availability）；
        否则保持「未限制」文案，绝不回退到量程上限冒充真实值。
        状态(% )维度读不到时回退 100%(不限)。

        系统侧计划发生更变（插拔电被系统切走等）时同样丢弃待应用的睿频档位：
        它属于上一个计划，绝不能跟着落到新计划上。
        """
        self._pending_boost = None
        b = self.instance
        if self._sync_freq_availability():
            try:
                fv = b.read_max_freq() or _FREQ_MAX
            except Exception:
                fv = _FREQ_MAX
            fv = max(_FREQ_MIN, min(_FREQ_MAX, fv))
            self.freq_slider.setValue(fv)
            self._update_freq_label(fv)

        try:
            sv = b.read_max_state() or 100
        except Exception:
            sv = 100
        sv = max(_STATE_MIN, min(_STATE_MAX, sv))
        self.state_slider.setValue(sv)
        self._update_state_label(sv)

    def _apply_freq(self) -> None:
        """把「待应用」的三个维度一次性下发到【当前活动】计划。

        三个维度同规则：睿频（提升模式）、最大频率(MHz)、最大处理器状态(%)，
        都只在这里（应用按钮 / 手柄 Y / 滑块上按 A·Enter）才真正写电源计划，
        在此之前无论怎么点睿频、怎么拖滑块都只改本地值。
        """
        # 睿频：有未下发的档位才写，写完立即清空待应用标记（按钮星号消失）。
        pending = self._pending_boost
        if pending is not None:
            self.instance.apply_boost_mode(pending)
            self._pending_boost = None
        sv = self.state_slider.value()
        if self.freq_slider.isEnabled():
            self.instance.apply_freq_settings(mhz=self.freq_slider.value(),
                                              state_pct=sv)
        else:
            # 平台未暴露 MHz 设置：只下发「最大处理器状态」，避免写一个必然被
            # 平台拒绝的值（旧代码会把假的 5500MHz 一起发下去，然后提示部分设置被跳过）。
            self.instance.apply_freq_settings(mhz=None, state_pct=sv)
        self.sync()

    def _switch(self, guid: str) -> None:
        # 待应用的睿频档位属于**切换前**那个计划，切计划后必须丢弃——否则会
        # 把旧计划的选择写进新计划。
        self._pending_boost = None
        self.instance.set_active(guid)
        self.sync()


# --------------------------------------------------------------------------
# HWiNFO 监控（HwinfoBlock 的卡片）
# --------------------------------------------------------------------------
class HwinfoPanel(BlockPanel):
    """HWiNFO 监控卡片：五层状态 + 12h 倒计时 + 关键读数 + 一键重启。

    存在的意义就一条：**让「温度源静默回落到 ACPI」这件事无处藏身**。
    fan 卡片的 ``TempSource="hwinfo"`` 一旦读不到数据就会静默改用 ACPI 皮肤温度
    （比 CPU 结温低 10~20℃），风扇转速随之算低——「80℃ 风扇仍低转」的老坑。
    免费版共享内存还有 12 小时自动断供的限制，所以这张卡必须常驻可见。

    本卡片只读 + 一个手动按钮，**绝不自动拉起或杀掉 HWiNFO**（见
    ``blocks/hwinfo.py`` 模块 docstring）。
    """

    # 状态 → (显示色, 角标文本)。色值跟着主题走，但语义色固定：
    # 绿=可用 黄=即将到期 红=不可用 灰=未运行/未安装
    _STATE_COLORS = {
        "ok": "#4ade80",
        "warning": "#f5d74b",
        "stale": "#e64b5a",
        "no_sm": "#e64b5a",
        "stopped": "#8b97aa",
        "missing": "#8b97aa",
    }

    def _setup(self) -> None:
        # 第一行：状态点 + 状态文本
        head = QHBoxLayout()
        self.state_dot = QLabel("●")
        self.state_dot.setStyleSheet("color:#8b97aa; font-size:13px;")
        self.state_dot.setFixedWidth(14)
        self.state_lbl = _label("检测中…", color="#cdd6e4", size=12)
        head.addWidget(self.state_dot)
        head.addWidget(self.state_lbl, 1)
        self.root.addLayout(head)

        # 第二行：版本 / 12h 倒计时 / 读数条数
        self.meta_lbl = _label("", color="#8b97aa", size=11)
        self.meta_lbl.setWordWrap(True)
        self.root.addWidget(self.meta_lbl)

        # 第三行：关键实测读数（CPU 结温 + 电池功率）
        self.read_lbl = _label("", color="#cdd6e4", size=12)
        self.read_lbl.setWordWrap(True)
        self.root.addWidget(self.read_lbl)

        # 弱提示行：可能没提权 / 部署状态。无内容时隐藏。
        self.hint_lbl = _label("", color="#f5d74b", size=11)
        self.hint_lbl.setWordWrap(True)
        self.hint_lbl.setVisible(False)
        self.root.addWidget(self.hint_lbl)

        self.root.addStretch(1)

        foot = QHBoxLayout()
        self.restart_btn = _btn("重启 HWiNFO", self.accent)
        foot.addWidget(self.restart_btn)
        foot.addStretch(1)
        self.root.addLayout(foot)
        self._bind(self.restart_btn, "action")

    # -- 动作 -------------------------------------------------------------
    def _on_restart(self) -> None:
        self.instance.restart_hwinfo()
        self.sync()

    # -- 状态绘制 ---------------------------------------------------------
    def sync(self) -> None:
        st = self.instance.probe_status()

        color = self._STATE_COLORS.get(st.state, "#8b97aa")
        self.state_dot.setStyleSheet(f"color:{color}; font-size:13px;")
        self.state_lbl.setText(st.text)

        # 角标：12h 倒计时（未知时不显示）
        if st.remaining_s >= 0 and st.state in ("ok", "warning"):
            self._corner_lbl.setText(f"剩余 {st.remaining_text()}")
            self._corner_lbl.setStyleSheet(
                f"color: {color}; font-size: 11px;" if st.state == "warning"
                else "color: #9aa6b8; font-size: 11px;"
            )
            self._corner_lbl.setVisible(True)
        else:
            self._corner_lbl.setVisible(False)

        # meta 行
        bits = []
        if st.version:
            bits.append(f"v{st.version}")
        if st.running:
            h = int(st.uptime_s // 3600)
            m = int((st.uptime_s % 3600) // 60)
            bits.append(f"已运行 {h}h{m:02d}m")
        bits.append(f"读数 {st.readings}" if st.sm_ok else "共享内存未连接")
        self.meta_lbl.setText(" · ".join(bits))

        # 读数行
        reads = []
        if st.temp_cpu is not None:
            reads.append(f"CPU {st.temp_cpu:.0f}℃")
        if st.batt_w is not None:
            reads.append(f"电池 {'+' if st.batt_w >= 0 else ''}{st.batt_w:.1f}W")
        self.read_lbl.setText(" · ".join(reads) if reads else "（暂无读数）")

        # 弱提示
        hints = []
        if st.maybe_not_elevated:
            hints.append("读数偏少，HWiNFO 可能没以管理员运行")
        deploy_state, deploy_text = getattr(self.instance, "deploy_state", ("idle", ""))
        # 真值来自 probe：只要进程在跑（无论装哪、是否后启动），「未检测到」就是
        # 启动时的过期快照，必须压住不显示——否则会与状态行「运行中」自相矛盾。
        if deploy_state == "missing" and not st.running:
            hints.append("未检测到 HWiNFO，请前往 hwinfo.com 下载便携版")
        elif deploy_state == "deployed":
            hints.append(f"已自动部署：{deploy_text}")
        self.hint_lbl.setText("；".join(hints))
        self.hint_lbl.setVisible(bool(hints))

        # 按钮：重启中置灰；未安装时改为引导语义（点了也只弹 toast）
        if getattr(self.instance, "restarting", False):
            self.restart_btn.setText("重启中…")
            self.restart_btn.setEnabled(False)
        else:
            self.restart_btn.setEnabled(True)
            self.restart_btn.setText(
                "重启 HWiNFO" if st.running else "启动 HWiNFO"
            )

    def nav_items(self) -> list:
        return [
            NavItem(self.restart_btn, "action", confirm=lambda: self._on_restart()),
        ]


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------
_PANELS = {
    "rtss": RtssPanel,
    "freeze": FreezePanel,
    "power": PowerPanel,
    "fan": FanPanel,
    "undervolt": UndervoltPanel,
    "procmon": ProcMonPanel,
    "ls": LSPanel,
    "tile": TilePanel,
    "perfmon": PerfMonPanel,
    "smt": SmtPanel,
    "tasklist": TaskListPanel,
    "changelog": ChangelogPanel,
    "preset": PresetPanel,
    "powerplan": PowerPlanPanel,
    "hwinfo": HwinfoPanel,
}


def make_panel(block_type: str, instance: Any, title: str, color: Any,
               block_key: str | None = None, cfg: Any = None) -> BlockPanel:
    cls = _PANELS.get(block_type, BlockPanel)
    panel = cls(instance, title, color, block_key=block_key, cfg=cfg)
    # Tag every panel with its block type so the button editor can group
    # buttons by functional block (mirrors button-editor.ps1 GetBlockLabel).
    panel.block_type = block_type
    return panel