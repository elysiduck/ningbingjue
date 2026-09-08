"""Gamepad ABXY hint badge — shared rendering + ``HintButton``.

Mirrors the original ``RoundedButton.GamepadHint``: a small coloured disc
(A绿 / B红 / X蓝 / Y黄) carrying the letter, drawn in a control's corner.  It is
used by **both** the HUD ``RoundedButton`` and the in-window editor's plain
buttons so *every* shortcut is shown in gamepad style — never as keyboard text.

A single palette lives here so the badge looks identical everywhere (HUD,
editor overlay, focus rectangle).
"""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QPushButton, QSizePolicy, QWidget

# Gamepad ABXY badge colours (A绿 / B红 / X蓝 / Y黄) — matches the original's
# RoundedButton::SetHintColor convention.
HINT_COLORS = {
    "A": QColor(130, 255, 30),
    "B": QColor(230, 75, 90),
    "X": QColor(95, 175, 235),
    "Y": QColor(245, 215, 75),
}

# Horizontal space reserved for an inline left-side badge.  Radius 9 + margin 5
# + 1px backing + a little breathing room → 24.  Controls that draw their own
# centred text should inset the text rect by this amount so the badge and label
# never overlap.
BADGE_WIDTH = 24

# 按按钮宽度分流：宽度达到此阈值即视为「长按钮」。长按钮的提示胶囊画在
# 左侧；短按钮画在顶部。
HINT_WIDE_THRESHOLD = 96

# HintButton 专用：低于此宽度的按钮视为「短按钮」，提示胶囊贴在按钮上沿
# （内部顶部，文字下移），而非左侧。比 HINT_WIDE_THRESHOLD 略大，确保
# 锁帧:关 / 自动锁帧:关 这类按钮稳定走顶部胶囊。
HINT_TOP_W = 112

# 左侧胶囊（长按钮）预留给文字的左内边距，避免胶囊压住文字。
LEFT_CAP_W = 26

# 右侧胶囊（长按钮）预留给文字的右内边距（Y 提示放在进程按钮右边时使用）。
RIGHT_CAP_W = 26

# 顶部胶囊尺寸（仅用于短按钮、且完全在按钮内部，不超出边界）
# 刻意做扁：短按钮自身高度只有 ~26px，胶囊太占高会压住下方文字，故内高压到 9。
CAPSULE_TOP_MARGIN = 1     # 胶囊贴按钮顶边（仅留 1px 缝隙）
CAPSULE_INNER_HEIGHT = 9   # 胶囊实际高度（扁胶囊，给文字让出更多竖向空间）

# 自绘按钮内部文字留白（避免文字贴边，看起来像被压扁）
BUTTON_PAD_X = 8
BUTTON_PAD_Y = 3


def draw_hint_badge(painter: QPainter, hint: str, w: int, h: int,
                    radius: int = 9, margin: int = 5,
                    placement: str = "left") -> None:
    """Back-compat shim: render the legacy left/top **disc** badge.

    New code should call :func:`draw_hint_auto` (capsule form) instead.  Kept so
    any stray caller does not break.
    """
    if hint not in HINT_COLORS:
        return
    r = radius
    if placement == "top":
        cx, cy = w // 2, r + margin
    else:  # left, vertical centre
        cx, cy = r + margin, h // 2
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(0, 0, 0, 150))
    painter.drawEllipse(cx - r - 1, cy - r - 1, 2 * (r + 1), 2 * (r + 1))
    painter.setBrush(HINT_COLORS[hint])
    painter.drawEllipse(cx - r, cy - r, 2 * r, 2 * r)
    f = painter.font()
    f.setBold(True)
    f.setPixelSize(16)  # 9pt→16px 放大
    f.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    painter.setFont(f)
    painter.setPen(QColor(12, 14, 20))
    painter.drawText(cx - r, cy - r, 2 * r, 2 * r,
                     Qt.AlignmentFlag.AlignCenter, hint)
    painter.restore()


