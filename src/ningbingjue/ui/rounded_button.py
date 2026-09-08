"""RoundedButton — PyQt6 port of the C# ``RoundedButton``.

Key behaviors preserved: explicit ``set_colors`` API, hover gradient, neon
border/glow, ``CutSize`` corner cut, ``GamepadHint`` A/B/X/Y badge, and toggle
(active/inactive) dual-state coloring.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPaintEvent,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import QWidget

from ..core.color.rgb import Color
from .gamepad_hint import (
    BADGE_WIDTH,
    CAPSULE_TOP_MARGIN,
    HINT_PILL_TOP_H,
    choose_hint_side,
    draw_hint_auto,
)


class RoundedButton(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._back = QColor(28, 30, 38)
        self._hover = QColor(40, 43, 52)
        self._border = QColor(95, 175, 235)
        self._fore = QColor(230, 255, 30)
        self._hover_fore = QColor(255, 255, 255)
        self._normal_back = self._back
        self.corner_radius = 0
        self.cut_size = 8
        self.border_thickness = 4
        self.hover_only_border = False
        self.hover_gradient = True
        self._grad_top = QColor(34, 36, 44)
        self._grad_bot = QColor(22, 24, 32)
        self.glow_width = 3
        self._hover_border = QColor(0, 0, 0, 0)  # 悬浮边框色（透明 = 未设置，沿用常规边框）
        self.gamepad_hint = ""
        self.show_gamepad_hint = False
        # 长按提示：True → 徽章画成拖尾式（胶囊 + 渐隐横条），与短按徽章区分；
        # _hint_hold_progress 驱动虚点随按住时长淡出。属性名与 HintButton 对齐，
        # 好让控制器 / 长按逻辑用同一套 hasattr 判断对两类按钮都生效。
        self._hint_long = False
        self._hint_hold_progress = 0.0
        # When False the gamepad hint is NOT drawn inline (the subclass paints
        # it itself, e.g. TileButton draws it above the icon). Defaults True so
        # plain RoundedButton buttons keep the original inline-hint behaviour.
        self._inline_hint = True
        self._text = ""
        self._hovered = False
        self._is_toggle = False
        self._active = False
        self._active_back = QColor(0, 0, 0, 0)
        self._active_border = QColor(0, 0, 0, 0)
        self._inactive_back = QColor(0, 0, 0, 0)
        self._inactive_border = QColor(0, 0, 0, 0)
        self.setMouseTracking(True)

    # -- color API ---------------------------------------------------------
    def set_colors(self, back: Color, hover: Color, border: Color, fore: Color,
                   hover_fore: Optional[Color] = None) -> None:
        self._back = QColor(back.r, back.g, back.b)
        self._normal_back = self._back
        self._hover = QColor(hover.r, hover.g, hover.b)
        self._border = QColor(border.r, border.g, border.b)
        self._fore = QColor(fore.r, fore.g, fore.b)
        self._hover_fore = QColor(hover_fore.r, hover_fore.g, hover_fore.b) if hover_fore else QColor(255, 255, 255)
        self.update()

    def set_active_colors(self, active_back: Color, active_border: Color,
                          inactive_back: Color, inactive_border: Color) -> None:
        self._active_back = QColor(active_back.r, active_back.g, active_back.b)
        self._active_border = QColor(active_border.r, active_border.g, active_border.b)
        self._inactive_back = QColor(inactive_back.r, inactive_back.g, inactive_back.b)
        self._inactive_border = QColor(inactive_border.r, inactive_border.g, inactive_border.b)

    # -- gradient (悬停渐变 顶/底) ---------------------------------------
    # 之前 paintEvent 读的是 _grad_top/_grad_bot，而外部用 gradient_hover_top
    # 赋值（属性名不匹配），导致『渐变顶/渐变底』完全无效。这里补上属性桥接。
    @property
    def gradient_hover_top(self) -> QColor:
        return self._grad_top

    @gradient_hover_top.setter
    def gradient_hover_top(self, c: Optional[Color]) -> None:
        self._grad_top = QColor(c.r, c.g, c.b) if c else QColor(34, 36, 44)
        self.update()

    @property
    def gradient_hover_bottom(self) -> QColor:
        return self._grad_bot

    @gradient_hover_bottom.setter
    def gradient_hover_bottom(self, c: Optional[Color]) -> None:
        self._grad_bot = QColor(c.r, c.g, c.b) if c else QColor(22, 24, 32)
        self.update()

    # -- hover border (悬浮边框) -----------------------------------------
    @property
    def hover_border(self) -> QColor:
        return self._hover_border

    @hover_border.setter
    def hover_border(self, c: Optional[Color]) -> None:
        self._hover_border = QColor(c.r, c.g, c.b) if c else QColor(0, 0, 0, 0)
        self.update()

    def set_button_state(self, active: bool) -> None:
        self._is_toggle = True
        self._active = active
        self.update()

    def clear_button_state(self) -> None:
        self._is_toggle = False
        self._active = False
        self.update()

    def set_gamepad_hint(self, hint: str) -> None:
        """Persistently show the gamepad hint (A/B/X/Y) on this button.

        The badge layout (left disc vs top capsule) is chosen automatically by
        width at paint time — see :func:`draw_hint_auto`.

        幂等：hint 与显示态都未变化时直接返回，避免每次焦点移动都无条件
        ``update()`` 触发整按钮重绘。``_refresh_active_hints`` 每移动一次就
        遍历所有 item 调用本方法，原实现对每个带 hint 的按钮都重绘一遍，是
        长按切光标卡顿的次要共享根因（与 ``set_card_selected`` 的 early-return
        对齐）。仅在确有变化时才重绘。
        """
        # 与 HintButton.set_gamepad_hint 一致的大小写归一化：徽章绘制函数
        # （draw_hint_auto 等）按 *大写* key 查 HINT_COLORS，传小写会静默不绘制。
        hint = (hint or "").upper()
        wanted = bool(hint)
        if hint == self.gamepad_hint and self.show_gamepad_hint == wanted:
            return
        self.gamepad_hint = hint
        self.show_gamepad_hint = wanted
        self.update()

    def set_hint_long_press(self, v: bool) -> None:
        """标记该按钮的手柄提示为「长按」，徽章改用拖尾式（胶囊 + 渐隐横条）。

        与 :meth:`HintButton.set_hint_long_press` 同名同义，使控制器用同一套
        ``hasattr`` 判断即可对两类按钮下发长按标记。幂等：值未变直接返回。
        """
        v = bool(v)
        if v == self._hint_long:
            return
        self._hint_long = v
        self.update()

    def set_hint_hold_progress(self, p: float) -> None:
        """推长按进度(0→1)：0 = 虚点全亮静止；1 = 全部灭（长按已触发）。"""
        p = max(0.0, min(1.0, float(p)))
        if abs(p - self._hint_hold_progress) < 0.01:
            return
        self._hint_hold_progress = p
        self.update()

    # -- text (so ForeColor / HoverForeColor are actually visible) ------
    def setText(self, text: str) -> None:  # noqa: N802 - Qt camelCase
        self._text = text or ""
        self.update()

    def text(self) -> str:  # noqa: N802 - Qt camelCase
        return self._text

    def set_hover_state(self, hovered: bool) -> None:
        """Force the hover visual without a real mouse (used by the editor's
        '悬停' preview state so gradient / hover-fore colours become visible)."""
        self._hovered = bool(hovered)
        self.update()

    # -- mouse -------------------------------------------------------------
    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self.update()

    # -- drawing -----------------------------------------------------------
    def _path(self, w: int, h: int):
        """Button outline as a ``QPainterPath``.

        When ``corner_radius > 0`` the button is a rounded rectangle; otherwise
        it keeps the original cut-corner (hexagon) silhouette.  Rounded corners
        take precedence so the editor's 圆角 slider has a visible effect.
        """
        path = QPainterPath()
        if self.corner_radius > 0:
            path.addRoundedRect(0, 0, w, h, self.corner_radius, self.corner_radius)
            return path
        cs = self.cut_size
        path.moveTo(cs, 0)
        path.lineTo(w, 0)
        path.lineTo(w, h - cs)
        path.lineTo(w - cs, h)
        path.lineTo(0, h)
        path.lineTo(0, cs)
        path.closeSubpath()
        return path

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        back = self._back
        if self._is_toggle:
            tb = self._active_back if self._active else self._inactive_back
            if tb.alpha() != 0:
                back = tb
        if self._hovered:
            back = self._hover

        if self.hover_gradient and self._hovered:
            grad = QLinearGradient(0, 0, 0, h)
            grad.setColorAt(0, self._grad_top)
            grad.setColorAt(1, self._grad_bot)
            painter.setBrush(grad)
        else:
            painter.setBrush(back)

        border = self._border
        if self._is_toggle:
            tb = self._active_border if self._active else self._inactive_border
            if tb.alpha() != 0:
                border = tb
        # 悬浮边框：悬停时优先用专属边框色（留空则沿用常规边框）。
        if self._hovered and self._hover_border.alpha() != 0:
            border = self._hover_border
        # 仅悬停边框：非悬停态不画边框。
        if self.hover_only_border and not self._hovered:
            painter.setPen(Qt.PenStyle.NoPen)
        else:
            painter.setPen(QPen(border, self.border_thickness))
        painter.drawPath(self._path(w, h))

        # centred label — ForeColor (normal) / HoverForeColor (hover).
        # When a gamepad hint is shown, reserve room for it so the two never
        # overlap: a wide button insets the text on the LEFT (disc layout); a
        # narrow button pushes the text DOWN (top-capsule layout).
        if self._text:
            painter.setPen(QColor(self._hover_fore if self._hovered else self._fore))
            bf = QFont("Microsoft YaHei")
            bf.setPixelSize(13)  # 与 HintButton/卡片文字统一 13px，比例协调
            bf.setBold(True)
            bf.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
            painter.setFont(bf)
            text_rect = self.rect()
            if self.show_gamepad_hint:
                # 与 HintButton 一致的动态摆放：宽按钮默认左缘胶囊、文字右移避让；
                # 若居中文字会盖到胶囊（文字过宽）则回退顶部左上角、文字下移。
                side = choose_hint_side(w, self._text, painter.fontMetrics())
                if side == "left":
                    text_rect = text_rect.adjusted(BADGE_WIDTH, 0, 0, 0)
                else:
                    # 扁胶囊：预留 顶边距 + 胶囊高 + 1px 背板余量，与 HintButton 一致
                    text_rect = text_rect.adjusted(
                        0, CAPSULE_TOP_MARGIN + HINT_PILL_TOP_H + 1, 0, 0)
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, self._text)

        # gamepad hint badge — left disc (wide) or top capsule (narrow),
        # drawn inline so it is never clipped by any parent.  Subclasses that
        # paint the hint themselves (TileButton, above the icon) set
        # ``_inline_hint = False`` to suppress this.
        if self.show_gamepad_hint and self._inline_hint:
            draw_hint_auto(painter, self.gamepad_hint, w, h, text=self._text,
                           long_press=self._hint_long,
                           hold_progress=self._hint_hold_progress)
        painter.end()