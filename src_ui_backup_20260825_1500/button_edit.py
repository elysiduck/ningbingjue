"""In-window Button Style Editor — faithful port of ``button-editor.ps1``.

Mirrors the original ``Show-ButtonStyleEditor`` but rendered *inside* the main
edit overlay (a ``QStackedWidget`` child, never a dialog), matching how the
tile / procmon editors are hosted.  It implements the original's two-layer
preset system:

* **L2 整体风格**  — ``BUTTON_STYLES`` (global, applied to every button).
* **L1 定制预设**  — ``PER_BUTTON_PRESETS`` (per-button / per-group presets).
* **绑定映射**     — ``BUTTON_PRESET_MAP`` (button key → preset name).

Layout is **top-to-bottom** (the panel is narrow, so a left/right split cramps
both halves): Top = a compact header (back / title / status), then a live
*preview list* of every button in the HUD grouped by functional block (mirrors
``RenderCanvas``); Bottom = style selector + live preview + preset list +
property editor (name / cut / corner / border / 3 checkboxes / 11 colour
swatches / active+inactive states).

Every preview item is a **type-faithful clone** of its live source button —
``TileButton`` sources clone into a ``TileButton`` (cut-corner polygon + name
label rendered *inside* the button, plus icon), ``QPushButton`` sources clone
into a ``QPushButton`` carrying the exact same ``text()`` and ``styleSheet()``
— so the list always matches what the HUD actually shows.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Optional

from PyQt6.QtCore import (
    QPoint,
    QRect,
    QSize,
    Qt,
    QTimer,
    QPropertyAnimation,
    QEasingCurve,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QWheelEvent, QPainter, QPainterPath, QRegion
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QLayout,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..core.color import styles as _BS
from ..core.color.rgb import hex_to_color
from ..core.color.semantic import get_theme_accent
from ..core.color.styles import (
    BUTTON_PRESET_MAP,
    BUTTON_SELECTION,
    BUTTON_STYLES,
    DIRECT_STYLES,
    PER_BUTTON_PRESETS,
    ButtonStyle,
    _apply_style_to_ctrl,
    apply_per_button_preset,
    apply_styles_to_buttons,
    bind_button_preset,
    save_all_button_data,
    unbind_button_preset,
)
# P0-2 fix: _coerce_focus_opacity moved to core/config so the load path
# (AppConfig.from_dict) and the editor path share one defensive parser.
# Re-export here so existing `from ningbingjue.ui.button_edit import
# _coerce_focus_opacity` (tests) keeps working.
from ..core.config import _coerce_focus_opacity  # noqa: F401  (re-export)
from .interaction import NavItem, build_focus_style
from .faq_popup import ClickableTitle
from .rounded_button import RoundedButton
from .tile_button import TileButton
from .gamepad_hint import HintButton


class _NoWheelSlider(QSlider):
    """Horizontal slider that ignores the mouse wheel (so scrolling the
    editor never nudges 切角/圆角/边框 by accident).  Wheel events are
    swallowed instead of changing the value."""

    def __init__(self, orientation: Qt.Orientation = Qt.Orientation.Horizontal,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(orientation, parent)

    def wheelEvent(self, _e: "QWheelEvent") -> None:  # noqa: N802
        _e.accept()  # 完全吞掉滚轮：既不改值，也不让编辑器跟着滚


# block_type → Chinese group label (mirrors GetBlockLabel ordering).
_BLOCK_ORDER = ["ls", "rtss", "power", "fan", "freeze", "tile", "procmon", "header", "other"]
_BLOCK_LABELS = {
    "ls": "LS预设", "rtss": "锁帧控制", "power": "电源设置", "fan": "风扇控制",
    "freeze": "进程冻结", "tile": "页面布局编辑", "procmon": "进程监控",
    "header": "主页面顶端", "other": "其他",
}

# 颜色字段按「按钮状态」分组，重新梳理每个颜色对应的状态：
#   常规态  → 静止时看到的样子
#   悬停态  → 鼠标移上去时看到的样子
#   开关态  → 仅 toggle/开关按钮的 开/关 两种状态
_COLOR_GROUPS = [
    ("常规态", [
        ("BackColor", "背景"),
        ("BorderColor", "边框"),
        ("ForeColor", "文字"),
    ]),
    ("悬停态", [
        ("HoverColor", "悬停"),
        ("HoverForeColor", "悬浮文字"),
        ("HoverBorderColor", "悬浮边框"),
        ("GradientHoverTop", "渐变顶"),
        ("GradientHoverBottom", "渐变底"),
    ]),
    ("开关态", [
        ("ActiveBackColor", "激活背景"),
        ("ActiveBorderColor", "激活边框"),
        ("InactiveBackColor", "未激活背景"),
        ("InactiveBorderColor", "未激活边框"),
    ]),
]
# 这些分组标题下的色块行要居中（常规态只有 3 个色块，居中更美观）。
_CENTERED_GROUPS = set()  # 三个颜色分组统一左对齐，与悬停/开关态一致
# 扁平列表（保持与旧代码兼容：_COLOR_FIELDS 仍被 _blank_editor / 遍历使用）。
_COLOR_FIELDS = [(k, l) for _, items in _COLOR_GROUPS for k, l in items]

# 定制预览的四个态按钮 → 各自依赖的颜色字段。某态的颜色全空时，该态按钮
# 置灰禁用（避免点了没反应、误以为"预览≠所选"）。常规态始终有底色，永不禁用。
_STATE_COLOR_KEYS = {
    "normal": [],
    "hover": ["HoverColor", "HoverForeColor", "HoverBorderColor",
              "GradientHoverTop", "GradientHoverBottom"],
    "active": ["ActiveBackColor", "ActiveBorderColor"],
    "inactive": ["InactiveBackColor", "InactiveBorderColor"],
}

# 每个颜色字段的含义 + 对应按钮状态（挂在色块 tooltip 上）
_COLOR_HELP = {
    "BackColor": "【常规态】按钮静止时的填充底色",
    "HoverColor": "【悬停态】鼠标移上去时的填充色（切到『悬停』预览态查看）",
    "BorderColor": "【常规态】按钮外框的常态颜色（静止时就显示）",
    "ForeColor": "【常规态】按钮文字的常态颜色",
    "HoverForeColor": "【悬停态】鼠标移上去时文字的颜色（切到『悬停』预览态查看）",
    "HoverBorderColor": "【悬停态】鼠标移上去时外框的颜色；留空则沿用『边框』色",
    "GradientHoverTop": "【悬停态】悬停渐变的上沿色（需勾选『悬停渐变』且在『悬停』态才显示）",
    "GradientHoverBottom": "【悬停态】悬停渐变的下沿色（需勾选『悬停渐变』且在『悬停』态才显示）",
    "ActiveBackColor": "【开关态】开关按钮『开』态的填充（切到『激活』预览态查看）",
    "ActiveBorderColor": "【开关态】开关按钮『开』态的外框",
    "InactiveBackColor": "【开关态】开关按钮『关』态的填充（切到『未激活』预览态查看）",
    "InactiveBorderColor": "【开关态】开关按钮『关』态的外框",
}
_SLIDER_HELP = {
    "切角": "切掉按钮四角的斜角大小（0 = 直角，越大越斜）",
    "圆角": "按钮四角圆滑程度（0 = 直角；>0 变圆角/胶囊，圆角滑块即时生效）",
    "边框": "外框线条的粗细（像素）",
}

# Fallback border colour when a source button has none (matches RoundedButton default).
_MAUVE_FALLBACK = "5fafeb"


def _block_type_of(widget: QWidget) -> str:
    """Walk up to the nearest BlockPanel to learn the button's block type."""
    p = widget.parent()
    while p is not None:
        bt = getattr(p, "block_type", None)
        if bt:
            return bt
        p = p.parent()
    return "other"


def collect_button_groups(window: QWidget,
                           exclude: Optional[QWidget] = None,
                           popup: Optional[QWidget] = None,
                           extra_excludes: Optional[list[QWidget]] = None
                           ) -> "OrderedDict[str, list[tuple[str, QWidget]]]":
    """Collect every styled button in the HUD, grouped by block.

    Returns ``OrderedDict[block_type] -> [(key, btn), ...]`` where ``key`` is
    ``"block_type#local_index"`` (deterministic, stable across rebuilds).
    Buttons that are descendants of ``exclude`` (the editor itself) or of the
    floating ``popup`` (the button-list popup that hosts preview clones) are
    skipped, so preview clones never perturb the HUD button indices.  Buttons
    descending from any widget in ``extra_excludes`` are also skipped (used to
    drop the standalone "快捷按钮" editor window, whose own QPushButton controls
    must never appear in the button-*style* editor's preview list).
    """
    xtra = [w for w in (extra_excludes or []) if w is not None]
    raw: "OrderedDict[str, list]" = OrderedDict((k, []) for k in _BLOCK_ORDER)
    for cls in (RoundedButton, QPushButton):
        for b in window.findChildren(cls):  # type: ignore[arg-type]
            if exclude is not None and exclude.isAncestorOf(b):
                continue
            # Skip preview clones living in the floating button-list popup.
            if popup is not None and popup.isAncestorOf(b):
                continue
            # Skip buttons inside explicitly-excluded windows (e.g. the
            # "快捷按钮" shortcut editor — we only want the live HUD cards).
            if any(x.isAncestorOf(b) for x in xtra):
                continue
            # Skip HUD navigation chrome (the top page-number bar).  These are
            # QPushButton children of the window, not card buttons, and must not
            # show up — duplicated — in the editor's preview list.
            if b.property("page_nav"):
                continue
            bt = _block_type_of(b)
            if bt not in raw:
                raw[bt] = []
            raw[bt].append(b)
    result: "OrderedDict[str, list[tuple[str, QWidget]]]" = OrderedDict()
    for bt, btns in raw.items():
        if not btns:
            continue
        result[bt] = [(f"{bt}#{i}", b) for i, b in enumerate(btns)]
    return result


def _qcolor_to_hex(c: QColor) -> str:
    return f"{c.red():02x}{c.green():02x}{c.blue():02x}"


class FlowLayout(QLayout):
    """Horizontal wrapping layout.

    Preview clones flow left→right and wrap to the next line only when the
    combined width of the current row would exceed the available space
    (requirement: several buttons share a row until the row is full, then wrap).

    Widgets carrying the dynamic property ``flowBreak`` (our group-header
    labels) force a line break *before and after* them, so each block header
    sits on its own line above its button row.
    """

    def __init__(self, parent: Optional[QWidget] = None,
                 hspacing: int = 4, vspacing: int = 4) -> None:
        super().__init__(parent)
        self._hspacing = hspacing
        self._vspacing = vspacing
        self._items: list = []

    # -- QLayout interface ----------------------------------------------
    def addItem(self, item: "QLayoutItem") -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:  # noqa: N802
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int):  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self) -> QSize:  # noqa: N802
        w = self.contentsRect().width()
        if w <= 0 and self.parent() is not None:
            w = self.parent().width()
        if w <= 0:
            w = 200
        return QSize(w, self.heightForWidth(w))

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(2 * m.left(), 2 * m.top())
        return size

    # -- layout engine ---------------------------------------------------
    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        effective = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x = effective.x()
        y = effective.y()
        line_height = 0
        force_new_line = True  # the first item always starts a fresh line
        for item in self._items:
            widget = item.widget()
            is_break = bool(widget is not None and widget.property("flowBreak"))
            item_w = item.sizeHint().width()
            item_h = item.sizeHint().height()

            # A ``flowBreak`` widget always starts on its own line (unless it
            # is the very first item, in which case line_height is zero and we
            # are already at the top).
            if is_break and line_height > 0:
                x = effective.x()
                y += line_height + self._vspacing
                line_height = 0
            elif force_new_line:
                x = effective.x()
                if line_height > 0:
                    y += line_height + self._vspacing
                line_height = 0
            else:
                # Would this item overflow the current row?  Wrap if so.
                if x + item_w > effective.right() and line_height > 0:
                    x = effective.x()
                    y += line_height + self._vspacing
                    line_height = 0
            force_new_line = False  # consumed

            if not test_only:
                item.setGeometry(QRect(x, y, item_w, item_h))
            x += item_w + self._hspacing
            line_height = max(line_height, item_h)
            if is_break:
                force_new_line = True

        return y + line_height - effective.y() + m.top() + m.bottom()


