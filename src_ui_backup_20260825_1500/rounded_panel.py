"""RoundedPanel — PyQt6 port of the C# ``RoundedPanel`` card container."""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QPoint, QRect
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPaintEvent, QPen, QPolygon
from PyQt6.QtWidgets import QWidget

from ..core.color.rgb import Color


class RoundedPanel(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.cut_size = 10
        self.border_width = 1
        self.border_color = QColor(0, 0, 0, 0)
        self.back_color = QColor(28, 30, 38)
        self.gradient_top = QColor(34, 36, 44)
        self.gradient_bottom = QColor(22, 24, 32)

    def set_colors(self, back: Color, border: Optional[Color] = None) -> None:
        self.back_color = QColor(back.r, back.g, back.b)
        if border is not None:
            self.border_color = QColor(border.r, border.g, border.b)
        self.update()

    def _path(self, w: int, h: int) -> QPolygon:
        cs = self.cut_size
        poly = QPolygon()
        poly.append(QPoint(cs, 0))
        poly.append(QPoint(w, 0))
        poly.append(QPoint(w, h - cs))
        poly.append(QPoint(w - cs, h))
        poly.append(QPoint(0, h))
        poly.append(QPoint(0, cs))
        return poly

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0, self.gradient_top)
        grad.setColorAt(1, self.gradient_bottom)
        painter.setBrush(grad)
        painter.setPen(QPen(self.border_color, self.border_width))
        painter.drawPolygon(self._path(w, h))
        painter.end()
