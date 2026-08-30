"""RoundedButton — PyQt6 port of the C# ``RoundedButton``.

Key behaviors preserved: explicit ``set_colors`` API, hover gradient, neon
border/glow, ``CutSize`` corner cut, ``GamepadHint`` A/B/X/Y badge, and toggle
(active/inactive) dual-state coloring.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QPoint, Qt
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
    CAPSULE_INNER_HEIGHT,
    CAPSULE_TOP_MARGIN,
    HINT_WIDE_THRESHOLD,
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
        """
        self.gamepad_hint = hint or ""
        self.show_gamepad_hint = bool(self.gamepad_hint)
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
            bf.setPixelSize(18)  # 10pt→18px 放大，掌机高PPI下更清晰
            bf.setBold(True)
            bf.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
            painter.setFont(bf)
            text_rect = self.rect()
            if self.show_gamepad_hint:
                if self.width() >= HINT_WIDE_THRESHOLD:
                    text_rect = text_rect.adjusted(BADGE_WIDTH, 0, 0, 0)
                else:
                    # 扁胶囊：预留 顶边距 + 胶囊高 + 1px 背板余量，与 HintButton 一致
                    text_rect = text_rect.adjusted(
                        0, CAPSULE_TOP_MARGIN + CAPSULE_INNER_HEIGHT + 1, 0, 0)
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, self._text)

        # gamepad hint badge — left disc (wide) or top capsule (narrow),
        # drawn inline so it is never clipped by any parent.  Subclasses that
        # paint the hint themselves (TileButton, above the icon) set
        # ``_inline_hint = False`` to suppress this.
        if self.show_gamepad_hint and self._inline_hint:
            draw_hint_auto(painter, self.gamepad_hint, w, h)
        painter.end()