class ButtonEditPanel(QWidget):
    """Inline button-style editor (port of ``Show-ButtonStyleEditor``)."""

    def __init__(self, app: Any, overlay: Any) -> None:
        super().__init__()
        self.setStyleSheet(
            "QLabel { background: transparent; border: none; }"
            "QFrame { border: none; background: transparent; }"
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
            "QSlider { border: none; background: transparent; }"
            "QSlider::groove:horizontal { border: none; height: 6px;"
            " background: rgba(255,255,255,0.12); border-radius: 3px; }"
            "QSlider::handle:horizontal { border: none; background: #7fd1ff;"
            " width: 12px; height: 12px; margin: -3px 0; border-radius: 6px; }"
            "QSlider::sub-page:horizontal { background: #7fd1ff; border-radius: 3px; }"
            "QCheckBox { spacing: 6px; color: #b8c2d0; font-size: 11px;"
            " border: none; background: transparent; }"
            "QCheckBox::indicator { width: 14px; height: 14px; border: none;"
            " border-radius: 3px; background: rgba(255,255,255,0.12); }"
            "QCheckBox::indicator:hover { background: rgba(255,255,255,0.18); }"
            "QCheckBox::indicator:checked { background: #7fd1ff; }"
            "QCheckBox::indicator:unchecked { background: rgba(255,255,255,0.12); }"
        )
        self._app = app
        self._overlay = overlay
        # 焦点蒙层（选中高亮）防抖存档：任何改动（鼠标拖滑块 / 键盘调滑块 /
        # 改颜色十六进制 / 取色器）都会经 _apply_focus_live 写内存，这里再启动
        # 一个 400ms 单发定时器把内存值落盘。这样无论用户是否触发
        # sliderReleased / editingFinished、是否先点返回再退出、或直接 Alt+F4
        # 退出，焦点蒙层参数都不会丢——不再依赖关退出/隐藏等单一触发点。
        self._focus_save_timer = QTimer(self)
        self._focus_save_timer.setSingleShot(True)
        self._focus_save_timer.setInterval(400)
        self._focus_save_timer.timeout.connect(self._save_focus_config)
        # 缓存用户最后一次明确设置的焦点蒙层值。_save_focus_config（关闭 /
        # 隐藏 / 松手 / 防抖）落盘时直接用缓存值写盘，不再重读滑块 / 颜色框
        # 的“当前”状态——否则关闭时控件可能已被重置（或被 refresh 回填），
        # 导致读到的透明度是默认 25 而非用户值（“透明度总是重置”的根因）。
        self._pending_focus_color: Optional[str] = None
        self._pending_focus_opacity: Optional[int] = None
        # 多选：canvas 上的按钮选择是一个集合，点击切换成员资格。
        # 退出/重进编辑器时恢复上次选中的按钮（持久化在 BUTTON_SELECTION）。
        self._sel_keys: set[str] = set(BUTTON_SELECTION)
        # render_canvas 实际渲染出来的按钮 key（用于「全选」只选可见项）。
        self._visible_keys: list[str] = []
        self._sel_preset: Optional[str] = None
        self._preset_name_draft: str = ""  # name typed into the 名称 field
        self._editor: dict[str, Any] = self._blank_editor()
        # True once the user *explicitly* edits the style (slider / colour) after
        # a preset was loaded.  While False, applying a selected preset uses the
        # preset's stored geometry (so a rounded preset is never silently
        # flattened to the editor's possibly-reset corner_radius — #3).
        self._dirty: bool = False
        # 定制预览的状态：normal / hover / active / inactive —— 决定哪些
        # 专属颜色在预览里可见（渐变/悬停前景只在 hover，激活边框只在 toggle）。
        self._preview_state: str = "normal"
        self._state_base_style: str = ""

        # 布局：顶部一行 header（返回 | 标题 | ? | 状态），其下直接是按钮编辑
        # 选项（滚动区）。按钮列表不再占用编辑器上半部分——它移到「左侧浮窗」
        # ButtonListPopup（复刻原本 FAQ 浮窗的 dock 左侧 / 滑出 / 跟随窗口行为），
        # 进入编辑器时自动弹出。标题点击不再弹 FAQ，改由右上角「?」按钮触发。
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ---- HEADER: one compact row (back | title | ? | status) --------
        header = QHBoxLayout()
        header.setSpacing(8)
        back = QPushButton("← 返回")
        back.setFixedHeight(24)
        back.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #cfe6ff;"
            " border: none; border-radius: 5px;"
            " padding: 2px 10px; font-size: 11px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.16); border: none; }"
            "QPushButton:pressed { background: rgba(255,255,255,0.12); border: none; }"
            "QPushButton:focus { border: none; }")
        back.clicked.connect(lambda _=False: self._overlay.open_layout())
        self._back_btn = back
        header.addWidget(back)
        title = QLabel("按钮风格")
        title.setStyleSheet("color: #7fd1ff; font-weight: bold; font-size: 13px;")
        header.addWidget(title)
        # 疑难解答（FAQ）改为由右上角「?」按钮触发；左侧浮窗位置改放按钮列表。
        help_btn = QPushButton("?")
        help_btn.setFixedSize(22, 22)
        help_btn.setToolTip("疑难解答 / 帮助")
        help_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #cfe6ff;"
            " border: none; border-radius: 5px; font-size: 12px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.16); border: none; }")
        help_btn.clicked.connect(lambda _=False: self._open_faq())
        header.addWidget(help_btn)
        header.addStretch(1)
        self._status = QLabel("点选按钮后可绑定预设")
        self._status.setStyleSheet("color: #8b97aa; font-size: 10px;")
        header.addWidget(self._status)
        root.addLayout(header)

        # ---- 按钮列表浮窗（左侧，进入编辑器自动弹出）------------------
        # 复刻 faq_popup.FaqPopup：作为主窗口的从属 Tool 窗口，停靠主窗口左侧、
        # 从边缘滑出、跟随主窗口移动。里面承载原本在编辑器内的按钮列表画布
        # （全选/清空工具栏 + 滚动区 + FlowLayout 画布），由 render_canvas() 渲染。
        parent_win = (self._overlay.window()
                      if self._overlay is not None else None) \
            or getattr(self._app, "_window", None) or self
        self._list_popup = ButtonListPopup(parent_win, self)
        # 登记为「关联窗口」：按钮列表弹窗获得焦点时，主窗口保持点亮 + 呼吸
        # （不按失焦处理）。主窗口不存在该方法时安全跳过。
        _reg = getattr(parent_win, "register_associated_window", None)
        if callable(_reg):
            _reg(self._list_popup)

        # ---- 编辑选项（滚动区，独占编辑器全部高度）-------------------
        right = QScrollArea()
        right.setWidgetResizable(True)
        # 控制列叠放多张卡（样式 / 预设 / 预览 / 属性）。允许纵向滚动条，保证
        # 最底部「保存为定制预设 / 覆盖当前预设 / 保存当前分配 / 应用并刷新画布」
        # 这些按钮在任何窗口高度下都可达。
        right.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        right.setStyleSheet("QScrollArea { border: none; background: transparent; }"
                             "QScrollBar:vertical { width: 0px; background: transparent; }"
                             "QScrollBar:horizontal { height: 0px; background: transparent; }")
        rwrap = QWidget()
        rlay = QVBoxLayout(rwrap)
        rlay.setContentsMargins(0, 0, 0, 0)
        rlay.setSpacing(8)
        right.setWidget(rwrap)
        self._controls_scroll = right
        root.addWidget(right, 1)

        self._build_style_section(rlay)
        self._build_preset_section(rlay)
        self._build_preview_section(rlay)
        self._build_property_section(rlay)
        self._build_focus_section(rlay)
        rlay.addStretch(1)

    # -- 疑惑解答（点击标题打开）--------------------------------------
    def _open_faq(self) -> None:
        faq = self._overlay.faq()
        if faq is not None:
            faq.open_for("button_edit", "鼠标 标题")

    # -- blank editor state ---------------------------------------------
    @staticmethod
    def _blank_editor() -> dict[str, Any]:
        return {
            "cut_size": 8, "corner_radius": 0, "border_thickness": 3,
            "hover_only_border": False, "hover_gradient": False, "link_theme": False,
            "colors": {k: "" for k, _ in _COLOR_FIELDS},
        }

    # -- section builders -----------------------------------------------
    def _btn(self, text: str, color: str) -> QPushButton:
        b = QPushButton(text)
        b.setStyleSheet(
            "QPushButton { background: rgba(95,175,235,0.18); color: #cfe6ff;"
            " border: none; border-radius: 6px;"
            " padding: 4px 8px; font-size: 11px; font-weight: bold; }"
            "QPushButton:hover { background: rgba(95,175,235,0.32); border: none; }"
            "QPushButton:pressed { background: rgba(95,175,235,0.22); border: none; }"
            "QPushButton:focus { border: none; }"
            "QPushButton:disabled { background: rgba(255,255,255,0.04);"
            " color: #5a6373; border: none; }")
        return b

    def _build_style_section(self, parent: QVBoxLayout) -> None:
        card = QFrame()
        card.setStyleSheet("QFrame { background: transparent; border-radius: 10px;"
                           " border: none; }")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        h = QLabel("整体分配方案 (L2)")
        h.setStyleSheet("color: #7fd1ff; font-weight: bold; font-size: 13px;")
        lay.addWidget(h)
        note = QLabel("命名化的『按钮 → 预设』绑定集合；未绑定的按钮保持 HUD 原生样子")
        note.setWordWrap(True)
        note.setStyleSheet("color: #8b97aa; font-size: 9px;")
        lay.addWidget(note)
        # Full-width single row: style combo stretches, actions trail right.
        row = QHBoxLayout()
        row.setSpacing(4)
        self._style_cb = QComboBox()
        self._style_cb.addItems(list(BUTTON_STYLES.keys()))
        self._style_cb.currentTextChanged.connect(self._on_style_changed)
        row.addWidget(self._style_cb, 1)
        b_new = self._btn("新建", "")
        b_copy = self._btn("复制", "")
        b_del = self._btn("删除", "")
        b_apply = self._btn("应用", "")
        b_new.clicked.connect(self._new_style)
        b_copy.clicked.connect(self._copy_style)
        b_del.clicked.connect(self._del_style)
        b_apply.clicked.connect(self._apply_style)
        for b in (b_new, b_copy, b_del, b_apply):
            row.addWidget(b)
        lay.addLayout(row)
        parent.addWidget(card)

    def _build_preview_section(self, parent: QVBoxLayout) -> None:
        card = QFrame()
        card.setStyleSheet("QFrame { background: transparent; border-radius: 10px;"
                           " border: none; }")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        h = QLabel("定制预览")
        h.setStyleSheet("color: #8fd4ff; font-weight: bold; font-size: 12px;")
        lay.addWidget(h)

        # 状态切换：让『悬停 / 激活 / 未激活』专属颜色在预览里可见
        # （渐变与悬停前景只在悬停态；激活/未激活边框只在开关态）。
        state_row = QHBoxLayout()
        state_row.setSpacing(4)
        self._state_btns: dict[str, QPushButton] = {}
        for st, lab in (("normal", "常规"), ("hover", "悬停"),
                        ("active", "激活"), ("inactive", "未激活")):
            b = self._btn(lab, "")
            b.setCheckable(True)
            b.clicked.connect(lambda _c=False, s=st: self._on_preview_state(s))  # type: ignore[assignment]
            self._state_btns[st] = b
            state_row.addWidget(b)
        lay.addLayout(state_row)
        self._state_base_style = self._state_btns["normal"].styleSheet()
        self._state_btns["normal"].setChecked(True)

        hint = QLabel("提示：渐变 / 悬停前景只在『悬停』态可见；"
                      "激活 / 未激活边框只在『激活 / 未激活』态可见（开关按钮）。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8b97aa; font-size: 9px;")
        lay.addWidget(hint)

        pnl = QFrame()
        pnl.setStyleSheet("QFrame { background: transparent; border: none;"
                          " border-radius: 6px; }")
        pl = QHBoxLayout(pnl)
        pl.setContentsMargins(8, 8, 8, 8)
        self._preview = RoundedButton()
        self._preview.setFixedSize(220, 30)
        self._preview.border_thickness = 2
        # 文字由 RoundedButton 内部绘制，跟随 ForeColor / HoverForeColor，
        # 不再用写死颜色的覆盖层（之前导致『前景色改了没用』）。
        self._preview.setText("预览按钮")
        pl.addStretch(1)
        pl.addWidget(self._preview)
        pl.addStretch(1)
        lay.addWidget(pnl)
        self._refresh_state_button_enabled()
        parent.addWidget(card)

    def _on_preview_state(self, state: str) -> None:
        """Switch the preview button's demo state and re-apply the style."""
        self._preview_state = state
        for st, b in self._state_btns.items():
            if st == state:
                b.setChecked(True)
                b.setStyleSheet("QPushButton { background: #2b6cb0; color: #fff;"
                                " border: none; border-radius: 5px;"
                                " padding: 3px 8px; font-weight: bold; }"
                                "QPushButton:hover { border: none; }"
                                "QPushButton:pressed { border: none; }"
                                "QPushButton:focus { border: none; }")
            else:
                b.setChecked(False)
                b.setStyleSheet(self._state_base_style)
        self._apply_to_preview()

    def _apply_preview_state(self) -> None:
        """Reflect ``self._preview_state`` on the preview button so the right
        colour group is demonstrated."""
        prev = self._preview
        st = self._preview_state
        if st == "hover":
            prev.clear_button_state()
            prev.set_hover_state(True)
        elif st == "active":
            prev.set_hover_state(False)
            prev.set_button_state(True)
        elif st == "inactive":
            prev.set_hover_state(False)
            prev.set_button_state(False)
        else:
            prev.set_hover_state(False)
            prev.clear_button_state()

    def _refresh_state_button_enabled(self) -> None:
        """Disable a 定制预览 state button when its colour group is empty.

        Clicking a state whose colours are all unset left the sample looking
        unchanged — the exact '预览 ≠ 我选的' confusion.  Greying the button
        (and tool-tipping why) makes the contract explicit.  The 常规态 always
        has a base colour, so it is never disabled."""
        for st, btn in self._state_btns.items():
            keys = _STATE_COLOR_KEYS.get(st, [])
            if not keys:
                btn.setEnabled(True)
                btn.setToolTip("")
                continue
            filled = any(self._editor["colors"].get(k) for k in keys)
            if filled:
                btn.setEnabled(True)
                btn.setToolTip("")
            else:
                btn.setEnabled(False)
                label = "悬停" if st == "hover" else "开关"
                btn.setToolTip(f"该状态未填色：在『属性编辑』给{label}态色块填色后才可预览")
                if self._preview_state == st:
                    # 当前正停在一个刚变空的态 → 回落到常规态，避免卡在看不见的态
                    self._on_preview_state("normal")

    def _build_preset_section(self, parent: QVBoxLayout) -> None:
        card = QFrame()
        card.setStyleSheet("QFrame { background: transparent; border-radius: 10px;"
                           " border: none; }")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        h = QLabel("定制预设 (L1)")
        h.setStyleSheet("color: #7fd1ff; font-weight: bold; font-size: 13px;")
        lay.addWidget(h)
        self._preset_list = QListWidget()
        self._preset_list.setFixedHeight(88)
        self._preset_list.setStyleSheet(
            "QListWidget { background: #14161c; color: #dfe6f0; border-radius: 6px;"
            " border: none; font-size: 11px; }")
        self._preset_list.currentTextChanged.connect(self._on_preset_selected)
        lay.addWidget(self._preset_list)
        row = QHBoxLayout()
        row.setSpacing(4)
        b_new = self._btn("新建", "")
        b_copy = self._btn("复制", "")
        b_del = self._btn("删除", "")
        b_new.clicked.connect(self._new_preset)
        b_copy.clicked.connect(self._copy_preset)
        b_del.clicked.connect(self._del_preset)
        for b in (b_new, b_copy, b_del):
            row.addWidget(b)
        lay.addLayout(row)
        brow = QHBoxLayout()
        brow.setSpacing(4)
        self._bind_btn = self._btn("绑定到选中按钮", "")
        b_unbind = self._btn("取消绑定", "")
        self._bind_btn.clicked.connect(self._bind)
        b_unbind.clicked.connect(self._unbind)
        brow.addWidget(self._bind_btn, 1)
        brow.addWidget(b_unbind)
        lay.addLayout(brow)
        parent.addWidget(card)

    def _build_property_section(self, parent: QVBoxLayout) -> None:
        card = QFrame()
        card.setStyleSheet("QFrame { background: transparent; border-radius: 10px;"
                           " border: none; }")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        h = QLabel("属性编辑")
        h.setStyleSheet("color: #7fd1ff; font-weight: bold; font-size: 13px;")
        lay.addWidget(h)

        # name
        nrow = QHBoxLayout()
        nrow.addWidget(QLabel("名称"))
        self._name = QLineEdit()
        self._name.setPlaceholderText("输入新预设名称（留空则自动命名）")
        self._name.editingFinished.connect(self._on_name_changed)
        self._name.textChanged.connect(lambda _t: setattr(self, "_preset_name_draft",
                                                          self._name.text().strip()))
        nrow.addWidget(self._name, 1)
        lay.addLayout(nrow)

        # geometry sliders — three side-by-side columns (saves two rows vs.
        # stacking them vertically now that the section is full-width).
        sliders_row = QHBoxLayout()
        sliders_row.setSpacing(10)
        for lbl, rng, attr in (("切角", (0, 30), "_cut"),
                               ("圆角", (0, 15), "_corner"),
                               ("边框", (0, 6), "_border")):
            col = QVBoxLayout()
            col.setSpacing(2)
            hrow = QHBoxLayout()
            hrow.setContentsMargins(0, 0, 0, 0)
            lab = QLabel(lbl)
            lab.setStyleSheet("color: #b8c2d0; font-size: 10px;")
            lab.setToolTip(_SLIDER_HELP.get(lbl, ""))
            val = QLabel("0")
            val.setStyleSheet("color: #dfe6f0; font-size: 11px;")
            val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            hrow.addWidget(lab)
            hrow.addStretch(1)
            hrow.addWidget(val)
            col.addLayout(hrow)
            sl = _NoWheelSlider(Qt.Orientation.Horizontal)
            sl.setRange(rng[0], rng[1])
            sl.valueChanged.connect(self._on_geo_changed)
            setattr(self, attr + "_slider", sl)
            sl.value_label = val  # type: ignore[attr-defined]
            col.addWidget(sl)
            sliders_row.addLayout(col, 1)
        lay.addLayout(sliders_row)

        # checkboxes
        crow = QHBoxLayout()
        self._chk_link = QCheckBox("链接主题")
        self._chk_hob = QCheckBox("仅悬停边框")
        self._chk_hg = QCheckBox("悬停渐变")
        self._chk_link.setToolTip("让按钮颜色跟随当前全局主题自动派生（忽略手填色）")
        self._chk_hob.setToolTip("勾选后：边框只在鼠标悬停时显示，静止时不画边框")
        self._chk_hg.setToolTip("勾选后：悬停时『渐变顶/渐变底』取代『悬停』填充色")
        for c in (self._chk_link, self._chk_hob, self._chk_hg):
            c.toggled.connect(self._on_geo_changed)
            crow.addWidget(c)
        crow.addStretch(1)
        lay.addLayout(crow)

        # 颜色色块：按按钮状态分组（常规态 / 悬停态 / 开关态），每组一行，
        # 颜色按按钮状态分组：每个分组标题单独一行（左侧小字），下方一排色块
        # 横排铺开。这样在 282px 宽的面板里也不会出现横向溢出（旧版是「标题
        # 在左 + 一排色块」横向排布，110px 标题 + 5 个 46px 色块 ≈ 370px 远超
        # 面板宽度，必然触发横向滚动条）。
        self._swatches: dict[str, QFrame] = {}
        for gname, items in _COLOR_GROUPS:
            gbox = QVBoxLayout()
            gbox.setSpacing(3)
            glab = QLabel(gname)
            glab.setStyleSheet("color: #8fd4ff; font-size: 10px; font-weight: bold;")
            if gname in _CENTERED_GROUPS:
                glab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            gbox.addWidget(glab)
            srow = QHBoxLayout()
            srow.setSpacing(4)
            for key, label in items:
                col = QVBoxLayout()
                col.setSpacing(1)
                sw = QFrame()
                sw.setFixedSize(40, 22)
                sw.setStyleSheet("QFrame { background: #555; border: none;"
                                 " border-radius: 4px; }")
                sw.setCursor(Qt.CursorShape.PointingHandCursor)
                sw.mousePressEvent = lambda _e, k=key: self._pick_color(k)  # type: ignore[assignment]
                sw.setToolTip(_COLOR_HELP.get(key, ""))
                self._swatches[key] = sw
                lab = QLabel(label)
                lab.setStyleSheet("color: #b8c2d0; font-size: 8px;")
                lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
                col.addWidget(sw)
                col.addWidget(lab)
                srow.addLayout(col)
            if gname in _CENTERED_GROUPS:
                srow.addStretch(1)
                srow.insertStretch(0, 1)  # 两端留白 → 居中
            else:
                srow.addStretch(1)
            gbox.addLayout(srow)
            lay.addLayout(gbox)

        # bottom action buttons
        brow = QHBoxLayout()
        brow.setSpacing(4)
        b1 = self._btn("保存为定制预设", "")
        b2 = self._btn("覆盖当前预设", "")
        b3 = self._btn("保存当前分配", "")
        b4 = self._btn("应用并刷新画布", "")
        b1.clicked.connect(self._save_as_preset)
        b2.clicked.connect(self._overwrite_preset)
        b3.clicked.connect(self._commit_style)
        b4.clicked.connect(self._apply_and_refresh)
        b1.setToolTip("把当前属性存为新 L1 预设")
        b2.setToolTip("写回列表选中的 L1 预设")
        b3.setToolTip("将当前按钮→预设绑定保存为整体分配方案（L2）")
        b4.setToolTip("把主题的预设指派应用到脚本主界面按钮")
        brow.addWidget(b1)
        brow.addWidget(b2)
        brow2 = QHBoxLayout()
        brow2.setSpacing(4)
        brow2.addWidget(b3)
        brow2.addWidget(b4)
        lay.addLayout(brow)
        lay.addLayout(brow2)
        parent.addWidget(card)

    # -- 焦点蒙层 (选中高亮) -------------------------------------------
    def _build_focus_section(self, parent: QVBoxLayout) -> None:
        """选中可交互元素时显示的边框 + 半透明蒙皮。

        颜色同时作用于边框与蒙皮；『蒙皮透明度』调到 0 即只剩外框（满足
        『只想要外边有个框』的需求）。配置写入全局 AppConfig，并实时同步到
        运行中的焦点矩形。"""
        card = QFrame()
        card.setStyleSheet("QFrame { background: transparent; border-radius: 10px;"
                           " border: none; }")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        h = QLabel("焦点蒙层（选中高亮）")
        h.setStyleSheet("color: #7fd1ff; font-weight: bold; font-size: 13px;")
        lay.addWidget(h)
        note = QLabel("聚焦可交互元素时的边框 + 半透明蒙皮。把『蒙皮透明度』调到 0 "
                      "即只剩外框；颜色同时作用于边框与蒙皮。")
        note.setWordWrap(True)
        note.setStyleSheet("color: #8b97aa; font-size: 9px;")
        lay.addWidget(note)

        # 颜色行：色块（可点）+ 十六进制文本框
        row_color = QHBoxLayout()
        row_color.setSpacing(6)
        lab_c = QLabel("蒙层颜色")
        lab_c.setStyleSheet("color: #b8c2d0; font-size: 10px;")
        row_color.addWidget(lab_c)
        self._focus_color_sw = QPushButton()
        self._focus_color_sw.setFixedSize(30, 20)
        self._focus_color_sw.setCursor(Qt.CursorShape.PointingHandCursor)
        self._focus_color_sw.clicked.connect(self._pick_focus_color)
        row_color.addWidget(self._focus_color_sw)
        self._focus_color_hex = QLineEdit()
        self._focus_color_hex.setMaximumWidth(80)
        self._focus_color_hex.setStyleSheet(
            "QLineEdit { background: #0e1117; color: #dfe6f0;"
            " border: 1px solid rgba(255,255,255,0.10);"
            " border-radius: 4px; padding: 3px 6px; }")
        self._focus_color_hex.textChanged.connect(self._on_focus_color_text)
        self._focus_color_hex.editingFinished.connect(self._save_focus_config)
        row_color.addWidget(self._focus_color_hex)
        row_color.addStretch(1)
        lay.addLayout(row_color)

        # 透明度行：滑块 + 百分比
        row_op = QHBoxLayout()
        row_op.setSpacing(6)
        lab_o = QLabel("蒙皮透明度")
        lab_o.setStyleSheet("color: #b8c2d0; font-size: 10px;")
        row_op.addWidget(lab_o)
        self._focus_op_slider = _NoWheelSlider(Qt.Orientation.Horizontal)
        self._focus_op_slider.setRange(0, 100)
        self._focus_op_slider.valueChanged.connect(self._on_focus_opacity)
        self._focus_op_slider.sliderReleased.connect(self._save_focus_config)
        row_op.addWidget(self._focus_op_slider, 1)
        self._focus_op_val = QLabel("25%")
        self._focus_op_val.setStyleSheet(
            "color: #dfe6f0; font-size: 11px; min-width: 36px;")
        row_op.addWidget(self._focus_op_val)
        lay.addLayout(row_op)

        parent.addWidget(card)
        self._load_focus_section()

    def _focus_controller(self) -> Any:
        """Reach the running InteractionController (may be None in headless)."""
        win = getattr(self._app, "_window", None)
        return getattr(win, "_controller", None) if win else None

    def _load_focus_section(self) -> None:
        cfg = self._app.config_manager.config
        hexv = (getattr(cfg, "focus_color", "e6ff1e") or "e6ff1e")[:6]
        if len(hexv) != 6:
            hexv = "e6ff1e"
        # 注意：透明度合法值包含 0，而 0 是假值，不能用 `or 25` 兜底（会把已
        # 保存的 0 误当成“缺失”吃掉，导致编辑器一打开滑块跳回默认 25）。
        # _coerce_focus_opacity 只对真正缺失 / None / 空串回退默认。
        op = _coerce_focus_opacity(getattr(cfg, "focus_opacity", None))
        # Block signals while populating: setText()/setValue() would otherwise
        # fire textChanged/valueChanged -> _apply_focus_live(), which re-reads
        # the *other* control (still at its default at this point) and clobbers
        # the just-loaded value back to the default. That bug made the saved
        # opacity revert to 25 on every re-open ("焦点蒙层无法保存").
        self._focus_color_hex.blockSignals(True)
        self._focus_op_slider.blockSignals(True)
        try:
            self._focus_color_hex.setText(hexv)
            self._focus_color_sw.setStyleSheet(
                f"QPushButton {{ background: #{hexv}; border: 1px solid"
                f" rgba(255,255,255,0.30); border-radius: 4px; }}")
            self._focus_op_slider.setValue(op)
            self._focus_op_val.setText(f"{op}%")
        finally:
            self._focus_color_hex.blockSignals(False)
            self._focus_op_slider.blockSignals(False)
        # 初始化缓存为当前已保存值：用户未改过时落盘写回原值（无害）；
        # 用户改过后由 _apply_focus_live 用信号值覆盖，不再依赖滑块状态。
        self._pending_focus_opacity = op
        self._pending_focus_color = hexv if len(hexv) == 6 else None

    def _pick_focus_color(self) -> None:
        cur = self._focus_color_hex.text().strip() or "e6ff1e"
        init = QColor("#" + cur) if len(cur) == 6 else QColor(230, 255, 30)
        col = QColorDialog.getColor(init, self, "焦点蒙层颜色")
        if col.isValid():
            hexv = f"{col.red():02x}{col.green():02x}{col.blue():02x}"
            self._focus_color_hex.setText(hexv)
            self._apply_focus_live()
            self._save_focus_config()

    def _on_focus_color_text(self, _text: str) -> None:
        hexv = (self._focus_color_hex.text().strip() or "e6ff1e")[:6]
        if len(hexv) == 6:
            self._focus_color_sw.setStyleSheet(
                f"QPushButton {{ background: #{hexv}; border: 1px solid"
                f" rgba(255,255,255,0.30); border-radius: 4px; }}")
            self._apply_focus_live()

    def _on_focus_opacity(self, val: int) -> None:
        # 用信号携带的新值，而不是重读 slider.value()——后者在拖动首帧或
        # 控件被 refresh 重置时可能仍是旧值/默认值 25（透明度重置的根因之一）。
        self._focus_op_val.setText(f"{val}%")
        self._apply_focus_live(opacity=val)

    def _apply_focus_live(self, opacity: int | None = None) -> None:
        """Write focus colour/opacity to the live controller (no disk save).

        ``opacity`` (when given) is the value carried by the slider's
        valueChanged signal — use it directly instead of re-reading
        ``slider.value()``, which may still hold the *previous* value when this
        slot runs (first frame of a drag) or can be reset to the default by a
        later refresh().  When ``opacity`` is None (the colour-edit paths),
        fall back to reading the slider.  The effective opacity is cached in
        ``self._pending_focus_opacity`` so _save_focus_config can flush it
        without depending on the control's current state."""
        raw = self._focus_color_hex.text().strip()
        hexv = raw[:6] if len(raw) == 6 else ""
        # P0-1 fix: validate hex legality, not just length.  A 6-char-but-
        # non-hex string (e.g. "gghhii") used to pass the length check, get
        # written to cfg.focus_color, persist to layout.json, and crash the
        # entire app on next startup (build_focus_style → int("gg", 16)).
        # Now: invalid input is silently ignored (cfg keeps its previous valid
        # value); the text box still shows what the user typed so they can fix
        # it, but no bad value reaches disk.
        valid_color = len(hexv) == 6 and all(c in "0123456789abcdefABCDEF" for c in hexv)
        if opacity is None:
            opacity = int(self._focus_op_slider.value())
        op = max(0, min(100, int(opacity)))
        cfg = self._app.config_manager.config
        if valid_color:
            cfg.focus_color = hexv
        cfg.focus_opacity = op
        # 缓存最后用户设置值：落盘（_save_focus_config）时直接用，不重读控件，
        # 彻底摆脱“关闭时控件被重置读回 25”的隐患。
        self._pending_focus_opacity = op
        if valid_color:
            self._pending_focus_color = hexv
        ctrl = self._focus_controller()
        if ctrl is not None and hasattr(ctrl, "set_focus_style"):
            ctrl.set_focus_style(hexv if valid_color else cfg.focus_color, op)
        # 防抖落盘：400ms 内无新改动才写盘（见 __init__ 中的 _focus_save_timer）。
        # 覆盖鼠标拖拽 / 键盘微调 / 颜色编辑等所有交互路径，避免依赖单一触发点。
        self._focus_save_timer.start()

    def _save_focus_config(self) -> None:
        # 用“最后用户设置值”兜底写盘：关闭 / 隐藏 / 松手 / 防抖触发时控件可能
        # 已被重置（或被 refresh 回填），重读会得到默认 25——这是之前透明度
        # 总是重置的根因。pending 在 _load_focus_section 初始化为已保存值，
        # 用户改过后由 _apply_focus_live 用信号值覆盖，故此处必然是最新值。
        cfg = self._app.config_manager.config
        if self._pending_focus_opacity is not None:
            cfg.focus_opacity = int(self._pending_focus_opacity)
        if self._pending_focus_color is not None:
            cfg.focus_color = str(self._pending_focus_color)
        try:
            self._app.save_all()
        except Exception:
            pass

    # -- refresh --------------------------------------------------------
    def refresh(self) -> None:
        """Sync the theme combo with the registry, load the active theme's
        saved preset assignments (so the canvas reflects the current state),
        and re-render."""
        self._style_cb.blockSignals(True)
        self._style_cb.clear()
        self._style_cb.addItems(list(BUTTON_STYLES.keys()))
        idx = self._style_cb.findText(_BS.ACTIVE_STYLE_NAME)
        if idx >= 0:
            self._style_cb.setCurrentIndex(idx)
        self._style_cb.blockSignals(False)
        # Load the active theme's saved assignments into the in-memory map.
        BUTTON_PRESET_MAP.clear()
        BUTTON_PRESET_MAP.update(BUTTON_STYLES.get(_BS.ACTIVE_STYLE_NAME, {}))
        self._refresh_preset_list()
        # Restore the preview if a L1 preset was already selected.
        if self._sel_preset and self._sel_preset in PER_BUTTON_PRESETS:
            self._load_preset(self._sel_preset)
        self._load_focus_section()
        # 重新进入编辑器时恢复上次选中的按钮（持久化在 BUTTON_SELECTION）。
        self._sel_keys = set(BUTTON_SELECTION)
        self.render_canvas()
        # 进入按钮风格编辑器时，自动弹出左侧按钮列表浮窗（复刻 FAQ 浮窗的
        # dock 左侧 / 从边缘滑出；编辑器被切走或隐藏时由 hideEvent 自动关闭）。
        self._list_popup.open()
        self._update_bind_label()

    # -- 自动关闭左侧按钮列表浮窗 --------------------------------------
    def hideEvent(self, ev: object) -> None:  # noqa: N802
        # 离开按钮风格编辑器（切到别的子页面 / 整体隐藏 / 退应用）时，把内存中
        # 已实时写好的焦点蒙层参数（focus_color / focus_opacity）强制落盘一次。
        # 否则仅靠 sliderReleased / editingFinished 触发保存：键盘调滑块不会触发
        # sliderReleased、关闭时焦点信号可能来不及触发、且 closeEvent 不调 save_all，
        # 导致焦点蒙层“总是重置”。内存值由 _apply_focus_live 在拖动/输入时已更新，
        # 这里只需 flush 即可覆盖所有交互路径（鼠标 / 键盘 / 直接退出）。
        try:
            self._save_focus_config()
        except Exception:
            pass
        # 编辑器被切到别的子页面或整体隐藏时，按钮列表浮窗一并收起
        # （与 FAQ 浮窗在切页时关闭的行为一致；详见 _on_page_switched）。
        if getattr(self, "_list_popup", None) is not None:
            self._list_popup.close_popup()
        super().hideEvent(ev)  # type: ignore[call-arg]

    # -- button collection ---------------------------------------------
    def _collect_groups(self) -> "OrderedDict[str, list[tuple[str, QWidget]]]":
        """Collect live HUD buttons, excluding the edit overlay AND the
        floating button-list popup.

        Mirrors the PS1 ``RenderCanvas`` which walks the live cards only — the
        editor's own controls must never appear in its preview list.  We pass
        ``self._overlay`` (the EditOverlay) as the exclude root so every editor
        panel (layout / tile / procmon / button) is skipped, and we also pass
        ``self._list_popup`` so the preview clones it hosts are never mistaken
        for real HUD buttons (which would shift indices and break selection /
        binding stability).
        """
        win = getattr(self._app, "_window", None)
        if win is None:
            return OrderedDict()
        popup = getattr(self, "_list_popup", None)
        # The standalone "快捷按钮" editor (ShortcutEditor) is parented to the
        # main window, so its own QPushButton controls (录制/清除/删除/关闭/添加)
        # would otherwise leak into this preview list.  Drop them.
        # 关键修复：_shortcut_editor 实际挂在 AppWindow（self._app._window）上，
        # 而非 App 控制器（self._app）。原代码从 self._app 读取永远为 None，导致
        # extra_excludes 恒为 None、快捷键编辑器按钮一直漏进列表「其他」分组。
        # 必须从 win（= AppWindow）读取才正确。
        shortcut_ed = getattr(win, "_shortcut_editor", None)
        return collect_button_groups(
            win, exclude=self._overlay, popup=popup,
            extra_excludes=[shortcut_ed] if shortcut_ed else None)

    # -- canvas ---------------------------------------------------------
    def render_canvas(self) -> None:
        # Detach + delete every existing clone FIRST, so re-rendering can never
        # let stale clones accumulate (the "覆盖当前预设 → 列表越来越多" report).
        # setParent(None) detaches the widget from the canvas immediately —
        # deleteLater() alone only runs on the next event loop tick, which left
        # a brief window where an old + new clone of the same button coexisted.
        while self._canvas_lay.count():
            item = self._canvas_lay.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            elif item is not None:
                item.deleteLater()
        groups = self._collect_groups()
        self._visible_keys = []
        for bt, items in groups.items():
            # Filter: ① skip tile blocks entirely  ② freeze block: keep only
            # one visible candidate (❄) and one visible frozen (🔥) as
            # representatives.
            if bt.startswith("tile"):
                continue
            filtered = list(items)
            if bt == "freeze":
                keep: list[tuple[str, QWidget]] = []
                found_cand = False
                found_froz = False
                for key, widget in items:
                    if isinstance(widget, QPushButton):
                        t = widget.text()
                        if t.startswith("❄") and not found_cand:
                            keep.append((key, widget))
                            found_cand = True
                        elif t.startswith("🔥") and not found_froz:
                            keep.append((key, widget))
                            found_froz = True
                    if found_cand and found_froz:
                        break
                filtered = keep
            if not filtered:
                continue
            hdr = QLabel(f">> {_BLOCK_LABELS.get(bt, bt)} ({len(filtered)})")
            hdr.setStyleSheet("color: #8fd4ff; font-weight: bold; font-size: 11px;"
                              " border: none; background: transparent;")
            # Force the header onto its own line (FlowLayout wraps after it).
            hdr.setProperty("flowBreak", True)
            self._canvas_lay.addWidget(hdr)
            for key, btn in filtered:
                is_sel = (key in self._sel_keys)
                self._visible_keys.append(key)
                self._canvas_lay.addWidget(
                    self._make_preview_item(key, btn, is_sel))
        # FlowLayout has no stretch item; rows wrap automatically.

    def _make_preview_item(self, key: str, btn: QWidget, is_sel: bool = False) -> QWidget:
        """One canvas row: the type-faithful *clone* of a live HUD button,
        wrapped in a :class:`_PreviewItem` that overlays the HUD focus frame
        when the item is selected.

        The clone keeps its original HUD style, size and interactivity (click →
        toggle selection).  Selection is indicated **only** by the outer focus
        frame — the clone's own border / corner is never touched, so picking a
        rounded/pill button never turns it into a rectangle."""
        clone = self._clone_button(btn)
        cfg = self._app.config_manager.config
        fc = getattr(cfg, "focus_color", "e6ff1e")
        # 与 _load_focus_section 同理：保留合法 0 值（不能用 `or 25` 兜底），
        # 否则预览选中框在透明度=0 时会错误渲染成 25%。
        fo = _coerce_focus_opacity(getattr(cfg, "focus_opacity", None))
        item = _PreviewItem(key, clone, is_sel, fc, fo, parent=self._canvas)
        # Remember which source key this clone represents (used by the preview
        # list + tests to map a clone back to its live button).
        clone._src_key = key
        # Selection is driven by the clone's own click signal — no mouse-
        # transparent hack, so the clone's native hover / pressed states render
        # exactly as they do in the HUD.
        if isinstance(clone, (TileButton, QPushButton)):
            clone.clicked.connect(lambda _checked=False, k=key: self._toggle_button(k))
        else:
            clone.mousePressEvent = lambda _e, k=key: self._toggle_button(k)  # type: ignore[assignment]
        return item


    def _clone_button(self, btn: QWidget) -> QWidget:
        """Clone a live HUD button for the preview list, **faithfully** — same
        colours, geometry, icon and native shape.

        Selection styling is **not** applied here; it lives on the wrapping
        :class:`_PreviewItem` focus frame (see :meth:`_make_preview_item`), so a
        clone is never reshaped or recoloured and never turns into a rectangle
        when selected.  Clones stay fully interactive (own hover / pressed
        states, no mouse-transparency hack)."""
        if isinstance(btn, TileButton):
            clone: QWidget = TileButton()
            self._copy_rounded_state(btn, clone)  # type: ignore[arg-type]
            tclone = clone  # type: TileButton
            tclone.set_tile_name(btn._name_label.text())
            tclone.show_tile_name = btn.show_tile_name
            pix = getattr(btn, "_pixmap", None)
            if pix is not None and not pix.isNull():
                tclone._pixmap = pix.copy()
                tclone.update()
            # Use the source's geometry — no fixed override, so the clone sizes
            # exactly like the source (selection is shown by the wrapping frame,
            # never by touching this clone's own border).
        elif isinstance(btn, RoundedButton):
            clone = RoundedButton()
            self._copy_rounded_state(btn, clone)
        else:
            # Native QPushButton / HintButton：克隆成与源相同的类，并完整复制
            # 切角 / 圆角自绘所需的 _bs_* 状态，否则切角预设的按钮在预览列表里会
            # 丢失边框（自绘状态没传过去 → 退回原生绘制）。
            if isinstance(btn, HintButton):
                pclone: QPushButton = HintButton(btn.text())  # type: ignore[attr-defined]
            else:
                pclone = QPushButton(btn.text())  # type: ignore[attr-defined]
            pclone.setStyleSheet(btn.styleSheet())  # type: ignore[attr-defined]
            pclone.setFont(btn.font())
            # 复制切角 / 圆角自绘状态（HintButton 专用属性；普通 QPushButton 忽略）。
            for _a in ("_bs_back", "_bs_hover", "_bs_border", "_bs_fore",
                       "_bs_bt", "_cut_size", "_corner_radius", "_hovered"):
                if hasattr(btn, _a):
                    setattr(pclone, _a, getattr(btn, _a))
            clone = pclone
        # Make the preview clone render at the live HUD button's exact on-screen
        # size (requirement: width/height must match the real buttons).
        self._copy_size(btn, clone)
        return clone

    @staticmethod
    def _copy_rounded_state(src: RoundedButton, dst: RoundedButton) -> None:
        """Copy every paint-relevant attribute so the clone renders pixel-
        identically to the source (no Color↔QColor round-trip loss)."""
        for attr in ("_back", "_hover", "_border", "_fore", "_hover_fore",
                     "_normal_back", "_grad_top", "_grad_bot",
                     "_active_back", "_active_border",
                     "_inactive_back", "_inactive_border"):
            v = getattr(src, attr, None)
            if isinstance(v, QColor):
                setattr(dst, attr, QColor(v))
        for attr in ("corner_radius", "cut_size", "border_thickness",
                     "hover_only_border", "hover_gradient", "glow_width",
                     "_is_toggle", "_active"):
            if hasattr(src, attr):
                setattr(dst, attr, getattr(src, attr))
        dst.update()

    @staticmethod
    def _copy_size(src: QWidget, dst: QWidget) -> None:
        """让预览克隆的尺寸与源按钮的**尺寸意图**一致，而非盲目复制布局后的几何。

        修复（2026-08-21，按钮列表部分按钮宽度过宽）：HUD 里部分文本按钮
        **没有显式宽度约束**（``minimumWidth()==0`` 且 ``maximumWidth()==
        QWIDGETSIZE_MAX``），宽度完全由卡片布局的 stretch / 等分拉伸决定——
        例如风扇卡片「应用」、降压控制卡片「自动降压」、SMT 卡片「热核视图」，
        HUD 里被拉成 ~392px 占满整行的「全宽按钮」。旧逻辑直接
        ``setFixedSize(src.width(), src.height())`` 把拉伸后的几何(392px)硬拷给
        预览克隆，按钮列表里就出现占满整行的巨宽按钮，比按钮实际应有的内容宽度
        宽得多。

        修复：对文本按钮（HintButton / QPushButton）只复制**显式宽度约束**
        （fixedWidth / minimumWidth / maximumWidth 任一被固定过）；无显式约束时
        不设宽度，克隆由 FlowLayout 按内容自然宽度（sizeHint）分配。自绘按钮
        （RoundedButton / TileButton）没有可靠的内容 sizeHint，保持复制布局几何。

        * Qt 对尚未布局的子控件报告 640x480：检测该未布局默认值后只复制尺寸
          *约束*（min/max/policy），避免克隆膨胀成 640x480。
        * 高度优先复制显式高度约束（fixedHeight），否则取布局几何高度——垂直
          方向一般不受水平 stretch 影响，所见即所得。
        """
        w, h = src.width(), src.height()
        if w <= 0 or h <= 0 or (w >= 600 and h >= 400):
            dst.setMinimumSize(src.minimumSize())
            dst.setMaximumSize(src.maximumSize())
            dst.setSizePolicy(src.sizePolicy())
            return
        if isinstance(src, (HintButton, QPushButton)):
            # 文本按钮：宽度只复制显式约束；无约束（布局 stretch 拉宽）时
            # 不设 min/maxWidth，克隆由 FlowLayout 按内容自然宽度分配。
            if src.minimumWidth() > 0 or src.maximumWidth() < 16777215:
                dst.setMinimumWidth(src.minimumWidth())
                dst.setMaximumWidth(src.maximumWidth())
            if src.minimumHeight() > 0 and src.minimumHeight() == src.maximumHeight():
                dst.setFixedHeight(src.minimumHeight())
            else:
                dst.setFixedHeight(h)
            dst.setSizePolicy(src.sizePolicy())
            return
        # 自绘按钮：复制布局几何（像素忠实）。
        dst.setFixedSize(w, h)

    def _sync_selection(self) -> None:
        """Persist the current selection set so it survives editor close/reopen."""
        BUTTON_SELECTION.clear()
        BUTTON_SELECTION.update(self._sel_keys)
        self._persist()

    def _toggle_button(self, key: str) -> None:
        """Toggle ``key`` in/out of the multi-selection set (click-to-toggle)."""
        if key in self._sel_keys:
            self._sel_keys.discard(key)
        else:
            self._sel_keys.add(key)
        self._update_bind_label()
        self.render_canvas()
        self._sync_selection()
        if self._sel_keys:
            parts = []
            for k in sorted(self._sel_keys):
                b = BUTTON_PRESET_MAP.get(k)
                parts.append(f"{k}→{b}" if b else k)
            self._status.setText(f"已选 {len(self._sel_keys)} 个: " + ", ".join(parts))
        else:
            self._status.setText("点选按钮后可绑定预设")

    def _select_all(self) -> None:
        """Select every button currently visible on the canvas."""
        self._sel_keys.update(self._visible_keys)
        self._update_bind_label()
        self.render_canvas()
        self._sync_selection()
        self._status.setText(f"已全选 {len(self._sel_keys)} 个按钮")

    def _clear_selection(self) -> None:
        self._sel_keys.clear()
        self._update_bind_label()
        self.render_canvas()
        self._sync_selection()
        self._status.setText("已清空选择")

    def _update_bind_label(self) -> None:
        """Reflect the selection count on the 绑定 button (and disable it when
        nothing is selected)."""
        btn = getattr(self, "_bind_btn", None)
        if btn is None:
            return
        n = len(self._sel_keys)
        if n == 0:
            btn.setText("绑定到选中按钮")
            btn.setEnabled(False)
        else:
            btn.setText(f"绑定到选中按钮 ({n})")
            btn.setEnabled(True)

    # -- style (L2) handlers -------------------------------------------
    def _on_style_changed(self, name: str) -> None:
        """Switch to a different theme — load its saved preset assignments."""
        if name not in BUTTON_STYLES:
            return
        _BS.ACTIVE_STYLE_NAME = name
        BUTTON_PRESET_MAP.clear()
        BUTTON_PRESET_MAP.update(BUTTON_STYLES[name])
        self.render_canvas()

    def _new_style(self) -> None:
        """Create an empty theme (no preset assignments yet)."""
        n = 1
        while f"自定义{n}" in BUTTON_STYLES:
            n += 1
        name = f"自定义{n}"
        BUTTON_STYLES[name] = {}
        self._style_cb.blockSignals(True)
        self._style_cb.addItem(name)
        self._style_cb.setCurrentIndex(self._style_cb.count() - 1)
        self._style_cb.blockSignals(False)
        # 落盘：否则重启后新建主题丢失
        self._persist()

    def _copy_style(self) -> None:
        """Deep-copy the selected theme's assignment map."""
        name = self._style_cb.currentText()
        src = BUTTON_STYLES.get(name)
        if src is None:
            return
        nn = f"{name} (复制)"
        BUTTON_STYLES[nn] = dict(src)
        self._style_cb.blockSignals(True)
        self._style_cb.addItem(nn)
        self._style_cb.setCurrentIndex(self._style_cb.count() - 1)
        self._style_cb.blockSignals(False)
        # 落盘：否则重启后复制主题丢失
        self._persist()

    def _del_style(self) -> None:
        """Delete the selected theme (keep at least one)."""
        if self._style_cb.count() <= 1:
            return
        name = self._style_cb.currentText()
        BUTTON_STYLES.pop(name, None)
        self._style_cb.blockSignals(True)
        self._style_cb.removeItem(self._style_cb.currentIndex())
        self._style_cb.blockSignals(False)
        new_name = self._style_cb.currentText()
        if _BS.ACTIVE_STYLE_NAME == name:
            _BS.ACTIVE_STYLE_NAME = new_name
        # 落盘：否则重启后删除的主题/激活主题丢失
        self._persist()

    def _apply_style(self) -> None:
        """Apply the selected theme to live HUD buttons (push preset assignments)."""
        name = self._style_cb.currentText()
        if not name:
            return
        _BS.ACTIVE_STYLE_NAME = name
        BUTTON_PRESET_MAP.clear()
        BUTTON_PRESET_MAP.update(BUTTON_STYLES.get(name, {}))
        # 落盘：记住当前激活主题（__Active__），否则重启后回退到默认主题
        self._persist()
        self._apply_to_live()
        self.render_canvas()
        self._status.setText(f"已应用主题: {name}")

    def _apply_to_live(self) -> None:
        """Collect live HUD buttons and push the current preset assignments."""
        groups = self._collect_groups()
        live: list = []
        key_map: dict[int, str] = {}
        for bt, items in groups.items():
            if bt == "freeze":
                continue
            for key, btn in items:
                live.append(btn)
                key_map[id(btn)] = key
        apply_styles_to_buttons(live, key_map)

    # -- preset (L1) handlers -------------------------------------------
    def _refresh_preset_list(self) -> None:
        self._preset_list.blockSignals(True)
        self._preset_list.clear()
        self._preset_list.addItems(list(PER_BUTTON_PRESETS.keys()))
        if self._sel_preset and self._sel_preset in PER_BUTTON_PRESETS:
            i = self._preset_list.findItems(self._sel_preset, Qt.MatchFlag.MatchExactly)
            if i:
                self._preset_list.setCurrentItem(i[0])
        self._preset_list.blockSignals(False)

    def _on_preset_selected(self, name: str) -> None:
        if name in PER_BUTTON_PRESETS:
            self._sel_preset = name
            self._load_preset(name)

    def _load_preset(self, name: str) -> None:
        st = PER_BUTTON_PRESETS.get(name)
        if st is None:
            return
        self._editor = {
            "cut_size": st.cut_size, "corner_radius": st.corner_radius,
            "border_thickness": st.border_thickness,
            "hover_only_border": st.hover_only_border, "hover_gradient": st.hover_gradient,
            "link_theme": st.link_theme,
            "colors": dict(st.colors),
        }
        self._dirty = False  # loaded fresh from the preset, not yet edited
        self._sync_widgets()
        self._apply_to_preview()

    def _current_style(self) -> "ButtonStyle":
        """Build the ButtonStyle actually applied by 绑定 / 应用并刷新画布.

        * If a preset is selected and the user has NOT edited the style since
          loading it (``_dirty`` is False), use the **preset's stored geometry**
          verbatim.  This guarantees a rounded preset is applied rounded even if
          the editor's corner_radius field somehow reads 0 (#3).
        * If the user has edited (``_dirty`` True) or no preset is selected,
          build from ``self._editor`` (WYSIWYG: the preview and the applied
          result always match).
        """
        if self._sel_preset and not self._dirty and self._sel_preset in PER_BUTTON_PRESETS:
            base = PER_BUTTON_PRESETS[self._sel_preset]
            return ButtonStyle(
                cut_size=base.cut_size, corner_radius=base.corner_radius,
                border_thickness=base.border_thickness,
                hover_only_border=base.hover_only_border, hover_gradient=base.hover_gradient,
                link_theme=base.link_theme, colors=dict(base.colors))
        e = self._editor
        return ButtonStyle(
            cut_size=e["cut_size"], corner_radius=e["corner_radius"],
            border_thickness=e["border_thickness"],
            hover_only_border=e["hover_only_border"], hover_gradient=e["hover_gradient"],
            link_theme=e["link_theme"], colors={k: v for k, v in e["colors"].items() if v},
        )

    def _new_preset(self) -> None:
        n = 1
        while f"预设#{n}" in PER_BUTTON_PRESETS:
            n += 1
        name = f"预设#{n}"
        self._save_preset(name)
        self._sel_preset = name
        self._refresh_preset_list()
        # 落盘：新建的 L1 预设必须写入 button-presets.json，否则仅为内存态、
        # 关闭/重启后丢失（即『无法保存按钮预设』的根因）。
        self._persist()

    def _copy_preset(self) -> None:
        if not self._sel_preset:
            return
        src = PER_BUTTON_PRESETS.get(self._sel_preset)
        if src is None:
            return
        nn = f"{self._sel_preset} (复制)"
        PER_BUTTON_PRESETS[nn] = type(src)(
            cut_size=src.cut_size, corner_radius=src.corner_radius,
            border_thickness=src.border_thickness, hover_only_border=src.hover_only_border,
            hover_gradient=src.hover_gradient, link_theme=src.link_theme,
            colors=dict(src.colors))
        self._sel_preset = nn
        self._refresh_preset_list()
        # 落盘：复制出的 L1 预设必须写入磁盘
        self._persist()

    def _del_preset(self) -> None:
        if not self._sel_preset:
            return
        PER_BUTTON_PRESETS.pop(self._sel_preset, None)
        self._sel_preset = None
        self._refresh_preset_list()
        # 落盘：删除的 L1 预设须同步从磁盘移除
        self._persist()

    def _bind(self) -> None:
        if not self._sel_keys:
            self._status.setText("请先在左侧点选按钮（可多选）")
            return
        if not self._sel_preset:
            self._status.setText("请先在右侧选择预设")
            return
        # WYSIWYG：以编辑器当前预览样式为准（已选预设且未改则直接用预设存储值），
        # 写回选中的预设并套到按钮，避免「绑定后显示直角、但预览是圆角」的错位。
        st = self._current_style()
        PER_BUTTON_PRESETS[self._sel_preset] = st
        for key in self._sel_keys:
            bind_button_preset(key, self._sel_preset)
            btn = self._live_button(key)
            if btn is not None:
                try:
                    _apply_style_to_ctrl(btn, st)
                except Exception:
                    pass
        # 落盘：把绑定写入当前主题并持久化，否则『保存当前分配』未点时、
        # 关闭/重启后绑定丢失（refresh() 会用当前主题重新覆盖内存映射）。
        self._persist_assignments()
        # 应用即「消费」多选：绑定后清空选中集，避免按钮一直挂着黄色高亮、
        # 重进编辑器仍选中（用户反馈：应用后还选中很困惑）。
        n = len(self._sel_keys)
        self._sel_keys.clear()
        self._sync_selection()
        self._update_bind_label()
        self._status.setText(f"已绑定 {n} 个按钮 → {self._sel_preset}")
        self.render_canvas()

    def _unbind(self) -> None:
        if not self._sel_keys:
            return
        for key in self._sel_keys:
            unbind_button_preset(key)
        # 落盘：同步把解绑结果写入当前主题
        self._persist_assignments()
        self._status.setText(f"已解绑 {len(self._sel_keys)} 个按钮")
        self.render_canvas()

    def _live_button(self, key: str) -> Optional[QWidget]:
        for _, items in self._collect_groups().items():
            for k, b in items:
                if k == key:
                    return b
        return None

    # -- property editing ----------------------------------------------
    def _sync_widgets(self) -> None:
        e = self._editor
        self._cut_slider.blockSignals(True)
        self._corner_slider.blockSignals(True)
        self._border_slider.blockSignals(True)
        self._cut_slider.setValue(e["cut_size"])
        self._corner_slider.setValue(e["corner_radius"])
        self._border_slider.setValue(e["border_thickness"])
        self._cut_slider.blockSignals(False)
        self._corner_slider.blockSignals(False)
        self._border_slider.blockSignals(False)
        self._cut_slider.value_label.setText(str(e["cut_size"]))  # type: ignore[attr-defined]
        self._corner_slider.value_label.setText(str(e["corner_radius"]))  # type: ignore[attr-defined]
        self._border_slider.value_label.setText(str(e["border_thickness"]))  # type: ignore[attr-defined]
        self._chk_link.setChecked(e["link_theme"])
        self._chk_hob.setChecked(e["hover_only_border"])
        self._chk_hg.setChecked(e["hover_gradient"])
        name = self._sel_preset or ""
        self._name.setText(name)
        self._preset_name_draft = name
        for key, sw in self._swatches.items():
            hexv = e["colors"].get(key, "")
            if hexv:
                sw.setStyleSheet(f"QFrame {{ background: #{hexv};"
                                 f" border: none; border-radius: 4px; }}")
            else:
                sw.setStyleSheet("QFrame { background: #555; border: none;"
                                 " border-radius: 4px; }")
        self._refresh_state_button_enabled()

    def _on_geo_changed(self) -> None:
        self._dirty = True
        self._editor["cut_size"] = self._cut_slider.value()
        self._editor["corner_radius"] = self._corner_slider.value()
        self._editor["border_thickness"] = self._border_slider.value()
        self._editor["link_theme"] = self._chk_link.isChecked()
        self._editor["hover_only_border"] = self._chk_hob.isChecked()
        self._editor["hover_gradient"] = self._chk_hg.isChecked()
        self._cut_slider.value_label.setText(str(self._editor["cut_size"]))  # type: ignore[attr-defined]
        self._corner_slider.value_label.setText(str(self._editor["corner_radius"]))  # type: ignore[attr-defined]
        self._border_slider.value_label.setText(str(self._editor["border_thickness"]))  # type: ignore[attr-defined]
        self._apply_to_preview()

    def _on_name_changed(self) -> None:
        # The 名称 field names the preset created by 保存为定制预设.
        self._preset_name_draft = self._name.text().strip()

    def _pick_color(self, key: str) -> None:
        cur = self._editor["colors"].get(key, "")
        dlg = QColorDialog()
        if cur:
            try:
                dlg.setCurrentColor(QColor("#" + cur))
            except Exception:
                pass
        if dlg.exec() == QColorDialog.DialogCode.Accepted:
            c = dlg.currentColor()
            hexv = _qcolor_to_hex(c)
            self._dirty = True
            self._editor["colors"][key] = hexv
            self._swatches[key].setStyleSheet(
                f"QFrame {{ background: #{hexv}; border: none;"
                f" border-radius: 4px; }}")
            self._apply_to_preview()
            self._refresh_state_button_enabled()

    def _apply_to_preview(self) -> None:
        e = self._editor
        cols = e["colors"]
        prev = self._preview
        # 性能：RoundedButton 的 set_colors / 各属性 setter 各自都会 update()，
        # 一次调用会触发十几次重绘。临时关掉预览按钮的更新，集中到末尾只重绘
        # 一次——这是拖动滑块时卡顿的主因。
        prev.setUpdatesEnabled(False)
        try:
            back = hex_to_color(cols.get("BackColor", "1c1e26"))
            hover = hex_to_color(cols.get("HoverColor", "2a2d38"))
            border_c = hex_to_color(cols.get("BorderColor", _MAUVE_FALLBACK))
            fore = hex_to_color(cols.get("ForeColor", "e6ff1e"))
            hover_fore = hex_to_color(cols.get("HoverForeColor", "ffffff"))
            self._preview.set_colors(back, hover, border_c, fore, hover_fore)
            # 文字由 RoundedButton 内部绘制，自动跟随 ForeColor / HoverForeColor，
            # 悬停态会切换到 HoverForeColor（见 paintEvent）。
            self._preview.cut_size = e["cut_size"]
            self._preview.corner_radius = e["corner_radius"]
            self._preview.border_thickness = e["border_thickness"]
            self._preview.hover_only_border = e["hover_only_border"]
            self._preview.hover_gradient = e["hover_gradient"]
            # 悬停渐变：顶/底都设置了才生效，避免只设一个时底色变成默认灰。
            if cols.get("GradientHoverTop") and cols.get("GradientHoverBottom"):
                self._preview.gradient_hover_top = hex_to_color(cols["GradientHoverTop"])
                self._preview.gradient_hover_bottom = hex_to_color(cols["GradientHoverBottom"])
            # 悬浮边框：仅悬停时由 RoundedButton 使用。
            if cols.get("HoverBorderColor"):
                self._preview.hover_border = hex_to_color(cols["HoverBorderColor"])
            ab = hex_to_color(cols["ActiveBackColor"]) if cols.get("ActiveBackColor") else None
            abb = hex_to_color(cols["ActiveBorderColor"]) if cols.get("ActiveBorderColor") else None
            ib = hex_to_color(cols["InactiveBackColor"]) if cols.get("InactiveBackColor") else None
            ibb = hex_to_color(cols["InactiveBorderColor"]) if cols.get("InactiveBorderColor") else None
            if ab is not None:
                self._preview.set_active_colors(ab, abb, ib, ibb)
        except Exception:
            pass
        # 把当前选中的预览状态（常规/悬停/激活/未激活）套用到预览按钮上，
        # 让对应的专属颜色组在预览里可见。
        self._apply_preview_state()
        prev.setUpdatesEnabled(True)

    # -- commit / persist ----------------------------------------------
    def _unique_preset_name(self, base: str) -> str:
        """Return ``base`` if free, else ``base#n`` until an unused name is found
        (so 保存为定制预设 never silently overwrites an existing preset)."""
        base = (base or "我的预设").strip() or "我的预设"
        if base not in PER_BUTTON_PRESETS:
            return base
        n = 1
        while f"{base}#{n}" in PER_BUTTON_PRESETS:
            n += 1
        return f"{base}#{n}"

    def _save_preset(self, name: str) -> None:
        PER_BUTTON_PRESETS[name] = ButtonStyle(
            cut_size=self._editor["cut_size"], corner_radius=self._editor["corner_radius"],
            border_thickness=self._editor["border_thickness"],
            hover_only_border=self._editor["hover_only_border"],
            hover_gradient=self._editor["hover_gradient"], link_theme=self._editor["link_theme"],
            colors={k: v for k, v in self._editor["colors"].items() if v})

    def _reapply_preset_everywhere(self, preset_name: str) -> None:
        """Re-push ``preset_name`` onto every live HUD button currently bound
        to it.  Without this, overwriting a preset saved the data but the UI
        never repainted — the '覆盖当前预设没作用' report."""
        for _, items in self._collect_groups().items():
            for key, btn in items:
                if BUTTON_PRESET_MAP.get(key) == preset_name:
                    try:
                        apply_per_button_preset(btn, preset_name)
                    except Exception:
                        pass

    def _overwrite_preset(self) -> None:
        if not self._sel_preset:
            # Previously this silently returned — the button looked dead.
            self._status.setText("请先在右侧列表选择要覆盖的预设")
            return
        # 若用户在「名称」里输入了与当前预设不同的名字，则视为「覆盖并改名」：
        # 删除旧键、写到新键，并把所有引用旧名的绑定映射一并迁到新名，否则
        # 只是改了列表显示、实际绑定的还是旧名（即『改了名字没生效』）。
        new_name = (getattr(self, "_preset_name_draft", "") or "").strip()
        if new_name and new_name != self._sel_preset:
            if new_name in PER_BUTTON_PRESETS:
                # 新名字已存在：避免静默覆盖，提示用户。
                self._status.setText(f"名称「{new_name}」已存在，请换一个")
                return
            PER_BUTTON_PRESETS.pop(self._sel_preset, None)
            self._save_preset(new_name)
            for k in list(BUTTON_PRESET_MAP.keys()):
                if BUTTON_PRESET_MAP[k] == self._sel_preset:
                    BUTTON_PRESET_MAP[k] = new_name
            style_map = BUTTON_STYLES.get(_BS.ACTIVE_STYLE_NAME, {})
            for k in list(style_map.keys()):
                if style_map[k] == self._sel_preset:
                    style_map[k] = new_name
            self._sel_preset = new_name
            self._name.setText(new_name)
            self._preset_name_draft = new_name
        else:
            self._save_preset(self._sel_preset)
        # 立即把新预设套回所有绑定它的按钮，让改动看得见（覆盖即重刷）。
        self._reapply_preset_everywhere(self._sel_preset)
        # 落盘：覆盖后的 L1 预设必须写回 button-presets.json
        self._persist()
        self._refresh_preset_list()
        self.render_canvas()
        self._status.setText(f"已覆盖预设: {self._sel_preset}（已重刷绑定按钮）")

    def _save_as_preset(self) -> None:
        """Save the *current editor* values as a brand-new L1 preset.

        No canvas-button selection is required — a preset is just a style
        definition.  Its name comes from the 名称 field if the user typed one,
        else from the selected button's label, else an auto name.  The name is
        made unique so it never clobbers an existing preset.  Previously this
        bailed out (silently) unless a canvas button was selected first, which
        is what made the button appear to 'do nothing'."""
        draft = (getattr(self, "_preset_name_draft", "") or "").strip()
        if draft:
            base = draft[:16]
        elif self._sel_keys:
            first = sorted(self._sel_keys)[0]
            btn = self._live_button(first)
            base = "Btn"
            if btn is not None:
                # RoundedButton exposes ``.name``; native QPushButton uses ``.text()``.
                if isinstance(btn, RoundedButton):
                    t = getattr(btn, "name", None) or ""
                elif hasattr(btn, "text") and callable(getattr(btn, "text")):
                    try:
                        t = btn.text() or ""
                    except Exception:
                        t = ""
                else:
                    t = ""
                if t:
                    base = str(t)[:8]
            else:
                base = "我的预设"
        else:
            base = "我的预设"
        name = self._unique_preset_name(base)
        self._save_preset(name)
        self._sel_preset = name
        self._preset_name_draft = name
        self._name.setText(name)
        self._refresh_preset_list()
        # 落盘：新建的 L1 预设必须写入 button-presets.json，否则仅为内存态、
        # 关闭/重启后丢失（即『无法保存按钮预设』的根因）。
        self._persist()
        self._status.setText(f"已存为预设: {name}")

    def _persist_assignments(self) -> None:
        """把当前 ``BUTTON_PRESET_MAP``（已绑定 / 解绑的结果）写入当前主题，
        并落盘。``refresh()`` 在每次打开编辑器时会用 ``BUTTON_STYLES[active]``
        重新覆盖内存映射，所以绑定必须提交进主题本身才能持久化。"""
        name = self._style_cb.currentText()
        if name not in BUTTON_STYLES:
            return
        BUTTON_STYLES[name] = dict(BUTTON_PRESET_MAP)
        _BS.ACTIVE_STYLE_NAME = name
        self._persist()

    def _commit_style(self) -> None:
        """Save the current button→preset assignments *into* the selected theme.

        If nothing has been bound yet, explain how to create a binding instead of
        silently writing an empty map (which looked like 'no effect')."""
        if not BUTTON_PRESET_MAP:
            self._status.setText("尚无绑定：先点选按钮 → 选预设 → 「绑定到选中按钮」")
        else:
            self._status.setText(f"已保存分配: {self._style_cb.currentText()}")
        self._persist_assignments()

    def _apply_and_refresh(self) -> None:
        """Apply the editor's *current preview style* onto every selected live
        button, then refresh the list.

        WYSIWYG 原则：预览里看到的是什么（圆角/直角、配色），应用到按钮就是什么。
        之前「应用到按钮」只套用『列表选中的预设』的**已存储数据**，而预设数据可能
        还是旧的（例如你在属性编辑里把圆角滑块拖大了，预览变圆角，却没点『覆盖当前
        预设』，于是预设里的 corner_radius 仍是 0）——结果就是『预览圆角、应用直角』
        的错位。这里直接以编辑器当前样式为准下发给选中按钮，从根本上消除预览与
        应用的不一致。

        若右侧选中了某预设：把当前预览样式写回该预设（保持 预览==预设 一致），并把
        该预设绑定到选中按钮，使绑定持久化、主题切换后依然生效。最后统一下发 +
        重绘按钮列表。"""
        if not self._sel_keys:
            self._status.setText("请先在左侧点选按钮（可多选）")
            return
        # 以编辑器当前预览样式为准构建 ButtonStyle（所见即所得）；已选预设且
        # 未手动改过时直接用预设存储值，确保圆角预设应用后仍是圆角（#3）。
        st = self._current_style()
        if self._sel_preset:
            # 把当前预览样式同步进选中的预设，保证预览==预设始终一致。
            PER_BUTTON_PRESETS[self._sel_preset] = st
        # 直接把预览样式套到每个选中的实时按钮上（不依赖预设旧数据）。
        for key in self._sel_keys:
            if self._sel_preset:
                bind_button_preset(key, self._sel_preset)
                # 已绑定预设 -> 清掉该键的直接样式，避免两者冲突。
                DIRECT_STYLES.pop(key, None)
            else:
                # 未选预设的直接应用：持久化到 DIRECT_STYLES，使 HUD 重建后
                # 仍生效（否则只改了内存里的实时按钮，rebuild 一重建就还原）。
                DIRECT_STYLES[key] = st
                unbind_button_preset(key)
            btn = self._live_button(key)
            if btn is not None:
                try:
                    _apply_style_to_ctrl(btn, st)
                except Exception:
                    pass
        n = len(self._sel_keys)
        # 把绑定同时写进当前主题（BUTTON_STYLES[active]）——「应用并刷新画布」
        # 之前只更新了内存 BUTTON_PRESET_MAP + 扁平 map 文件，没写进主题，导致
        # 关闭/重进编辑器时 refresh() 用主题覆盖内存映射、绑定丢失，HUD 重建后
        # 按钮退回直角（圆角预设白绑）。和「绑定」按钮一致，必须落盘到主题。
        self._persist_assignments()
        self._persist()
        self._apply_to_live()
        # 应用即「消费」多选：清空选中集（并清掉持久化的 BUTTON_SELECTION），
        # 让按钮不再挂着黄色高亮，重进编辑器也是干净状态。
        self._sel_keys.clear()
        self._sync_selection()
        self._update_bind_label()
        self.render_canvas()
        if self._sel_preset:
            self._status.setText(
                f"已应用并绑定 {n} 个按钮 → {self._sel_preset}")
        else:
            self._status.setText(f"已应用样式到 {n} 个按钮")

    def _persist(self) -> None:
        try:
            save_all_button_data(self._app.script_root)
        except Exception:
            pass

    # -- nav ------------------------------------------------------------
    def nav_items(self) -> list[NavItem]:
        items: list[NavItem] = []
        items.append(NavItem(self._back_btn, "action",
                             confirm=self._overlay.open_layout, hint="B"))
        items.append(NavItem(self._style_cb, "select",
                             confirm=lambda w=self._style_cb: (w.setFocus(), w.showPopup())))
        items.append(NavItem(self._preset_list, "select",
                             confirm=lambda w=self._preset_list: w.setFocus()))
        items.append(NavItem(self._cut_slider, "slider",
                             nudge=lambda d, sl=self._cut_slider: (
                                 sl.setValue(max(0, min(30, sl.value() + d))), True)[1]))
        items.append(NavItem(self._corner_slider, "slider",
                             nudge=lambda d, sl=self._corner_slider: (
                                 sl.setValue(max(0, min(20, sl.value() + d))), True)[1]))
        items.append(NavItem(self._border_slider, "slider",
                             nudge=lambda d, sl=self._border_slider: (
                                 sl.setValue(max(0, min(6, sl.value() + d))), True)[1]))
        for sw in self._swatches.values():
            items.append(NavItem(sw, "action", confirm=lambda w=sw: self._pick_color(
                next(k for k, v in self._swatches.items() if v is w))))
        return items


