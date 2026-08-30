"""Slider — custom FPS slider (track + fill + circular thumb) with drag.

Mirrors the rtss block's three-piece slider: track panel, fill panel, circular
thumb button.  Drag/click/track-click all funnel into ``on_value_changed``.
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter, QPaintEvent, QPen
from PyQt6.QtWidgets import QWidget


class Slider(QWidget):
    def __init__(self, parent: Optional[QWidget] = None, minimum: int = 20, maximum: int = 60) -> None:
        super().__init__(parent)
        self.minimum = minimum
        self.maximum = maximum
        self.value = minimum
        self.track_color = QColor(45, 50, 60)
        self.fill_color = QColor(95, 175, 235)
        self.thumb_color = QColor(130, 200, 240)
        self.on_value_changed: Optional[Callable[[int], None]] = None

    def _set_value(self, v: int) -> None:
        v = max(self.minimum, min(self.maximum, v))
        if v != self.value:
            self.value = v
            if self.on_value_changed:
                self.on_value_changed(v)
            self.update()

    def _value_from_x(self, x: int) -> int:
        w = max(1, self.width())
        ratio = max(0.0, min(1.0, x / w))
        return self.minimum + int(round(ratio * (self.maximum - self.minimum)))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._set_value(self._value_from_x(event.position().x()))

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._set_value(self._value_from_x(event.position().x()))

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        h = self.height()
        painter.setBrush(self.track_color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(0, h // 2 - 4, self.width(), 8, 4, 4)
        ratio = (self.value - self.minimum) / max(1, (self.maximum - self.minimum))
        fill_w = int(ratio * self.width())
        painter.setBrush(self.fill_color)
        painter.drawRoundedRect(0, h // 2 - 4, fill_w, 8, 4, 4)
        painter.setBrush(self.thumb_color)
        painter.setPen(QPen(QColor(255, 255, 255), 2))
        painter.drawEllipse(fill_w - 12, h // 2 - 12, 24, 24)
        painter.end()