def draw_top_capsule_badge(painter: QPainter, hint: str, w: int,
                           capsule_h: int = CAPSULE_INNER_HEIGHT,
                           top_margin: int = CAPSULE_TOP_MARGIN) -> None:
    """Paint a small ABXY pill at the **top-centre, fully inside** a control.

    The pill sits in a reserved top band so the control's text can be pushed
    below it without ever overlapping.  Used for compact/narrow buttons where a
    left-side disc would cover the first character.  Never drawn outside the
    control's own rect (so it is never clipped).
    """
    if hint not in HINT_COLORS:
        return
    pill_w = 22
    x = (w - pill_w) // 2
    y = top_margin
    radius = capsule_h // 2
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    # dark backing for contrast (slightly larger than the pill)
    painter.setBrush(QColor(0, 0, 0, 160))
    painter.drawRoundedRect(x - 1, y - 1, pill_w + 2, capsule_h + 2, radius + 1, radius + 1)
    # coloured pill
    painter.setBrush(HINT_COLORS[hint])
    painter.drawRoundedRect(x, y, pill_w, capsule_h, radius, radius)
    # letter — kept small because the pill is only CAPSULE_INNER_HEIGHT tall,
    # and drawn inside a slightly inset rect so it never touches the pill edge.
    f = painter.font()
    f.setBold(True)
    f.setPixelSize(12)  # 6pt→12px 放大（原过小几乎看不清）
    f.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    painter.setFont(f)
    painter.setPen(QColor(12, 14, 20))
    inset_x = 2
    inset_y = 1
    painter.drawText(x + inset_x, y + inset_y,
                     pill_w - 2 * inset_x, capsule_h - 2 * inset_y,
                     Qt.AlignmentFlag.AlignCenter, hint)
    painter.restore()


def draw_left_capsule_badge(painter: QPainter, hint: str, h: int,
                             capsule_w: int = 22, margin: int = 3) -> None:
    """Paint a small ABXY pill pinned to the **left edge, vertically centred**,
    fully inside the control (never clipped).  Used for long / wide buttons
    where a left-side placement keeps the label clear when it is right-inset.

    Mirrors the top capsule but on the horizontal edge — both are pills, just
    placed differently, so every shortcut reads as a consistent capsule.
    """
    if hint not in HINT_COLORS:
        return
    cy = h // 2
    cap_h = min(capsule_w, h - 4)
    radius = cap_h // 2
    x = margin
    y = cy - cap_h // 2
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    # dark backing for contrast (slightly larger than the pill)
    painter.setBrush(QColor(0, 0, 0, 160))
    painter.drawRoundedRect(x - 1, y - 1, capsule_w + 2, cap_h + 2, radius + 1, radius + 1)
    # coloured pill
    painter.setBrush(HINT_COLORS[hint])
    painter.drawRoundedRect(x, y, capsule_w, cap_h, radius, radius)
    # letter
    f = painter.font()
    f.setBold(True)
    f.setPixelSize(16)  # 9pt→16px 放大
    f.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    painter.setFont(f)
    painter.setPen(QColor(12, 14, 20))
    painter.drawText(x, y, capsule_w, cap_h, Qt.AlignmentFlag.AlignCenter, hint)
    painter.restore()


def draw_right_capsule_badge(painter: QPainter, hint: str, w: int, h: int,
                             capsule_w: int = 22, margin: int = 3) -> None:
    """Paint a small ABXY pill pinned to the **right edge, vertically centred**,
    fully inside the control (mirror of :func:`draw_left_capsule_badge`).  Used
    when a control opts to keep its hint on the right (e.g. freeze process
    buttons: Y = 标为游戏)."""
    if hint not in HINT_COLORS:
        return
    cy = h // 2
    cap_h = min(capsule_w, h - 4)
    radius = cap_h // 2
    x = w - margin - capsule_w
    y = cy - cap_h // 2
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    # dark backing for contrast (slightly larger than the pill)
    painter.setBrush(QColor(0, 0, 0, 160))
    painter.drawRoundedRect(x - 1, y - 1, capsule_w + 2, cap_h + 2, radius + 1, radius + 1)
    # coloured pill
    painter.setBrush(HINT_COLORS[hint])
    painter.drawRoundedRect(x, y, capsule_w, cap_h, radius, radius)
    # letter
    f = painter.font()
    f.setBold(True)
    f.setPixelSize(16)  # 9pt→16px 放大
    f.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    painter.setFont(f)
    painter.setPen(QColor(12, 14, 20))
    painter.drawText(x, y, capsule_w, cap_h, Qt.AlignmentFlag.AlignCenter, hint)
    painter.restore()


