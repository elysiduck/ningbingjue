"""Gaussian-blur helper shared by the live HUD frosted backdrop and the
呼吸灯自定义 editor preview.

Both the real card-area background (a grabbed + blurred desktop snapshot) and
the editor's representative preview use the *same* blur kernel so what you tune
in the editor matches what you see on the HUD.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPainter, QPixmap
from PyQt6.QtWidgets import (
    QGraphicsBlurEffect,
    QGraphicsPixmapItem,
    QGraphicsScene,
)


def blur_pixmap(src: QPixmap, radius: int) -> QPixmap:
    """Return ``src`` blurred with a Gaussian kernel of ``radius`` (px).

    ``radius <= 0`` (or a null source) returns the original pixmap unchanged.
    The output keeps the source's device-pixel-ratio so it stays crisp on
    HiDPI displays.
    """
    if src is None or src.isNull() or radius <= 0:
        return src
    dpr = src.devicePixelRatio() or 1.0
    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(src)
    item.setGraphicsEffect(QGraphicsBlurEffect())
    effect = item.graphicsEffect()
    if effect is not None:
        effect.setBlurRadius(radius)
    scene.addItem(item)
    out = QPixmap(src.size())
    out.setDevicePixelRatio(dpr)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    scene.render(painter)
    painter.end()
    return out