# ---------------------------------------------------------------- 按钮列表浮窗
class ButtonListPopup(QWidget):
    """紧贴主窗口一侧、从主窗口边缘向外滑出的按钮列表浮窗。

    复刻 ``faq_popup.FaqPopup`` 的 dock / 滑出 / 跟随窗口行为，但内容不是 FAQ
    文本，而是按钮风格编辑器的「按钮列表」：标题 + 关闭按钮、全选/清空工具栏、
    滚动区 + ``FlowLayout`` 画布（由 ``ButtonEditPanel.render_canvas()`` 渲染带
    黄色选中高亮的克隆按钮）。

    生命周期：
      * 进入按钮风格编辑器时由 ``ButtonEditPanel.refresh()`` 自动 ``open()``；
      * 编辑器被切到别的子页面或整体隐藏时由 ``ButtonEditPanel.hideEvent``
        自动 ``close_popup()``；
      * 主窗口移动时由 ``app_window.moveEvent`` 调 ``reposition()`` 跟随。
    """

    def __init__(self, parent_window: QWidget, panel: "ButtonEditPanel") -> None:
        super().__init__(parent_window)
        self._mw = parent_window
        self._panel = panel
        self._pw = 320
        self._side = "left"
        self._open = False
        self._anim: QPropertyAnimation | None = None
        self._final_rect: QRect | None = None
        self._main_rect: QRect | None = None
        self._sliver = 16
        self._accent = QColor(127, 209, 255)
        self._bg = QColor(22, 26, 34, 245)
        self._border_alpha = 210

        # 无框 Tool 窗口，以主窗口为父（从属窗口）：① 主窗口移动时自动跟随；
        # ② 切换虚拟桌面时同进退；③ 不出现在任务栏。dock 在主窗口外侧紧贴
        # （不重叠），绝不遮住主界面。
        # 关键修复（2026-08-18）：显式加 WindowStaysOnTopHint，保证弹窗稳定浮于
        # 主窗口之上（与已稳定的 PerfMonPopup 一致）；同时**不再用 setMask** 做「擦出」
        # 动画——frameless + 复杂子控件树（QScrollArea + FlowLayout）+ setMask 在
        # Windows DWM 真实渲染时会原生段错误（0xC0000005），表现为弹窗打不开 /
        # 程序崩溃。改用 windowOpacity 淡入淡出，保留圆角（WA_TranslucentBackground 自绘）。
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # 辅助浮窗不应触发 app 退出：Qt.Tool 即使有 parent，Qt 默认仍给它
        # WA_QuitOnClose=True；若主窗口已 hide 到托盘，关本弹窗会误触发
        # lastWindowClosed → app.quit()。显式关闭以根治此类"关浮窗=退程序"。
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setMinimumSize(0, 0)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)

        # 标题 + 关闭
        top = QHBoxLayout()
        self._title = QLabel("按钮列表")
        self._title.setStyleSheet("color:#9fb0c8;font-size:14px;font-weight:bold;")
        top.addWidget(self._title, 1)
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(26, 26)
        close_btn.setStyleSheet(
            "QPushButton{background:rgba(255,90,90,0.16);color:#ff9b9b;"
            "border:1px solid rgba(255,90,90,0.3);border-radius:6px;}"
            "QPushButton:hover{background:rgba(255,90,90,0.3);}")
        close_btn.clicked.connect(self.close_popup)
        top.addWidget(close_btn)
        outer.addLayout(top)

        # 多选工具栏（全选 / 清空）——点选按钮本身是「切换式」，这里提供批量入口。
        sel_bar = QHBoxLayout()
        sel_bar.setSpacing(4)
        b_all = panel._btn("全选", "")
        b_clear = panel._btn("清空", "")
        b_all.setToolTip("选中列表里所有可见按钮")
        b_clear.setToolTip("清空当前多选")
        b_all.clicked.connect(panel._select_all)
        b_clear.clicked.connect(panel._clear_selection)
        sel_bar.addWidget(b_all)
        sel_bar.addWidget(b_clear)
        sel_bar.addStretch(1)
        outer.addLayout(sel_bar)

        # 滚动区 + 画布（FlowLayout），由 panel.render_canvas() 填充
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea{border: none;background:transparent;}"
            "QScrollBar:vertical{width: 0px;background:transparent;}"
            "QScrollBar:horizontal{height: 0px;background:transparent;}"
            "QScrollBar::handle:vertical{background:rgba(150,165,190,0.30);"
            "border-radius:3px;}"
            "QScrollBar::handle:vertical:hover{background:rgba(150,165,190,0.50);}")
        canvas = QWidget()
        canvas_lay = FlowLayout(canvas, hspacing=6, vspacing=6)
        canvas_lay.setContentsMargins(0, 0, 0, 0)
        scroll.setWidget(canvas)
        outer.addWidget(scroll, 1)

        # 把画布交给 panel，使 render_canvas() 直接渲染到这里。
        panel._canvas = canvas
        panel._canvas_lay = canvas_lay
        panel._canvas_scroll = scroll

    # ---- 几何：按主窗口在屏幕的位置决定 dock 侧与滑入方向 ----
    def _dock_rect(self) -> QRect:
        gm = self._mw.geometry()
        screen = self._mw.screen().availableGeometry()
        ph = min(gm.height(), screen.height() - 8)
        pw = self._pw
        mw_cx = gm.x() + gm.width() / 2
        sc_cx = screen.x() + screen.width() / 2
        if mw_cx >= sc_cx:          # 主窗口在右半屏 -> dock 左侧，从主窗口左边缘向外伸出
            self._side = "left"
            dock_x = gm.x() - pw
        else:                       # 主窗口在左半屏 -> dock 右侧，从主窗口右边缘向外伸出
            self._side = "right"
            dock_x = gm.x() + gm.width()
        dock_x = max(screen.x(), min(dock_x, screen.x() + screen.width() - pw))
        return QRect(dock_x, gm.y(), pw, ph)

    # ---- 开 / 关 / 跟随 ----
    def open(self) -> None:
        if self._open:
            # 已是打开状态（如再次进入编辑器）：确保可见并重新跟随主窗口。
            self.reposition()
            self.show()
            return
        # 主题色：跟随呼吸灯颜色；整体固定深色系，和主界面保持一致。
        c = get_theme_accent()
        self._accent = QColor(c.r, c.g, c.b)
        self._final_rect = self._dock_rect()      # 同时设定 self._side
        self._main_rect = self._mw.geometry()
        self.setFixedSize(self._final_rect.width(), self._final_rect.height())
        self.setGeometry(self._final_rect)
        self.show()
        self._open = True
        # 从完全透明开始，向外淡入（轻量、不卡顿、不触发 DWM 崩溃）。
        self.setWindowOpacity(0.0)
        self._animate_reveal(True)

    def close_popup(self) -> None:
        if not self._open:
            return
        self._open = False
        self._animate_reveal(False)

    def reposition(self, instant: bool = True) -> None:
        if self._final_rect is None:
            return
        self._final_rect = self._dock_rect()
        self._main_rect = self._mw.geometry()
        self.setGeometry(self._final_rect)

    def is_open(self) -> bool:
        return self._open

    def _animate_reveal(self, show: bool) -> None:
        if self._anim is not None:
            self._anim.stop()
        # 直接动画 windowOpacity：frameless + 复杂子控件树下比 setMask 安全得多，
        # 彻底规避 Windows DWM 在「frameless + 复杂子控件树 + setMask」下的原生崩溃。
        self._anim = QPropertyAnimation(self, b"windowOpacity")
        self._anim.setDuration(150 if show else 120)
        self._anim.setEasingCurve(
            QEasingCurve.Type.OutCubic if show else QEasingCurve.Type.InCubic
        )
        self._anim.setStartValue(self.windowOpacity())
        self._anim.setEndValue(1.0 if show else 0.0)
        if not show:
            self._anim.finished.connect(self._on_hide_finished)
        self._anim.start()

    def _on_hide_finished(self) -> None:
        self.hide()

    # ---- 磨砂圆角面板 ----
    def paintEvent(self, ev: object) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        r = 12
        path = QPainterPath()
        path.addRoundedRect(1, 1, w - 2, h - 2, r, r)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._bg)
        p.drawPath(path)
        a = self._accent
        p.setPen(QColor(a.red(), a.green(), a.blue(), self._border_alpha))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.end()