def draw_hint_auto(painter: QPainter, hint: str, w: int, h: int,
                   wide_threshold: int = HINT_WIDE_THRESHOLD) -> None:
    """Pick the capsule layout by button width — both are pills, never discs.

    * **Wide button** (``w >= wide_threshold``) → capsule pinned to the left
      edge, vertically centred; the label is inset to its right by
      ``LEFT_CAP_W`` so the two never overlap.
    * **Narrow button** → capsule pinned to the top edge, fully inside the
      control (never clipped, never covers the centred label).
    """
    if hint not in HINT_COLORS:
        return
    if w >= wide_threshold:
        draw_left_capsule_badge(painter, hint, h)
    else:
        draw_top_capsule_badge(painter, hint, w)


class HintButton(QPushButton):
    """Button that overlays an ABXY gamepad-hint capsule (A/B/X/Y) on its
    **left** edge, in pill form.

    The button does **not** self-paint its background.  It lets ``QPushButton``
    render normally via the stylesheet, so:

    * every caller's ``setStyleSheet`` colours are honoured (the layout
      editor's 重启程序 / 保存 / 呼吸灯自定义 … keep their distinct tints — #4),
    * the font comes from the stylesheet / app default instead of a hardcoded
      bold 10px that made some labels look bloated (#3),
    * the label is never squeezed by a top capsule (#2) — the badge is always
      a small pill pinned to the left, inside reserved left padding, so it can
      never overlap the centred text.

    The badge only appears when ``show_gamepad_hint`` is True (gated by the
    ``InteractionController`` once the user navigates by keyboard / gamepad).
    """

    # Left-padding (px) reserved by :meth:`set_style` so the capsule never
    # covers the label.  Callers that supply their own stylesheet must include
    # at least this much left padding (the layout editor's buttons already do).
    RESERVED_LEFT = 24

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.gamepad_hint = ""
        self.show_gamepad_hint = False
        # 胶囊位置：短按钮 = "top"（贴在按钮上沿，内部顶部）；长按钮 = "left"
        # （左侧胶囊）。仅由宽度决定，与提示是否显示无关，故高度恒定不跳变。
        self._hint_placement = "left"
        # 锁定摆放位置（"left"/"top"/"right"）；非 None 时 _recompute_placement
        # 不再按宽度自适应，用于进程冻结按钮把 Y 提示固定放在右侧。
        self._hint_placement_lock = None
        self._accent = "#7da2c8"
        self._active = False
        # 切角（chamfer）自绘所需的状态。仅当按钮风格预设写入了 cut_size 且
        # corner_radius==0 时，paintEvent 才走自绘切角路径；其余情况仍是原生
        # 样式表绘制（行为不变）。默认 None 表示「未套用预设、用原生绘制」。
        self._cut_size = 0
        self._corner_radius = 0
        self._bs_back = None
        self._bs_hover = None
        self._bs_border = None
        self._bs_fore = None
        self._bs_bt = 0
        # 激活态/未激活态颜色：仅当套用的预设定义了 ActiveBackColor/
        # InactiveBackColor 时由 styles._apply_style_to_ctrl 注入（非空）。
        # set_style 在按钮激活(active)时优先用 _bs_active_* 绘制，使「锁帧/
        # 自动锁帧/GPU浮动」这类切换按钮在绑定了带激活态的预设（如 ZZZ2）
        # 后，点击真的显示「开」的颜色；稀疏预设不写，回落到「基础色+文字」。
        self._bs_active_back = None
        self._bs_active_border = None
        self._bs_inactive_back = None
        self._bs_inactive_border = None
        self._hovered = False
        # 文本左对齐开关：默认 False（沿用原生 QPushButton 的居中）。冻结卡片
        # 的候选/已冻结按钮开启，让序号贴按钮左缘、便于「按数字键」一目了然。
        self._left_align = False
        # 常驻显示：为 True 时即便手柄导航未激活（调用方下发空提示），仍保留
        # 已设提示字母并显示胶囊——用于布局编辑页「保存(Y)/取消(B)」这类希望
        # 永远亮着手柄键提示的按钮。
        self._hint_always = False
        # 完全屏蔽：为 True 时永不显示手柄提示胶囊（调用方下发任何字母都忽略），
        # 用于「快捷键」编辑按钮等明确要求取消提示的场景。
        self._hint_suppressed = False
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Don't let the button grab keyboard focus: arrow keys must always reach
        # the window so the unified nav model can move the cursor between
        # buttons (keyboard + gamepad share one path).  Click / Enter still work
        # through the clicked signal.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(26)

    def _hint_height(self) -> int:
        # 高度恒定：无论顶部胶囊还是左侧胶囊，按钮自身高度都不变（胶囊完全在
        # 按钮内部，文字用 contents margin 下推），所以短按钮/编辑器按钮不会
        # 因为提示 placement 翻成 top 而被撑高。
        return 26

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt camelCase
        sh = super().sizeHint()
        cm = self.contentsMargins()
        # Grow horizontally with the label; height is fixed by capsule placement.
        # Include contentsMargins so left/right/top capsule reservations do not
        # squeeze the label after the layout has sized the widget.
        return QSize(
            max(sh.width() + cm.left() + cm.right(), 56),
            max(sh.height() + cm.top() + cm.bottom(), self._hint_height()),
        )

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt camelCase
        mh = super().minimumSizeHint()
        cm = self.contentsMargins()
        return QSize(
            max(mh.width() + cm.left() + cm.right(), 56),
            max(mh.height() + cm.top() + cm.bottom(), self._hint_height()),
        )

    def reserve_width_for(self, texts) -> None:
        """一次性固定按钮宽度 = 能容纳 ``texts`` 中最宽文案的宽度。

        目的：让文字会随状态切换（开/关 / 监测中 / 当前频率 / 不可用…）的
        按钮，在切换时**宽度恒定不跳**，避免把同一行的兄弟按钮左右挤动、
        破坏手柄/键盘导航的视觉顺序。

        取最宽文案按按钮当前字体/样式量出自然宽度（``sizeHint`` 已含样式表
        内边距与边框）；若该宽度达到「长按钮」阈值（``HINT_TOP_W``），说明它
        走左侧 ABXY 胶囊、手柄提示出现时会再加 ``RESERVED_LEFT`` 左边距——
        把这部分也**预先计入**固定宽度，使「提示出现/消失」同样不改变宽度。
        短按钮走顶部胶囊（提示完全画在按钮内部、不加边距），无需额外预留。
        """
        if not texts:
            return
        widest = max(texts, key=lambda t: self.fontMetrics().horizontalAdvance(t))
        cur = self.text()
        try:
            self.setText(widest)
            base = self.sizeHint().width()
        finally:
            self.setText(cur)
        if base >= HINT_TOP_W:
            # 长按钮：左侧胶囊出现时会加 RESERVED_LEFT 左边距，一并预留，
            # 否则提示出现时固定宽度会把文字压缩。
            base += self.RESERVED_LEFT
        self.setFixedWidth(max(56, base))

    def _sync_hint_margins(self) -> None:
        """为 ABXY 胶囊预留空间：顶部胶囊（短按钮）留上边距把文字下推；左侧
        胶囊（长按钮）留左边距。无提示时内边距全部归零，文字保持居中。"""
        old = self.contentsMargins()
        if not self.show_gamepad_hint:
            self.setContentsMargins(0, 0, 0, 0)
        elif self._hint_placement == "top":
            # 顶部胶囊完全画在按钮内部固定高度(26px)内：paintEvent 自绘时会把
            # 文字压在下半区、胶囊贴按钮上沿，二者不重叠。因此不再用上边距把
            # 按钮撑高（此前 11px 上边距被 sizeHint/minimumSizeHint 累加进高度，
            # 正是纵向拉伸的根因）。内边距保持 0 → 高度恒定不跳变。
            self.setContentsMargins(0, 0, 0, 0)
        elif self._hint_placement == "right":
            # 右侧胶囊：预留右内边距给胶囊，文字保持左对齐不被压住。
            self.setContentsMargins(0, 0, RIGHT_CAP_W, 0)
        else:
            self.setContentsMargins(self.RESERVED_LEFT, 0, 0, 0)
        if self.contentsMargins() != old:
            # Inform the layout so the widget can grow/shrink to fit the new
            # reservation; otherwise the reserved space squeezes the label.
            self.updateGeometry()

    def set_style(self, accent: str, active: bool) -> None:
        """Set the base look (accent colour + active/pressed state).

        Implemented as a stylesheet so ``QPushButton`` paints it natively —
        preserving the focus ring, hover and (crucially) any later
        ``setStyleSheet`` override a caller applies for a special-coloured
        button.  The left padding for the gamepad capsule is applied via
        :meth:`_sync_hint_margins` (contents margin), NOT the stylesheet, so it
        only appears when a hint is shown and the text stays centred otherwise.
        """
        accent = accent or "#7da2c8"
        self._accent = accent
        self._active = bool(active)
        # 预设保护：若该按钮已套用用户预设（样式表已缓存到 _preset_ss），直接
        # 复用预设样式表后返回，绝不用自带默认外观覆盖。否则状态切换 / 周期
        # 刷新反复调用 set_style 会把「圆角 + 悬浮色」预设冲掉，交互后看起来
        # 就「预设消失、变回方角 + 绑定主题色」。预设按钮的开/关状态由按钮
        # 文字（如「锁帧:开/关」）表达，颜色永远遵循用户在按钮风格编辑器里的
        # 预设——这正是用户期望的「我设的圆角 + 悬浮色保持不动」。
        preset_ss = getattr(self, "_preset_ss", None)
        if preset_ss:
            # 预设保护：已套用户预设时，默认仍复用基础预设样式表（圆角/悬浮色
            # 不动，向后兼容「稀疏预设只靠文字表达开关」的 7-30 行为）。
            # 但若该预设定义了激活态颜色（_bs_active_back），则按钮激活时改用
            # 激活态颜色绘制——这是「套了带激活态的预设（如 ZZZ2）后，锁帧/
            # 自动锁帧/GPU浮动 点击仍显示『开』」的修复点。
            # 切角预设走自绘路径，激活色由 _paint_chamfer 在绘制时按 self._active
            # 实时取，这里直接复用 transparent 样式表即可（几何不变）。
            if self._cut_size and not self._corner_radius and self._bs_back is not None:
                self.setStyleSheet(preset_ss)
                self._sync_hint_margins()
                self.update()
                return
            if active and getattr(self, "_bs_active_back", None):
                self._apply_preset_variant(
                    self._bs_active_back, getattr(self, "_bs_active_border", None),
                    self._bs_fore)
                return
            if (not active) and getattr(self, "_bs_inactive_back", None):
                self._apply_preset_variant(
                    self._bs_inactive_back, getattr(self, "_bs_inactive_border", None),
                    self._bs_fore)
                return
            # 无激活态变体（稀疏预设）→ 保持基础外观，开关只靠文字表达。
            self.setStyleSheet(preset_ss)
            self._sync_hint_margins()
            self.update()
            return
        # 保留已套用预设的几何：当按钮风格预设写了圆角/切角后，HUD 在选中 /
        # 聚焦 / 重建时会再次调用 set_style（re-apply 默认外观）。若此处把
        # _corner_radius / _cut_size / _bs_* 清零，会把预设的圆角（>7px）或
        # 切角冲掉 —— 圆角预设看起来就「变回矩形」。仅当本按钮尚未套用任何
        # 预设（_bs_back 为空且无圆角）时，才回到原生默认外观。
        preset_applied = bool(getattr(self, "_bs_back", None)) or bool(self._corner_radius)
        if not preset_applied:
            self._bs_back = None
            self._cut_size = 0
            self._corner_radius = 0
        # 圆角：有预设就用预设值（保持按钮风格预设的圆角不丢失），否则默认 7px。
        radius = int(self._corner_radius) if self._corner_radius else 7
        if active:
            bg = accent
            fg = "#0c0f16"
            border = accent
        else:
            bg = "rgba(255,255,255,0.10)"
            fg = "#dfe6f0"
            border = "rgba(255,255,255,0.18)"
        # 顶部胶囊（短按钮）需要更小竖向留白，给上沿胶囊让出空间。
        vpad = 1 if self._hint_placement == "top" else 5
        # 文本左对齐（仅冻结卡片按钮开启）：text-align 走样式表，选中显示手柄
        # 提示时由 contents margin 的左边距继续把文字推到胶囊右侧，互不冲突。
        align = " text-align: left;" if self._left_align else ""
        ss = (
            "QPushButton {"
            f" background: {bg}; color: {fg};"
            f" border: 1px solid {border}; border-radius: {radius}px;"
            f" padding: {vpad}px 12px;"
            " font-size: 11px;" + align +
            "}"
            "QPushButton:hover { background: rgba(255,255,255,0.16); }"
            "QPushButton:focus { outline: none;"
            " border: 1px solid rgba(255,255,255,0.40); }"
        )
        self.setStyleSheet(ss)
        self._sync_hint_margins()
        self.update()

    def _apply_preset_variant(self, back, border, fore) -> None:
        """套用预设的激活/未激活态颜色，同时保留预设的几何（圆角/边框厚度）。

        仅用于非切角预设：圆角预设的外观完全由样式表控制，这里用激活态颜色
        重拼一张样式表（边框/圆角/内边距与原预设一致），既显示「开」的颜色，
        又不回落到默认主题色外观（避免 7-30 修复前的『交互后预设消失』问题）。
        """
        border = border or self._bs_border
        fore = fore or self._bs_fore
        cr = int(self._corner_radius) if self._corner_radius else 7
        bt = int(self._bs_bt) if self._bs_bt else 1
        ss = (
            f"QPushButton {{ background: rgb({back[0]},{back[1]},{back[2]});"
            f" color: rgb({fore[0]},{fore[1]},{fore[2]});"
            f" border: {bt}px solid rgb({border[0]},{border[1]},{border[2]});"
            f" border-radius: {cr}px; padding: 4px 10px; }}"
            "QPushButton:hover { background: rgba(255,255,255,0.16); }"
            "QPushButton:focus { outline: none;"
            " border: 1px solid rgba(255,255,255,0.40); }"
        )
        self.setStyleSheet(ss)
        self._sync_hint_margins()
        self.update()

    def _recompute_placement(self) -> None:
        """短按钮（宽度 < HINT_TOP_W）把提示胶囊贴在按钮上沿（内部顶部），
        其余按钮保持左侧胶囊。位置只由宽度决定（与提示是否显示无关），
        因此按钮高度恒定，不会在显示/隐藏提示时跳变。

        若 ``_hint_placement_lock`` 已设置（如冻结按钮锁定 "right"），
        则尊重锁定值、不再按宽度自适应覆盖。"""
        if self._hint_placement_lock:
            self._hint_placement = self._hint_placement_lock
            return
        self._hint_placement = (
            "top" if (self.width() > 0 and self.width() < HINT_TOP_W) else "left"
        )

    def set_hint_side(self, side: str) -> None:
        """锁定提示胶囊的摆放位置（"left" / "top" / "right"）。

        锁定后 :meth:`_recompute_placement` 不再按宽度自适应覆盖，确保像
        「进程冻结按钮把 Y 提示放右侧」这类特例能稳定生效（否则每次
        ``set_gamepad_hint`` 都会把位置重算回 left/top）。"""
        self._hint_placement_lock = side
        self._recompute_placement()
        self._sync_hint_margins()
        self.update()

    def set_hint_always_visible(self, v: bool) -> None:
        """让手柄提示「常驻显示」：即便手柄导航未激活，也保留并绘制提示胶囊。

        调用方通常仍需先 ``set_gamepad_hint("Y")`` 设定字母；本方法仅保证该
        字母在导航未激活时不被清空、不消失。"""
        self._hint_always = bool(v)
        if self._hint_always and self.gamepad_hint:
            self.show_gamepad_hint = True
            self._sync_hint_margins()
            self.update()

    def set_hint_suppressed(self, v: bool) -> None:
        """完全屏蔽手柄提示：为 True 时任何 ``set_gamepad_hint`` 调用都被忽略，
        提示胶囊永远不绘制（用于明确要求取消提示的按钮）。"""
        self._hint_suppressed = bool(v)
        if self._hint_suppressed and (self.gamepad_hint or self.show_gamepad_hint):
            self.gamepad_hint = ""
            self.show_gamepad_hint = False
            self._recompute_placement()
            self._sync_hint_margins()
            self.update()

    def set_gamepad_hint(self, hint: str) -> None:
        hint = hint or ""
        # 完全屏蔽：永不显示提示，调用方下发的任何字母都忽略。
        if self._hint_suppressed:
            if self.gamepad_hint or self.show_gamepad_hint:
                self.gamepad_hint = ""
                self.show_gamepad_hint = False
                self._recompute_placement()
                # 只刷新内边距（清空胶囊预留空间）并重绘，绝不重写整个样式表。
                self._sync_hint_margins()
                self.update()
            return
        # 常驻显示：导航未激活时调用方下发空串，仍保留已设字母不消失。
        if self._hint_always and hint == "" and self.gamepad_hint:
            hint = self.gamepad_hint
        if hint != self.gamepad_hint:
            self.gamepad_hint = hint
            self.show_gamepad_hint = bool(hint) or self._hint_always
            self._recompute_placement()
            # 只刷新内边距（为胶囊预留空间）并重绘，绝不重写整个样式表——
            # 否则会把按钮风格预设套上的圆角/颜色冲掉（选中显示提示后变回默认）。
            self._sync_hint_margins()
            self.update()
        elif self._hint_always and not self.show_gamepad_hint and self.gamepad_hint:
            # 字母未变但需补保证常驻可见（首次进入编辑器时可能尚未被下发过）。
            self.show_gamepad_hint = True
            self._sync_hint_margins()
            self.update()

    def enterEvent(self, ev: object) -> None:  # noqa: N802 - Qt camelCase
        self._hovered = True
        self.update()
        super().enterEvent(ev)  # type: ignore[call-arg]

    def leaveEvent(self, ev: object) -> None:  # noqa: N802 - Qt camelCase
        self._hovered = False
        self.update()
        super().leaveEvent(ev)  # type: ignore[call-arg]

    def resizeEvent(self, ev: object) -> None:  # noqa: N802 - Qt camelCase
        self._recompute_placement()
        # 只刷新内边距（胶囊位置可能随宽度翻转），绝不重写样式表——
        # 否则会冲掉按钮风格预设的颜色/圆角，以及编辑器按钮的自定义配色。
        self._sync_hint_margins()
        super().resizeEvent(ev)  # type: ignore[call-arg]

    def paintEvent(self, ev: object) -> None:  # noqa: N802 - Qt camelCase
        # 切角（chamfer）预设：corner_radius==0 且 cut_size>0 时，原生
        # QPushButton 无法做切角（border-radius 只能圆角），改为自绘切角路径。
        # 这样「切角」设置才会真正生效。其余情况（圆角预设 / 默认样式）仍走
        # 原生绘制，行为完全不变。
        if (self._cut_size and not self._corner_radius
                and self._bs_back is not None):
            self._paint_chamfer(ev)
            return
        # 三种摆放（短按钮顶部 / 长按钮左侧 / 右侧）统一走原生 QPushButton 绘制
        # （背景、文字、字体、聚焦环全部由样式表接管），仅在末尾叠加 ABXY 胶囊。
        # 这样短按钮显示提示时也能保留其真实背景/预设色，而不会像此前顶部胶囊
        # 自绘那样把按钮背景硬写成近乎透明（表现为「一片空白」）。顶部胶囊完全
        # 落在按钮内部（不超出边界），且 _sync_hint_margins 对 top 摆放已置 0
        # 上边距，故按钮高度恒定、不纵向拉伸（bug #1 修复不变式保持）。
        super().paintEvent(ev)
        if self.show_gamepad_hint and self.gamepad_hint in HINT_COLORS:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            if self._hint_placement == "right":
                # 右侧胶囊，位于预留的右边距内。
                draw_right_capsule_badge(
                    p, self.gamepad_hint, self.width(), self.height(),
                    capsule_w=16, margin=3)
            elif self._hint_placement == "top":
                # 短按钮顶部胶囊，完全贴在按钮上沿内部。
                draw_top_capsule_badge(p, self.gamepad_hint, self.width())
            else:
                # 长按钮：左侧胶囊，位于预留的左边距内。
                draw_left_capsule_badge(
                    p, self.gamepad_hint, self.height(),
                    capsule_w=16, margin=3)
            p.end()

    def _paint_chamfer(self, ev: object) -> None:  # noqa: N802 - Qt camelCase
        """自绘切角背景 + 边框 + 文字 + 手柄提示（仅切角预设启用）。"""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        cs = int(self._cut_size)
        cs = max(0, min(cs, min(w, h) // 2))
        path = QPainterPath()
        path.moveTo(cs, 0)
        path.lineTo(w, 0)
        path.lineTo(w, h - cs)
        path.lineTo(w - cs, h)
        path.lineTo(0, h)
        path.lineTo(0, cs)
        path.closeSubpath()
        # 激活态（active）且预设定义了激活色时，用激活色自绘；否则走基础色
        # （悬浮时用悬浮色）。切角预设的「开/关」颜色由此处实时取，几何不变。
        if self._active and self._bs_active_back is not None:
            back = self._bs_active_back
            border = self._bs_active_border or self._bs_border
            fore = self._bs_fore
        else:
            back = self._bs_hover if (self._hovered and self._bs_hover) else self._bs_back
            border = self._bs_border
            fore = self._bs_fore
        p.fillPath(path, QColor(*back))
        if self._bs_bt and border:
            p.setPen(QPen(QColor(*border), int(self._bs_bt)))
            p.drawPath(path)
        if fore:
            p.setPen(QColor(*fore))
            # 用按钮自身字体（与原生 QPushButton 一致），不要硬编码加粗大字——
            # 否则切角预设应用到主页面后文字会突然变粗变大（与原预览/其它按钮
            # 不一致）。
            p.setFont(self.font())
            tr = self.rect()
            if self.show_gamepad_hint and self._hint_placement == "top":
                tr = tr.adjusted(0, CAPSULE_TOP_MARGIN + CAPSULE_INNER_HEIGHT, 0, 0)
            elif self.show_gamepad_hint and self._hint_placement == "right":
                tr = tr.adjusted(0, 0, -BADGE_WIDTH, 0)
            elif self.show_gamepad_hint:
                tr = tr.adjusted(BADGE_WIDTH, 0, 0, 0)
            _ta = (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter) \
                if self._left_align else Qt.AlignmentFlag.AlignCenter
            p.drawText(tr, _ta, self.text())
            if self.show_gamepad_hint and self.gamepad_hint in HINT_COLORS:
                if self._hint_placement == "right":
                    draw_right_capsule_badge(
                        p, self.gamepad_hint, w, h, capsule_w=16, margin=3)
                else:
                    draw_hint_auto(p, self.gamepad_hint, w, h)
        p.end()


class GamepadHintBar(QWidget):
    """Persistent (always-on) legend of ABXY gamepad shortcuts, drawn in the
    exact same capsule style as :class:`HintButton` (``HINT_COLORS`` + dark
    backing) so it reads as one family with the in-control hints.

    Unlike :class:`HintButton`, this is *always* visible — it is a static
    reference, not driven by navigation state.  Used by panels that want a
    bottom strip listing what each gamepad key does (e.g. the freeze card:
    X=关闭进程, Y=标为游戏).  ``entries`` is a list of ``(letter, text)``;
    entries without a known colour are drawn as bare text.
    """

    def __init__(self, entries: list = None, parent: QWidget = None) -> None:
        super().__init__(parent)
        # Explicit palette so the bar is readable in both themes without
        # depending on CSS vars.  Dark background theme → light text below.
        self._pill_w = 18
        self._pill_h = 16
        self._gap = 14
        self._text_gap = 5
        self._letter_px = 14
        self._label_px = 15
        self.setContentsMargins(2, 4, 2, 4)
        self._entries = []
        self._layout = []
        self._total_w = 0
        self.set_entries(entries or [])

    def set_entries(self, entries: list) -> None:
        """Replace the displayed entries (``(letter, text)`` tuples)."""
        self._entries = list(entries)
        self._refresh()
        self.updateGeometry()
        self.update()

    def _refresh(self) -> None:
        fm = self.fontMetrics()
        x = 0
        self._layout = []
        for letter, text in self._entries:
            tw = fm.horizontalAdvance(text) if text else 0
            entry_w = self._pill_w + self._text_gap + tw
            self._layout.append((x, letter, text, entry_w))
            x += entry_w + self._gap
        self._total_w = max(0, x - self._gap)
        self.setFixedHeight(self._pill_h + 8)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt camelCase
        return QSize(self._total_w, self._pill_h + 8)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt camelCase
        return self.sizeHint()

    def paintEvent(self, ev: object) -> None:  # noqa: N802 - Qt camelCase
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        y = (self.height() - self._pill_h) // 2
        for (x, letter, text, entry_w) in self._layout:
            if letter in HINT_COLORS:
                # dark backing (slightly larger than the pill) for contrast
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(0, 0, 0, 150))
                p.drawRoundedRect(x - 1, y - 1, self._pill_w + 2,
                                  self._pill_h + 2, (self._pill_h + 2) // 2,
                                  (self._pill_h + 2) // 2)
                # coloured pill
                p.setBrush(HINT_COLORS[letter])
                p.drawRoundedRect(x, y, self._pill_w, self._pill_h,
                                  self._pill_h // 2, self._pill_h // 2)
                # letter — bold, dark on the coloured pill
                lf = QFont(self.font())
                lf.setBold(True)
                lf.setPixelSize(self._letter_px)
                lf.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
                p.setFont(lf)
                p.setPen(QColor(12, 14, 20))
                p.drawText(x, y, self._pill_w, self._pill_h,
                           Qt.AlignmentFlag.AlignCenter, letter)
            # label text — light, readable in dark theme
            p.setFont(self.font())
            tf = QFont(self.font())
            tf.setPixelSize(self._label_px)
            p.setFont(tf)
            p.setPen(QColor(223, 230, 240))
            tx = x + self._pill_w + self._text_gap
            p.drawText(tx, y, entry_w - self._pill_w - self._text_gap,
                       self._pill_h,
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       text)
        p.end()
