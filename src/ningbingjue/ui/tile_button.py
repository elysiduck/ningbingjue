"""TileButton — PyQt6 port of the C# ``TileButton`` (launcher tile).

Mirrors the original's full self-draw launcher tile: a rounded cut-corner
button (``RoundedButton``) that additionally carries an **icon / thumbnail**
that fills the entire button area (``SetTileImage``), plus a name label
overlaid at the bottom (``TileLabel`` / ``ShowTileName``).

The original extracts a high-res icon for ``.exe/.lnk/.dll`` via
``IconHelper.ExtractHighResIcon`` and otherwise loads the image file.  We
reproduce that with two entry points:

* :meth:`set_icon` — load an explicit image file (the tile's ``IconPath``,
  i.e. a custom user picture).
* :meth:`set_file_icon` — use ``QFileIconProvider`` to pull the OS shell
  icon for the launch target (exe / lnk / folder / file), the faithful
  equivalent of ``ExtractAssociatedIcon`` for everything else.

Because ``RoundedButton`` is a ``QWidget`` (not ``QPushButton``) we expose a
``clicked`` signal so the unified interaction layer can wire it the same way
it wires every other control.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QFileInfo, QRect, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QFileIconProvider, QLabel, QWidget

from .rounded_button import RoundedButton
from .gamepad_hint import draw_hint_auto


class TileButton(RoundedButton):
    clicked = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(54)
        # Icon is stored as a pixmap and drawn *manually* in paintEvent so the
        # gamepad-hint badge can be painted ON TOP of it.  A child QLabel would
        # be painted by Qt *after* the parent and hide the badge (the original
        # bug: tile icon/image covered the ABXY hint).
        self._pixmap: Optional[QPixmap] = None
        # Name label overlaid at the bottom with a semi-transparent backdrop.
        self._name_label = QLabel(self)
        self._name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._name_label.setStyleSheet(
            "color: #ffffff; font-size: 10px;"
            " background: rgba(0,0,0,0.45); padding: 2px 4px;")
        self.show_tile_name = True
        self._icon_path = ""
        self._selected = False
        self._tile_idx = -1
        # Per-tile override colours (empty string → use panel accent).
        self.tile_border_color = ""
        self.tile_bg_color = ""
        # Tile buttons paint the gamepad hint themselves (above the icon), so
        # suppress RoundedButton's inline hint — otherwise it would be drawn
        # under the icon and never seen.
        self._inline_hint = False

    def set_selected(self, sel: bool) -> None:
        """Highlight the tile (used by the editor's 2-D arrangement grid)."""
        self._selected = bool(sel)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        old = self._border
        if self._selected:
            self._border = QColor(245, 215, 75)  # yellow selection ring
        # RoundedButton draws bg/border (and text, none for tiles).  Its inline
        # hint is suppressed (see __init__), so it is NOT painted under the icon.
        super().paintEvent(event)
        self._border = old

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        # Icon fills the tile area.  Use a *cover* crop (scale to fill, then
        # centre-crop the overflow) instead of stretching, so changing the tile
        # size never distorts the picture.  Rendered at device resolution so it
        # stays crisp on fractional DPR (e.g. 1.5x handheld), matching the
        # tray-background cover logic in app_window.py.
        if self._pixmap is not None and not self._pixmap.isNull():
            tw, th = w - 4, h - 4
            if tw > 0 and th > 0:
                img = self._pixmap.toImage()
                if not img.isNull():
                    dpr = max(1.0, float(self.devicePixelRatio()))
                    dw, dh = int(round(tw * dpr)), int(round(th * dpr))
                    scaled = img.scaled(
                        dw, dh,
                        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation)
                    sx = max(0, (scaled.width() - dw) // 2)
                    sy = max(0, (scaled.height() - dh) // 2)
                    painter.drawImage(
                        QRect(2, 2, tw, th), scaled, QRect(sx, sy, dw, dh))

        # Gamepad hint painted LAST → always visible above the icon/image.
        if self.show_gamepad_hint:
            draw_hint_auto(painter, self.gamepad_hint, w, h)

        # Name overlaid at the bottom (child widget, painted after this — it
        # never overlaps the top/left hint, so the badge stays clear).
        if self.show_tile_name:
            nh = 22
            self._name_label.setGeometry(0, h - nh, w, nh)
            self._name_label.show()
        else:
            self._name_label.hide()
        painter.end()

    # -- icon ----------------------------------------------------------
    def set_icon(self, icon_path: str) -> None:
        """Load an explicit image file as the tile's icon/thumbnail."""
        self._icon_path = icon_path
        try:
            pix = QPixmap(icon_path)
            if not pix.isNull():
                self._pixmap = self._dpr_pixmap(pix)
                self.update()
                return
        except Exception:
            pass
        self.clear_icon()

    def set_file_icon(self, path: str) -> None:
        """Use the OS shell icon for the launch target (exe/lnk/folder/…)."""
        if not path:
            self.clear_icon()
            return
        try:
            icon: QIcon = QFileIconProvider().icon(QFileInfo(path))
            pix = icon.pixmap(128, 128)
            if not pix.isNull():
                self._pixmap = self._dpr_pixmap(pix)
                self._icon_path = ""
                self.update()
                return
        except Exception:
            pass
        self.clear_icon()

    def _dpr_pixmap(self, pix: QPixmap) -> QPixmap:
        """Upscale a freshly-loaded icon to device resolution and tag it with
        the screen's device-pixel-ratio, so it blits 1:1 on fractional DPR
        (e.g. 1.5x handheld) instead of being bilinear-upscaled by the raster.
        No-op when DPR==1 or the widget has no screen yet."""
        if pix is None or pix.isNull():
            return pix
        scr = self.screen()
        dpr = max(1.0, float(scr.devicePixelRatio())) if scr is not None else 1.0
        if dpr <= 1.0:
            return pix
        out = pix.scaled(
            int(round(pix.width() * dpr)), int(round(pix.height() * dpr)),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        out.setDevicePixelRatio(dpr)
        return out

    def clear_icon(self) -> None:
        self._icon_path = ""
        self._pixmap = None
        self.update()

    # -- name ----------------------------------------------------------
    def set_tile_name(self, name: str) -> None:
        self._name_label.setText(name)

    # -- interaction ---------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit()
        super().mousePressEvent(event)