# ---------------------------------------------------------------------------
# Preview selection focus frame
# ---------------------------------------------------------------------------
# Padding (px) between a preview clone and its selection focus frame, so the
# HUD-style frame sits visibly *around* the button (matches _FocusRect's pad).
FOCUS_PAD = 3


class _PreviewItem(QWidget):
    """A canvas row that hosts a faithful preview ``clone`` plus a
    mouse-transparent focus-frame overlay (the HUD's ``_FocusRect`` look) used
    to mark the row as *selected*.

    The clone is laid out with a small :data:`FOCUS_PAD` margin so the frame
    can sit visibly *around* the button — exactly like the HUD frames the
    focused control.  Selecting never touches the clone's own painting, so a
    rounded/pill button keeps its native shape: no rectangle artifact."""

    def __init__(self, key: str, clone: QWidget, is_sel: bool,
                 focus_color: str, focus_opacity: int,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._src_key = key
        self._clone = clone
        lay = QHBoxLayout(self)
        lay.setContentsMargins(FOCUS_PAD, FOCUS_PAD, FOCUS_PAD, FOCUS_PAD)
        lay.setSpacing(0)
        lay.addWidget(clone)
        self._frame = QFrame(self)
        self._frame.setStyleSheet(build_focus_style(focus_color, focus_opacity))
        # Mouse-transparent so every click / hover falls through to the clone
        # underneath (same trick as the HUD's _FocusRect).
        self._frame.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._frame.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._frame.raise_()
        self.set_sel(is_sel)

    def set_sel(self, sel: bool) -> None:
        """Show / hide the selection focus frame (does not touch the clone)."""
        self._frame.setVisible(bool(sel))

    def resizeEvent(self, ev: object) -> None:  # noqa: N802
        # Keep the frame covering the WHOLE item (clone + pad ring) regardless
        # of how the flow layout sizes us.
        self._frame.setGeometry(0, 0, self.width(), self.height())
        super().resizeEvent(ev)  # type: ignore[arg-type]
