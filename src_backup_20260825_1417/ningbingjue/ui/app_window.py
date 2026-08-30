"""Real PyQt6 overlay window — composes the 6 blocks into a live, interactive HUD.

This is the runnable GUI entry point.  It is import-only-on-Windows (PyQt6),
kept out of the core engine so the logic stays headless-testable.

Visual identity (restored to match the original ``凝冰结魄-覆盖任务栏``)
----------------------------------------------------------------------
The original is a **frameless, frosted/translucent** overlay with rounded
cards, a **neon-glow** border and a **breathing scanline** along the top edge
(``effects/scanline.ps1`` — a ~2.36 s sine breath).  This window restores
exactly that look:

* ``FramelessWindowHint`` + ``WA_TranslucentBackground`` and an explicit
  ``paintEvent`` that fills a rounded rect with a semi-transparent dark
  gradient, draws a multi-layer neon border, and a breathing scanline glow
  driven by the existing :class:`BreathingController`.
* Because we paint the background ourselves the window is *always visible*
  (the earlier frameless attempt was invisible because nothing was drawn).
* Drag-to-move from the header, a close button, and a **battery indicator**
  (mirroring the original ``Update-Battery`` 5-cell bar) in the header.
* Still an always-on-top overlay hugging the screen edge.

Performance
-----------
The blocks' data refresh (``refresh_panel`` / ``gpu_float_tick``) spawns
external processes (``wmic`` / ``ryzenadj`` / Lossless Scaling probes).  Those
run on a **single background thread** so the UI never blocks.  The UI thread
only paints (``panel.sync``) on a fast 1 s timer and again (via a queued
bridge) when the background pass completes.  ``sync`` is always executed on the
UI thread — never from the worker — so no widget is ever touched cross-thread.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from PyQt6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QEasingCurve,
    QThread,
    QTimer,
    Qt,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QRegion
from PyQt6.QtWidgets import (
    QApplication,
    QGraphicsBlurEffect,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QDoubleSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from ..platform.battery import get_battery_status
from ..core.color import get_theme_accent, set_theme_accent
from ..core.color.rgb import hex_to_color
from ..core.config import breath_speed_to_period
from ..core.shortcuts import ShortcutStore, ShortcutEngine, hotkey_from_token
from .breathing import BreathingController
from .blur_util import blur_pixmap
from .button_edit import collect_button_groups
from .edit_overlay import EditOverlay
from .gamepad import create_backend
from .interaction import InteractionController
from .keyutil import key_token_from_event
from .panels import make_panel, PerfMonPanel, PerfMonPopup
from ..core.metrics import MetricSampler
from ..platform.ryzenadj import _GpuUtilMonitor
from ..platform.undervolt import (
    HEAVY, IDLE, LIGHT,
    RyzenAdjBackend, UndervoltConfig, UndervoltController,
)

# Minimum breathing-brightness change (absolute alpha) required before we
# bother repainting the full translucent window.  Steps below this are
# imperceptible (the sine dwells near its apex/trough where it barely moves),
# so skipping them avoids needless DWM full-surface re-composites — this is
# what kept the focused CPU steady at ~10% even after the paint caching.
_BREATH_SKIP_DA = 0.02
from .faq_popup import FaqController
from ._scroll_style import apply_thin_scroll
from ..core.color.styles import apply_styles_to_buttons


# -- Windows session-lock notifier -----------------------------------------
# PyQt6 has no native "screen locked" signal, so on Windows we register for
# WTS session-change notifications and pause the breathing animation when the
# session is locked.  Off-screen / non-Windows runs never touch this.
_WTS_AVAILABLE = False
_wtsapi = None
_WM_WTSSESSION_CHANGE = 0x02B1
_WTS_SESSION_LOCK = 0x7
_WTS_SESSION_UNLOCK = 0x8

# Global "bring to front" hotkey.  Registered via the Win32 RegisterHotKey API
# and caught in winEvent (WM_HOTKEY) — no extra dependency (the bundled Python
# has no pywin32 / pynput).  The actual combo is read from the customizable
# system binding (default Win + Ctrl + F8) so the user can rebind it.
_HOTKEY_ID = 1
_MOD_WIN = 0x8
_MOD_CONTROL = 0x2
_MOD_ALT = 0x1
_MOD_SHIFT = 0x4
_VK_F8 = 0x77
_WM_HOTKEY = 0x0312

# Power-management broadcast (suspend / resume / hibernation).  PyQt6 has no
# native suspend/resume signal, so on Windows we catch WM_POWERBROADCAST in
# nativeEvent and re-initialize the gamepad subsystem on resume — otherwise the
# SDL controller handles opened before hibernation point at dead device contexts
# and the gamepad silently stops responding until the app is restarted.  (The
# existing WTS session-lock notifier does NOT cover hibernation: locking the
# screen is a different event from the OS suspending/resuming the machine.)
_WM_POWERBROADCAST = 0x0218
_PBT_APMSUSPEND = 0x0003          # system is about to suspend / hibernate
_PBT_APMRESUMESUSPEND = 0x0007    # resumed (legacy ACPI)
_PBT_APMRESUMEAUTOMATIC = 0x0012  # resumed (modern / connected standby)

if sys.platform == "win32":
    try:
        _wtsapi = ctypes.windll.wtsapi32
        _WTS_AVAILABLE = True
    except Exception:
        _WTS_AVAILABLE = False


class _RefreshWorker(QThread):
    """Runs each panel's heavy refresh off the UI thread."""

    refreshed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._tasks: list[Any] = []

    def set_tasks(self, tasks: list[Any]) -> None:
        self._tasks = tasks

    def run(self) -> None:  # pragma: no cover - exercised offscreen
        for task in self._tasks:
            try:
                task()
            except Exception:
                pass
        self.refreshed.emit()


class _FreezeWorker(QThread):
    """Runs one freeze/unfreeze op (window move + suspend/resume) off the UI
    thread.

    The window relocation shells out to PowerShell (the MScholtes VirtualDesktop
    module), which can be slow — or, for a window whose UI thread does not pump
    messages, block until the subprocess timeout.  Doing it on the UI thread is
    what made the app "非常卡，卡的几乎动不了了" during a freeze.  Here the whole
    operation runs on a throwaway thread; the result is delivered back to the UI
    thread via ``done_sig`` (queued)."""

    done_sig = pyqtSignal(object)

    def __init__(self, work: Any) -> None:
        super().__init__()
        self._work = work

    def run(self) -> None:  # pragma: no cover - exercised only in the live GUI
        try:
            res = self._work()
        except Exception:
            res = None
        self.done_sig.emit(res)


class _SyncBridge(QObject):
    """Lives on the UI thread; repaints panels when the worker signals."""

    def __init__(self, panels: list[Any], controller: Any,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._panels = panels
        self._controller = controller

    @pyqtSlot()
    def sync_all(self) -> None:
        for p in self._panels:
            try:
                p.sync()
            except Exception:
                pass
        # Keep the unified focus model in sync with dynamic panel content
        # (freeze / procmon / ls / tile lists change every refresh).
        try:
            self._controller.refresh()
        except Exception:
            pass


class GamepadIcon(QWidget):
    """Compact vector gamepad glyph for the header status area.

    Connected -> a green outlined controller (tooltip shows the pad name);
    disconnected -> hidden entirely so it costs no header space.  Drawn with
    QPainter (no icon-font / image dependency), so it stays crisp at any DPI.
    """

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._connected = False
        self._name = ""
        self.setFixedSize(22, 16)
        self.setCursor(Qt.CursorShape.WhatsThisCursor)
        self.setVisible(False)

    def set_state(self, connected: bool, name: str = "") -> None:
        self._connected = connected
        self._name = name or ""
        self.setToolTip(
            f"手柄: 已连接 ({self._name})" if (connected and self._name)
            else "手柄: 已连接" if connected
            else "手柄: 未检测到（可用键盘）")
        # Save space: only occupy the header when a pad is actually present.
        self.setVisible(connected)
        self.update()

    def paintEvent(self, ev: Any) -> None:  # noqa: D401 - Qt override
        if not self._connected:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._draw(p)

    def _draw(self, p: QPainter) -> None:
        """Draw the controller glyph onto ``p`` (injected painter so the same
        routine is unit-testable without a live widget)."""
        col = QColor(91, 227, 139)  # clear "connected" green
        w, h = self.width(), self.height()

        # --- controller body outline (rounded shell + two grips) ----------
        body = QPainterPath()
        body.moveTo(4, 3)
        body.lineTo(w - 4, 3)
        body.quadTo(w - 1, 3, w - 2, 7)
        body.quadTo(w - 3, 10, w - 4, 14)        # right side down
        body.quadTo(w - 5, 16, w - 9, 15)         # right grip bottom
        body.quadTo(w - 12, 14, w - 13, 10)       # right grip inner up
        body.lineTo(13, 10)                        # centre bottom (flat)
        body.quadTo(12, 14, 9, 15)                 # left grip inner bottom
        body.quadTo(5, 16, 4, 14)                  # left grip outer bottom
        body.quadTo(3, 10, 2, 7)                   # left side up
        body.quadTo(1, 3, 4, 3)                    # close
        pen = QPen(col)
        pen.setWidthF(1.6)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(body)

        # --- D-pad cross (left) ------------------------------------------
        cx, cy = 7.0, 8.0
        arm = 2.4
        p.setBrush(col)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(QRectF(cx - arm, cy - 0.8, arm * 2, 1.6))
        p.drawRect(QRectF(cx - 0.8, cy - arm, 1.6, arm * 2))

        # --- four face buttons (right), diamond layout ------------------
        bx, by = w - 8.0, 8.0
        for dx, dy in ((0, -3), (3, 0), (0, 3), (-3, 0)):
            p.drawEllipse(QPointF(bx + dx, by + dy), 1.3, 1.3)


class _BatteryBar(QLabel):
    """Header battery readout — mirrors the original 5-cell energy bar."""

    def __init__(self) -> None:
        super().__init__()
        self.set_none()

    def set_none(self) -> None:
        self.setText("🔌 N/A")
        self.setStyleSheet("color: #6b7689; font-size: 12px;")

    def set_status(self, pct: int, charging: bool, available: bool) -> None:
        if not available:
            self.set_none()
            return
        n = max(0, min(5, round(pct / 20)))
        bar = "▮" * n + "▯" * (5 - n)
        icon = "⚡" if charging else "🔋"
        self.setText(f"{icon} {pct}% {bar}")
        color = "#4be38a" if pct >= 60 else ("#ffd166" if pct >= 20 else "#ff6b6b")
        self.setStyleSheet(f"color: {color}; font-size: 12px;")


class _TileBlockPanelProxy:
    """Thin proxy that exposes a single ``ExtraTileBlock`` as an instance
    compatible with :class:`TilePanel`.

    Each extra tile block becomes its own panel in the HUD, complete with its
    own title, accent colour, and tile grid — mirroring the PS1's
    ``New-TileBlockPanel`` which creates a separate ``RoundedPanel``.
    """

    def __init__(self, extra_block: Any, parent: Any) -> None:
        self.tiles = list(extra_block.tiles)
        self.extra_blocks: list = []
        self._parent = parent
        self._extra = extra_block  # back-reference for in-place HUD refresh
        # Copy block-level attrs onto tiles[0] so _sync_block_title can
        # read them just like the base panel.
        if self.tiles:
            self.tiles[0].block_name = extra_block.block_name
            self.tiles[0].show_block_title = extra_block.show_block_title

    def launch(self, tile: Any) -> None:
        """Delegate launch to the real TileBlock."""
        return self._parent.launch(tile)

    # Stubs required by TilePanel.sync / _rebuild / nav_items.
    @property
    def pm(self) -> Any:
        return getattr(self._parent, "pm", None)

    @property
    def ui(self) -> dict:
        return getattr(self._parent, "ui", {})


class _FocusScrim(QWidget):
    """Full-window dark overlay painted on top of every HUD child while the
    window is *not* the active window, so the whole page visibly 'recedes' and
    the active / inactive state is unmistakable.

    It is ``WA_TransparentForMouseEvents`` so it never intercepts clicks (you
    can still interact with the HUD the moment you click back into it).  Its
    alpha is animated by the shared ``_dim_timer`` (see ``_OverlayWindow``).
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._alpha = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)

    def set_alpha(self, a: float) -> None:
        self._alpha = max(0.0, min(1.0, float(a)))

    def paintEvent(self, ev: Any) -> None:  # noqa: N802 - Qt override
        if self._alpha <= 0.002:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, int(self._alpha * 255)))
        # Clip to the window's rounded outline (radius must match the 16px
        # outer corner drawn in ``_OverlayWindow.paintEvent``) so the dark
        # scrim never bleeds past the rounded window corners.
        p.drawRoundedRect(self.rect(), 16, 16)
        p.end()


class _OverlayWindow(QWidget):
    """Top-most, frosted overlay holding one interactive panel per block."""

    def __init__(self, app: Any) -> None:
        super().__init__()
        self._app = app

        # --- Frosted / translucent skin ---------------------------------
        # On a real display we go frameless + WA_TranslucentBackground and
        # paint the background ourselves (in paintEvent).  The offscreen test
        # surface (QImage-backed) cannot paint a translucent frameless window
        # with a complex child tree, so headless runs fall back to a solid
        # window — visually identical code path to the pre-skin version.
        app_qt = QApplication.instance()
        self._offscreen = bool(app_qt) and app_qt.platformName() == "offscreen"
        # App identity: the taskbar / Alt-Tab entry reads 凝冰结魄 (not the raw
        # python script name).  Set it once on the shared QApplication.
        if app_qt is not None:
            try:
                app_qt.setApplicationName("凝冰结魄")
                app_qt.setApplicationDisplayName("凝冰结魄")
            except Exception:
                pass

        # Skin attributes are ALWAYS initialised (defensive): the overlay's
        # paintEvent / rebuild() may touch them even under offscreen, and we
        # must never raise AttributeError there.  Offscreen never uses the
        # custom frosted paint (see paintEvent), so these values are inert.
        self._accent = QColor(127, 209, 255)  # neon cyan (#7fd1ff)
        self._breath_alpha = 1.0
        self._bg = QColor(16, 16, 16, 235)
        # Cached paint layers (see _rebuild_bg_cache / _rebuild_glow_cache):
        # the full-window bg fill and the breathing neon border + scanline are
        # pre-rendered once (on geometry / theme / colour change) and blitted
        # every breath frame, instead of re-stroking 4 full-window rounded
        # paths in software raster — that per-frame stroking pinned the
        # foreground CPU at ~10%.
        self._bg_cache = None
        self._glow_cache = None
        # Last alpha value we actually painted at.  Used to coalesce
        # imperceptible breathing steps (see _on_breath) so we don't re-composite
        # the full translucent window on frames the user can't see change.
        self._last_paint_alpha = None

        # -- unfocus dim state --------------------------------------------
        # When the window loses focus but stays visible, the breathing light
        # keeps playing (it is NOT paused) but the glow colour brightness is
        # eased toward ``_DIM_UNFOCUSED`` so the neon border + frosted tray
        # glow dim to a "sleeping" state.  ``_glow_dim`` is the live value
        # (1.0 = focused, ~0.3 = dim) animated by ``_dim_timer``.
        self._glow_dim = 1.0
        self._dim_target = 1.0
        self._last_tray_dim = 1.0
        self._DIM_UNFOCUSED = 0.3  # glow brightness floor when unfocused-visible
        self._dim_timer = QTimer(self)
        self._dim_timer.setInterval(30)
        self._dim_timer.timeout.connect(self._dim_tick)

        # Unfocus page-dim scrim: a separate full-window dark overlay (the
        # ``_FocusScrim`` widget) whose alpha is eased toward ``_scrim_target``
        # by the same ``_dim_timer``.  0.45 ≈ "half-transparent black veil"
        # when another app is on top; 0 when focused.  The breathing glow also
        # drops to ``_DIM_UNFOCUSED`` (double dim → unmistakable inactive look).
        self._SCRIM_UNFOCUSED = 0.45
        self._scrim_alpha = 0.0
        self._scrim_target = 0.0
        self._scrim = None
        if self._offscreen:
            # Headless test surface: a frameless + WA_TranslucentBackground
            # window with a complex child tree crashes the offscreen raster
            # backend (Python exits 127, no traceback).  Fall back to a solid
            # always-on-top window — visually identical code path to the
            # pre-skin version that passed the full test suite on this host.
            self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
            self.setStyleSheet("QWidget { background: #0e1117; }")
        else:
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
            )
            # Opaque window: DROP WA_TranslucentBackground so the DWM no longer
            # re-composites this surface against the desktop every frame (that
            # per-frame full-window alpha blend against the wallpaper was the
            # dominant foreground-CPU cost).  The frosted look is preserved
            # because _theme_bg() now paints a fully-opaque dark fill (alpha
            # 255) and a setMask() rounded region keeps the 16px corners.
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
            self._bg = self._theme_bg()

        # --- Geometry: hug the screen edge as a tall right rail ----------
        # Height fills the available screen height (no 900px cap) so it scales
        # from a 768p laptop up to a 1440p desktop; re-fit on resolution /
        # monitor changes too (see _fit_to_screen + screenChanged wiring).
        self.setMinimumWidth(340)
        self._fit_to_screen()
        # Clip to a rounded rectangle up front so the corners are correct even
        # if the first real resizeEvent is a no-op (see _apply_window_mask).
        self._apply_window_mask()
        # Re-fit when the window is dragged onto another monitor.
        try:
            self.screenChanged.connect(self._fit_to_screen)  # type: ignore[attr-defined]
        except Exception:
            pass
        # Also re-fit when the *same* screen's DPI/scale changes (user drags the
        # Windows "缩放" slider while the HUD is open). That only fires
        # QWindow.devicePixelRatioChanged — not screenChanged — so wire it too.
        self._wire_dpr_change()

        # --- Header (drag handle + battery + buttons) -------------------
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)
        self.outer = outer

        header = QHBoxLayout()
        title = QLabel("凝冰结魄")
        title.setStyleSheet(
            "color: #eaf2ff; font-size: 15px; font-weight: bold; padding-bottom: 2px;"
        )
        header.addWidget(title, 1)

        self._batt = _BatteryBar()
        header.addWidget(self._batt)

        # Gamepad connection indicator: a compact vector gamepad glyph (no
        # text).  Connected -> green glyph (tooltip shows the pad name);
        # disconnected -> hidden entirely to save header space.  Driven by the
        # InteractionController's on_connection_changed callback.
        self._gp_status = GamepadIcon()
        header.addWidget(self._gp_status)

        self._edit_btn = QPushButton("⚙️")
        self._edit_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.10); color: #dfe6f0; "
            "border: 1px solid rgba(255,255,255,0.15); border-radius: 7px; "
            "padding: 4px 10px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.18); }")
        self._edit_btn.clicked.connect(self._open_editor)
        header.addWidget(self._edit_btn)

        self._min_btn = QPushButton("−")  # 普通减号 U+2212：最小化到系统托盘
        self._min_btn.setStyleSheet(
            "QPushButton { background: rgba(127,209,255,0.12); color: #a9d8ff; "
            "border: 1px solid rgba(127,209,255,0.28); border-radius: 7px; "
            "padding: 4px 9px; }"
            "QPushButton:hover { background: rgba(127,209,255,0.22); }")
        self._min_btn.clicked.connect(self._minimize_to_tray)
        header.addWidget(self._min_btn)

        self._exit_btn = QPushButton("✕")  # 真正的退出入口，触发 closeEvent 优雅收尾
        self._exit_btn.setStyleSheet(
            "QPushButton { background: rgba(255,90,90,0.16); color: #ff9b9b; "
            "border: 1px solid rgba(255,90,90,0.3); border-radius: 7px; "
            "padding: 4px 9px; }"
            "QPushButton:hover { background: rgba(255,90,90,0.3); }")
        self._exit_btn.clicked.connect(self._quit_app)
        header.addWidget(self._exit_btn)
        outer.addLayout(header)

        # --- System tray (the − button hides here; ✕ still fully quits) ---
        self._tray = None
        self._init_tray()

        # --- Scroll host for the 6 panels (HUD) -------------------------
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        # Keep the QScrollArea *frame* itself transparent.  The viewport gets
        # its OWN semi-transparent dark rounded background via a dedicated
        # stylesheet (see below).  NOTE: a plain `background: transparent` on
        # the viewport only makes it composite straight to the desktop under a
        # WA_TranslucentBackground top-level window — which is exactly why the
        # earlier window-level paintEvent tray never showed.
        # No reserved gutter: the slim scrollbar only appears while the content
        # is actually scrollable (temporary + thin, never steals page width).
        apply_thin_scroll(scroll, always_on=False)
        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        inner_layout = QVBoxLayout(inner)
        # Cards keep their original height and stack top-down; any leftover
        # vertical space is absorbed by a trailing stretch so panels are NOT
        # stretched to fill the scroll viewport.
        inner_layout.setSpacing(8)
        inner_layout.setContentsMargins(0, 0, 0, 0)
        scroll.setWidget(inner)
        self.inner = inner
        self.inner_layout = inner_layout
        self._scroll = scroll
        # CRITICAL: cap the scroll area's OWN minimum height.  With
        # setWidgetResizable(True) the QScrollArea's minimumSizeHint reports the
        # full height of every card stacked (e.g. ~728px on a 7" 1080p panel at
        # 150% DPI).  The top-level layout then adopts that as the WINDOW's
        # minimum size and grows the window past the physical screen — silently
        # overriding our setMaximumHeight() ceiling.  Pinning a small minimum
        # height lets the layout shrink the window to the screen height, with
        # the overflow handled by the scrollbar instead of overflowing off-screen.
        scroll.setMinimumHeight(160)
        # Symmetric WIDTH pin: with setWidgetResizable(True) the scroll area's
        # minimumSizeHint also reports the full *width* of the widest card (e.g.
        # a long card title / process name).  Without this pin that width
        # propagates up as the window's minimum width and pushes the frameless
        # window past the screen edge.  Pinning a small minimum width lets the
        # layout shrink the window to the screen width, with the overflow
        # handled by clipping (or word-wrapped titles) instead of overflowing.
        # NOTE: the resizeEvent width clamp is the final hard ceiling; this pin
        # is what stops the *layout* from adopting an oversized minimum.
        scroll.setMinimumWidth(340)

        # --- Scroll-smoothing ------------------------------------------------
        # The breathing light repaints the WHOLE translucent top-level window
        # every 30 ms (frosted fill + neon border + pre-blurred tray glow).
        # That full-window repaint runs on the same GUI thread as the scroll
        # viewport's repaints, so a fast wheel flick makes the two contend and
        # the page feels laggy/stuttery.  Two mitigations:
        #   1) WA_StaticContents on the viewport: scrolling blits the already-
        #      painted pixels and only repaints the newly-exposed strip instead
        #      of the entire viewport.
        #   2) While the user is actively scrolling, DEFER the breathing repaint
        #      so the GUI thread is free for scroll work; it resumes ~150 ms
        #      after the last wheel / arrow input (negligible on a 2.36 s breath).
        self._scroll.viewport().setAttribute(
            Qt.WidgetAttribute.WA_StaticContents, True)
        self._scroll_active_until = 0.0
        self._glow_dim_dirty = False
        self._scroll.viewport().installEventFilter(self)
        # 方向键 / PageUp-Down 的「切卡」拦截必须挂在整个 app 层级，而不仅
        # 仅是 viewport：鼠标点卡片后焦点常落在卡片内的按钮子控件上，此时
        # 键事件目标是子控件，视口级过滤器收不到，方向键就被 QScrollArea
        # 当「滚页面」吞掉（即「卡片发光了、方向键却滚页面」）。app 级过
        # 滤器能拦截任意焦点下的键事件，与手柄轮询走同一条控制器通道。
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        # Card tray background — a translucent rounded panel painted directly in
        # paintEvent OVER the breathing-light window, so the breathing glow shows
        # through it (frosted by ``tray_blur``).  Colour / opacity / border / blur
        # are user-customisable (see BreathingEditPanel + apply_tray).  The
        # blurred breathing glow behind the tray is pre-rendered once into an
        # offscreen pixmap (``_tray_glow_blur``) and only re-rendered when the
        # geometry / blur / breathing colour changes — never per breath frame.
        self._tray_blur = int(getattr(self._app.config_manager.config,
                                      "tray_blur", 18) or 18)
        self._tray_rgb = (14, 17, 23)
        self._tray_alpha = 219
        self._tray_border = True
        self._tray_glow_blur: Any = None
        self._glow_timer = QTimer(self)
        self._glow_timer.setSingleShot(True)
        self._glow_timer.timeout.connect(self._rebuild_tray_glow)

        # Fixed dark rounded card tray behind the HUD cards — and the empty
        # trailing area when a page has only a few cards.  Painted in paintEvent
        # (not a child widget) so it reliably composites OVER the breathing glow.
        # Colour / opacity / border / blur are user-customisable
        # (see BreathingEditPanel + apply_tray); read the persisted values now.
        _tray_cfg = self._app.config_manager.config
        self.apply_tray(
            str(getattr(_tray_cfg, "tray_color", "0e1117") or "0e1117"),
            int(getattr(_tray_cfg, "tray_opacity", 86) or 86),
            bool(getattr(_tray_cfg, "tray_border", True)),
            int(getattr(_tray_cfg, "tray_blur", 18) or 18),
        )

        self.panels: list[Any] = []
        # 独立浮动性能监控弹窗（全局唯一，由性能监控卡片右上角按钮切换）
        self._perfmon_popup: Any = None
        qapp = QApplication.instance()
        if qapp is not None:
            qapp.aboutToQuit.connect(self._close_perfmon_popup)
        # When the button-style editor is open we instantiate EVERY page's
        # panels (not just the current one) so the style editor can list and
        # style buttons that live on other pages.  The HUD is hidden behind the
        # editor overlay, so stacking all pages is invisible; the flag is reset
        # to False on editor close (see _close_editor) which rebuilds the
        # current page only.
        self._editor_all_pages: bool = False
        self._controller = InteractionController(
            self, self.inner, gamepad=create_backend(),
            config=self._app.config_manager.config)
        # Number keys 1..N jump to a top-level page; the gamepad connection
        # status drives the header indicator + tray tooltip.
        self._controller.on_goto_page = self._goto_page
        # LB / RB switch top-level pages (the user's "翻页" expectation).
        self._controller.on_prev_page = lambda: self._app.switch_page(-1)
        self._controller.on_next_page = lambda: self._app.switch_page(1)
        self._controller.on_connection_changed = self._on_gp_connection_changed
        # 主页面：焦点切换到另一个功能卡片时，关闭已打开的疑惑解答弹窗。
        self._controller.on_card_focus_change = self._on_card_focus_changed
        # Shell gamepad actions: Select toggles the layout editor, Start focuses
        # the window.  (_open_editor already toggles, so it serves both enter
        # and exit.)  Wired here so the controller can call back into the shell.
        self._controller.on_toggle_layout = self._open_editor
        self._controller.on_focus_window = self._focus_from_start
        self._build_panels()
        self._controller.refresh()

        # --- In-window edit overlay (stacked over the HUD) --------------
        # Mirrors the original's full-window $pnlEdit: the editor lives inside
        # the main window, never a separate dialog.
        self._edit_overlay = EditOverlay(self._app, self)
        self._body = QStackedWidget(self)
        self._body.addWidget(scroll)
        self._body.addWidget(self._edit_overlay)
        outer.addWidget(self._body, 1)

        # --- Page indicator bar (top, just below the header) ------------
        # Shows the current page (gold highlight) + one numeric button per
        # page (no ‹/› arrows).  Switching pages rebuilds the visible panels
        # and re-highlights the bar (see _on_page_switched / _goto_page).
        self._page_bar: Any = None
        self._build_page_bar()
        # Gamepad/keyboard page switches (app.switch_page / goto_page) and the
        # page-bar buttons converge here to rebuild the visible page.
        self._app._page_changed_cb = self._on_page_switched
        self._controller.set_edit_overlay(self._edit_overlay)
        self._edit_overlay.close_requested.connect(self._close_editor)
        # Gamepad requests route to the active editor sub-mode (no-op in HUD).
        self._controller.request_save.connect(self._gp_save)
        self._controller.request_cancel.connect(self._gp_cancel)
        self._controller.request_delete.connect(self._gp_delete)
        self._controller.start_gamepad()
        # Apply the persisted active button style (L2) + bound L1 presets to the
        # initial set of HUD buttons (mirrors PS1 Set-GlobalButtonStyle on boot).
        self._apply_button_styles()

        # --- Shortcut buttons (gamepad / keyboard, long & short press) ------
        # The engine keeps listening in the background (see _ensure_gamepad),
        # so gamepad shortcuts fire even while a game is in front; keyboard
        # shortcuts fire only while this window is the foreground window.
        _sc_path = None
        try:
            _cm = getattr(self._app, "config_manager", None)
            if _cm is not None and getattr(_cm, "path", None):
                _sc_path = os.path.join(
                    os.path.dirname(os.path.abspath(_cm.path)), "shortcuts.json")
        except Exception:
            _sc_path = None
        self._shortcut_store = ShortcutStore(_sc_path)
        self._shortcut_store.load()
        self._shortcut_engine = ShortcutEngine(self._shortcut_store)
        self._shortcut_engine.set_inapp_handler(self._run_inapp_action)
        self._controller.shortcut_engine = self._shortcut_engine
        # Push the customizable system (shell) bindings onto the controller so
        # Select / Start / the global hotkey follow the user's remap, not the
        # original hard-coded defaults.
        self._controller.set_system_bindings(self._shortcut_store.system)
        self._shortcut_editor: Any = None

        # Build the pre-blurred breathing-glow layer for the card tray once the
        # window is on screen (geometry is final then).  showEvent also triggers
        # this; the singleShot is a safety net for the very first paint.
        if not self._offscreen:
            QTimer.singleShot(0, self._rebuild_tray_glow)

        # --- Breathing scanline (original ~2.36 s sine breath) ----------
        _bcfg = self._app.config_manager.config
        _period = getattr(_bcfg, "breath_period_s", None)
        if not _period or _period <= 0:
            # Backwards-compat: migrate a legacy breath_speed to a period.
            _speed = getattr(_bcfg, "breath_speed", 50)
            _period = breath_speed_to_period(int(_speed)) if _speed else 2.36
        _max = getattr(_bcfg, "breath_max_brightness", 1.0) or 1.0
        self._breath = BreathingController(period_s=_period, max_brightness=_max)
        self._breath.on_scanline = self._on_breath
        # Custom glow colour (empty string => follow the theme accent).
        _raw_color = getattr(_bcfg, "breath_color", "") or ""
        self._breath_color = None
        if _raw_color:
            try:
                self._breath_color = hex_to_color(
                    _raw_color[1:] if _raw_color.startswith("#") else _raw_color)
            except Exception:
                self._breath_color = None
        # The breathing colour IS the canonical theme color (single accent
        # knob).  Publish it to the color subsystem and mirror it onto the
        # window neon border / tray icon so they all follow one source.
        set_theme_accent(_raw_color)
        _ac = get_theme_accent()
        self._accent = QColor(_ac.r, _ac.g, _ac.b)
        # Tray icon was rendered in _init_tray() (above) while _accent still
        # held its defensive default; re-render now that the real accent is known.
        _tray = getattr(self, "_tray", None)
        if _tray is not None:
            _tray.setIcon(self._make_tray_icon())
        if not self._offscreen:
            self._breath.start_scanline()
        # EditOverlay was built before this block, so refresh its outer border
        # now that the accent is known (it would otherwise stay default cyan).
        ov = getattr(self, "_edit_overlay", None)
        if ov is not None:
            ov._apply_window_border()
        # EditOverlay + panels were built before the breathing colour was
        # known, so re-skin every block/title with the accent now.
        self._refresh_all_accents()

        # Register for Windows session-lock notifications so the breathing
        # animation pauses when the screen is locked (handled in winEvent).
        self._wts_hwnd = None
        self._register_session_lock_notifier()
        # Global "bring to front" hotkey (customizable via the system binding;
        # default Win + Ctrl + F8).  Handled in winEvent.
        self._register_global_hotkey(
            self._shortcut_store.system.get("bring_to_front", {}).get("keyboard"))

        # --- 疑惑解答弹窗（解耦模块，见 faq_popup.py）------------------
        # 触发方式：鼠标点卡片标题、键盘 F1/H、手柄 L3（左摇杆按下）。
        # 切换页面自动关闭；主窗口拖动时实时跟随。
        self._faq = FaqController(self)
        self._controller.on_help = self._faq.toggle_for_current
        # 关联窗口登记：疑惑解答弹窗 / 按钮列表弹窗 是主程序的「从属窗口」，
        # 它们获得焦点时主窗口不应变暗或停呼吸（见 _is_own_popup_active）。
        self._associated_windows: list = []
        self.register_associated_window(self._faq._popup)

        # --- Battery poll ------------------------------------------------
        self._batt_timer = QTimer(self)
        self._batt_timer.timeout.connect(self._update_battery)
        self._batt_timer.start(2000)
        self._update_battery()

        # --- Background refresh worker (no parent -> we own its lifetime) -
        self._worker = _RefreshWorker()
        self._bridge = _SyncBridge(self.panels, self._controller, self)
        self._worker.refreshed.connect(self._bridge.sync_all, Qt.ConnectionType.QueuedConnection)

        # Freeze/unfreeze window relocation runs on its own throwaway thread so
        # a slow / hung PowerShell move never freezes the overlay (the user's
        # "冻结进程时非常卡").  The FreezeBlock stays UI-framework agnostic; we
        # just hand it a runner.
        self._freeze_workers: list[_FreezeWorker] = []
        freeze = self._app.blocks.get("freeze")
        if freeze is not None and hasattr(freeze, "set_async_runner"):
            freeze.set_async_runner(self._run_freeze_async)
            # Repaint the frozen list the instant a freeze/unfreeze completes,
            # instead of waiting for the next 1 s _fast tick (otherwise a freshly
            # frozen process can be appended and then dropped by _validate_frozen
            # before the screen ever shows it).
            if hasattr(freeze, "set_on_change"):
                freeze.set_on_change(self._bridge.sync_all)
            # 解冻成功后自动最小化凝冰结魄本体：把宿主窗口的最小化钩子注入
            # FreezeBlock，由它在解冻完成时调用（托盘/任务栏最小化）。
            if hasattr(freeze, "set_minimize_self_callback"):
                freeze.set_minimize_self_callback(self._minimize_to_tray)
            # 冻结成功后把焦点抢回凝冰结魄本体：冻结会最小化目标窗口（suspend
            # 前 ShowWindow(SW_MINIMIZE)），最小化前台窗口后 Windows 把焦点
            # 交给别的窗口——置顶(WindowStaysOnTopHint) ≠ 持焦点，HUD 于是
            # 失焦变暗。注入宿主窗口的重聚焦钩子，由 FreezeBlock 在冻结完成
            # 回调里调用（仅成功路径，解冻路径不触发）。
            if hasattr(freeze, "set_refocus_callback"):
                freeze.set_refocus_callback(self._refocus_after_freeze)

        self._fast = QTimer(self)
        self._fast.timeout.connect(self._bridge.sync_all)
        self._fast.start(1000)

        self._slow = QTimer(self)
        self._slow.timeout.connect(self._do_refresh)
        self._slow.start(2000)
        QTimer.singleShot(300, self._do_refresh)

        # --- 负载门控降压控制器（共享 MetricSampler）-------------------
        # 注意：它的 tick 用**专属定时器**，刻意不进 _pause_aux 的暂停集——
        # 否则 app 失焦（游戏前台）时 _slow 被停，降压反而不跑了。
        self._init_undervolt()

        # Drag-to-move bookkeeping (header is the drag handle).
        self._drag_offset: QPoint | None = None

        # --- Unfocus scrim (whole-window dim when not the active window) ---
        # Built last so it is the top-most child; keep it raised above the
        # content in resizeEvent too.  Skipped on the offscreen test surface.
        if not self._offscreen:
            self._scrim = _FocusScrim(self)
            self._scrim.setGeometry(self.rect())
            self._scrim.raise_()

    # -- theme helpers --------------------------------------------------
    def _theme_bg(self) -> QColor:
        """App background colour from the active theme.  Painted fully opaque
        (alpha 255) — the window is no longer translucent, so the DWM does NOT
        re-composite it against the desktop every frame; the frosted feel comes
        from this dark fill plus the cards / breathing glow layered on top."""
        theme = getattr(self._app.config_manager.config, "theme", None)
        csv = getattr(theme, "bg_app", "16,16,16") if theme else "16,16,16"
        parts = [p.strip() for p in csv.split(",")]
        if len(parts) == 3:
            try:
                return QColor(int(parts[0]), int(parts[1]), int(parts[2]), 255)
            except ValueError:
                pass
        return QColor(16, 16, 16, 255)

    # -- breathing ------------------------------------------------------
    def _on_breath(self, alpha: float) -> None:
        self._breath_alpha = alpha
        if self._offscreen:
            return
        # When this window is NOT the foreground window, the breathing light is
        # dimmed to the unfocus floor and covered by the black scrim, so it is
        # effectively invisible.  Skip the expensive full-window repaint
        # entirely and freeze the last dimmed frame.  The heavier periodic
        # timers (_slow / _batt_timer) are stopped by _pause_heavy_aux when
        # merely unfocused, but the lightweight 1 s display-refresh timer
        # (_fast) keeps running so the perfmon / read-only cards stay live
        # while a game is in front (the underlying MetricSampler samples
        # continuously regardless of focus, so the cost is just a few QLabel
        # text updates).  The breath phase keeps advancing above, so it resumes
        # seamlessly on refocus; _dim_tick still repaints during the brief
        # unfocus fade so the dim-down is visible.
        if not self.isActiveWindow() and not self._is_own_popup_active():
            return
        # Breathing now keeps animating during scroll.  The old guard that
        # deferred the repaint until the page settled (see _mark_scrolling) is
        # gone: the breath frame is only two cached-pixmap blits + a tray clip
        # blit (_glow_cache / _tray_glow_blur modulated by _breath_alpha), so
        # its cost equals the resting breath cost the user already tolerates.
        # Freezing it during scroll only produced the "breathing pauses / jumps
        # on scroll" artifact with no real perf win (the 9-17% unfocus spike it
        # once guarded against came from per-frame path strokes, long since
        # replaced by cached blits).  The expensive *focus-dim* re-blur is still
        # deferred during scroll in _dim_tick via _glow_dim_dirty.
        # If the focus-dim animation was deferred while scrolling, rebuild the
        # pre-blurred tray glow now that the GUI thread is free again.
        if getattr(self, "_glow_dim_dirty", False):
            self._glow_dim_dirty = False
            self._rebuild_tray_glow()
            return
        # Coalesce imperceptible breathing steps at ANY focus state (focused
        # or unfocused).  The scanline sine dwells near its peak/trough where
        # the brightness barely moves; repainting the whole translucent window
        # then only makes the DWM re-composite the full surface for a change
        # nobody can see.  We only repaint once the brightness has moved >=
        # _BREATH_SKIP_DA (2% — invisible, trims only the flat apex/trough
        # dwell, never the steep mid-rise).  The dim / unfocus *transition*
        # still repaints every frame because it is driven directly by
        # _dim_tick (which calls self.update() and bypasses this skip), and
        # _start_dim resets _last_paint_alpha so the first transition frame is
        # never swallowed.
        last = getattr(self, "_last_paint_alpha", None)
        if last is not None and abs(alpha - last) < _BREATH_SKIP_DA:
            return
        self._last_paint_alpha = alpha
        self.update()

    def _mark_scrolling(self) -> None:
        """Note recent scroll input.

        Breathing no longer freezes during scroll — ``_on_breath`` keeps
        animating.  This timestamp is still used by ``_dim_tick`` to defer the
        *expensive focus-dim re-blur* until the page settles (see
        ``_glow_dim_dirty``), so scroll input stays cheap.  Called from the
        viewport event filter on wheel / arrow-key events.
        """
        self._scroll_active_until = time.monotonic() + 0.15

    def eventFilter(self, obj: Any, event: Any) -> bool:  # noqa: N802 - Qt
        et = event.type()
        if et == QEvent.Type.Wheel:
            # 滚轮：仅标记「正在滚动」以暂缓呼吸灯整窗重绘（性能优化），
            # 不消费滚轮事件，页面照常滚动。
            self._mark_scrolling()
            return False
        if et == QEvent.Type.KeyPress:
            k = event.key()
            _ARROWS = (Qt.Key.Key_Up, Qt.Key.Key_Down,
                       Qt.Key.Key_Left, Qt.Key.Key_Right)
            _PAGES = (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown)
            if k in _ARROWS or k in _PAGES:
                self._mark_scrolling()
                # 只管本 HUD 处于前台、且事件目标属于本窗口层级的情况。
                if not self.isActiveWindow():
                    return False
                if not isinstance(obj, QWidget) or not self.isAncestorOf(obj):
                    return False
                if k in _ARROWS:
                    # 方向键：无论焦点落在卡片、卡片内按钮、滚动区视口还是无
                    # 焦点控件，都必须先过控制器 (InteractionController.
                    # handle_key)，与手柄轮询走同一通道，绝不能被 QScrollArea
                    # 当成「滚动页面」半路吞掉——这就是「卡片发光、方向键却
                    # 滚页面」的根因。
                    #
                    # 编辑态默认放行方向键（交给控件），但布局编辑器的卡片列表
                    # 放在 QScrollArea 里，方向键会被它吃掉去滚页面、焦点永远切
                    # 不动。所以布局编辑器必须在此拦截方向键（和 HUD / 手柄一
                    # 致）；其它编辑态仍放行，避免误伤列表/滑块等原生方向键交
                    # 互。文本框（含数字步进框）始终放行，保证可输入。
                    if self._controller._edit_mode:
                        _ov = getattr(self, "_edit_overlay", None)
                        _mode = (_ov.mode() if _ov is not None and
                                 callable(getattr(_ov, "mode", None)) else None)
                        if _mode != "layout":
                            return False
                        fw = self.focusWidget()
                        if isinstance(fw, (QLineEdit, QTextEdit, QPlainTextEdit,
                                           QSpinBox, QDoubleSpinBox)):
                            return False
                    else:
                        if (getattr(self, "_faq", None) is not None
                                and self._faq.is_open()):
                            return False
                        fw = self.focusWidget()
                        if isinstance(fw, (QLineEdit, QTextEdit, QPlainTextEdit,
                                           QSpinBox, QDoubleSpinBox)):
                            return False
                    try:
                        self._controller.handle_key(event)
                    except Exception:
                        # Backstop: never let a navigation error abort the app
                        # from inside the app-level event filter.
                        pass
                    return True
                # PageUp/PageDown：编辑态交给滚动区翻页；HUD 交给控制器跳面板。
                if self._controller._edit_mode:
                    return False
                try:
                    consumed = self._controller.handle_key(event)
                except Exception:
                    consumed = False
                if consumed:
                    return True
        return False

    # -- unfocus dim (breathing stays alive, glow brightness eases down) -
    @staticmethod
    def _dim_color(c: "QColor", f: float) -> "QColor":
        """Scale a colour's brightness toward black by factor ``f`` (0..1).

        Hue is preserved; only the RGB magnitude shrinks, so the neon border
        / scanline / frosted tray glow fade without shifting colour.
        """
        f = max(0.0, min(1.0, float(f)))
        return QColor(int(c.red() * f), int(c.green() * f),
                      int(c.blue() * f), c.alpha())

    def _start_dim(self, target: float) -> None:
        """Animate ``_glow_dim`` toward ``target`` (1.0 focused, ~0.3 dim)."""
        self._dim_target = max(0.0, min(1.0, float(target)))
        # Invalidate the paint-coalescing tracker so the focus/unfocus
        # transition is never swallowed by an imperceptible-step skip.
        self._last_paint_alpha = None
        if not self._dim_timer.isActive():
            self._dim_timer.start()

    def _dim_tick(self) -> None:
        cur = self._glow_dim
        nxt = cur + (self._dim_target - cur) * 0.2  # ~0.5s ease to target
        if abs(self._dim_target - nxt) < 0.01:
            nxt = self._dim_target
        self._glow_dim = nxt
        if nxt != cur and not self._offscreen:
            # The cached neon layer is baked at the current dim; drop it so the
            # next paint rebuilds it at the new brightness (only during the
            # brief 0.5s focus/unfocus fade, never in steady state).
            self._glow_cache = None
            # While scrolling, defer both the full-window repaint and the
            # expensive re-blur so the GUI thread stays free for scroll work;
            # the glow catches up once the page settles (see _on_breath).
            if time.monotonic() < self._scroll_active_until:
                self._glow_dim_dirty = True
            else:
                self.update()
                # The tray glow is a re-blurred pixmap — rebuilding it every
                # 30ms for 0.5s is wasteful, so throttle to ~6 steps across
                # the fade.
                if abs(getattr(self, "_last_tray_dim", 1.0) - nxt) >= 0.05:
                    self._rebuild_tray_glow()
                    self._last_tray_dim = nxt

        # Ease the unfocus scrim alpha toward its target in lock-step.
        scrim = getattr(self, "_scrim", None)
        scur = self._scrim_alpha
        snxt = scur + (self._scrim_target - scur) * 0.2
        if abs(self._scrim_target - snxt) < 0.01:
            snxt = self._scrim_target
        self._scrim_alpha = snxt
        if scrim is not None and snxt != scur:
            scrim.set_alpha(snxt)
            scrim.update()

        if nxt == self._dim_target and snxt == self._scrim_target:
            self._dim_timer.stop()

    # -- associated (owned) helper popups --------------------------------
    def register_associated_window(self, w: Any) -> None:
        """Register a docked helper popup (FAQ / button-list) as an *owned*
        window.  When such a window is the foreground window, the HUD treats
        itself as still focused — no unfocus dim, breathing keeps running."""
        if w is None:
            return
        lst = getattr(self, "_associated_windows", None)
        if lst is None:
            lst = []
            self._associated_windows = lst
        if w not in lst:
            lst.append(w)

    def _is_own_popup_active(self) -> bool:
        """True when the foreground window is one of this app's own docked
        helper popups (FAQ / button-list).

        In that case the main HUD must keep its full glow + breathing instead
        of dimming to the unfocused look: the popup is our own UI docked beside
        the HUD (a Tool window parented to it), not a separate third-party app.
        """
        if getattr(self, "_offscreen", False):
            return False
        lst = getattr(self, "_associated_windows", None)
        if not lst:
            return False
        try:
            aw = QApplication.activeWindow()
        except Exception:
            return False
        if aw is None:
            return False
        return any(aw is w for w in lst)

    def apply_breath(self, period_s: float, max_brightness: float,
                     color_hex: str) -> None:
        """Live-apply breathing settings changed in the editor sub-page."""
        if getattr(self, "_breath", None) is not None:
            self._breath.set_period(period_s)
            self._breath.set_max_brightness(max_brightness)
        if color_hex:
            try:
                self._breath_color = hex_to_color(
                    color_hex[1:] if color_hex.startswith("#") else color_hex)
            except Exception:
                self._breath_color = None
        else:
            self._breath_color = None
        # Breathing colour becomes the canonical theme accent; mirror it onto
        # the window neon border / tray icon so everything recolors live.
        set_theme_accent(color_hex)
        _ac = get_theme_accent()
        self._accent = QColor(_ac.r, _ac.g, _ac.b)
        tray = getattr(self, "_tray", None)
        if tray is not None:
            tray.setIcon(self._make_tray_icon())
        # The tray's frosted glow layer uses the breathing colour — rebuild it.
        self._rebuild_tray_glow()
        if not self._offscreen:
            self.update()
        # The breathing colour is the single theme-accent source: re-skin every
        # block + card from it so the change is visible everywhere at once.
        self._refresh_all_accents()

    def _refresh_all_accents(self) -> None:
        """Re-skin every block + card from the current breathing colour.

        Called after the breathing colour is (re)published (boot / live edit) so
        the single theme-accent source propagates everywhere: block titles,
        card accents, focus borders, freeze highlight.
        """
        app = self._app
        if app is not None and getattr(app, "theme_manager", None) is not None:
            try:
                app.theme_manager.propagate()
            except Exception:
                pass
        for p in self.panels:
            try:
                # Re-apply each card's effective accent (per-card override,
                # else theme accent / registry colour) so a changed preset —
                # or a changed breathing-light colour — shows up everywhere.
                if hasattr(p, "_resolve_accent"):
                    _c = p._resolve_accent()
                    p.set_accent(f"#{_c.r:02x}{_c.g:02x}{_c.b:02x}")
                if hasattr(p, "_sync_block_title"):
                    p._sync_block_title()
                if hasattr(p, "_apply_tile_colors"):
                    p._apply_tile_colors()
            except Exception:
                pass
        # Breathing colour / bg may have changed → drop the cached paint layers
        # so the next paintEvent re-renders them with the new colours.  Also
        # invalidate the paint-coalescing tracker so the first frame after a
        # theme change always repaints (otherwise a near-identical alpha could
        # be skipped and the new colour would never show).
        self._glow_cache = None
        self._bg_cache = None
        self._last_paint_alpha = None
        self.update()

    def apply_tray(self, color_hex: str, opacity: int, border: bool,
                   blur: int = 18) -> None:
        """Live-apply the card-tray background (colour / opacity / border / blur).

        The tray is a translucent rounded panel painted in ``paintEvent``
        *over* the breathing-light window, so the breathing glow shows through
        it (frosted by ``blur``).  This restores the original "tray lets the
        breathing light through" behaviour and replaces the old opaque desktop
        snapshot that hid the glow.

        Only colour / opacity / border / blur are stored here; the actual
        blurred breathing-glow layer behind the tray is rebuilt via
        ``_rebuild_tray_glow``.
        """
        scroll = getattr(self, "_scroll", None)
        if scroll is None:
            return
        # Resolve the fill colour (fall back to the original dark panel).
        r, g, b = 14, 17, 23
        try:
            _c = hex_to_color(
                color_hex[1:] if color_hex and color_hex.startswith("#") else color_hex)
            r, g, b = _c.r, _c.g, _c.b
        except Exception:
            r, g, b = 14, 17, 23
        # opacity: 0..100 (%) -> 0..255 alpha.
        _a = max(0, min(255, int(round((int(opacity) if opacity else 0) / 100.0 * 255))))
        self._tray_rgb = (r, g, b)
        self._tray_alpha = _a
        self._tray_border = bool(border)
        self._tray_blur = max(0, min(50, int(blur)))

        if getattr(self, "_offscreen", False):
            # Headless / test surface: paintEvent's custom draw is skipped, so
            # fall back to an opaque-ish rgba viewport fill (still correct-looking).
            style = (
                f"background: rgba({r}, {g}, {b}, {_a});"
                f" border: {'1px solid rgba(255, 255, 255, 0.16)' if border else 'none'};"
                f" border-radius: 14px;"
            )
            scroll.viewport().setStyleSheet(style)
            return

        # Real HUD: the tray is painted in paintEvent, so just rebuild the
        # pre-blurred glow layer (cheap) and repaint.  Keep the viewport itself
        # transparent so nothing fights the paintEvent composite.
        scroll.viewport().setStyleSheet("background: transparent; border: none;")
        self._rebuild_tray_glow()
        self.update()

    # -- card tray (translucent, breathing-light shows through) --------
    def _paint_breath_glow(self, p: Any, w: int, h: int, a: float,
                           glow: QColor) -> None:
        """Draw the breathing scanline band + neon border.  Shared by the live
        window paint and the offscreen tray glow layer."""
        # Top breathing scanline glow.
        band_h = 3 + int(12 * a)
        grad = QLinearGradient(0, 0, w, band_h + 6)
        gr = glow.red()
        gg = glow.green()
        gb = glow.blue()
        grad.setColorAt(0.0, QColor(gr, gg, gb, 0))
        grad.setColorAt(0.5, QColor(gr, gg, gb, int(70 * a)))
        grad.setColorAt(1.0, QColor(gr, gg, gb, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(grad)
        p.drawRoundedRect(0, 0, w, band_h + 6, 16, 16)
        # Neon glow border (multi-layer) — breathes.
        path = QPainterPath()
        path.addRoundedRect(0, 0, w, h, 16, 16)
        for i in range(3, 0, -1):
            pen = QPen(QColor(gr, gg, gb, int(34 * i * a)))
            pen.setWidth(i)
            p.setPen(pen)
            p.drawPath(path)
        p.setPen(QPen(glow, 1.2))
        p.drawPath(path)

    # -- paint-layer caching (perf) --------------------------------------
    def _make_dpr_pixmap(self, w: int, h: int) -> "QPixmap":
        """Allocate an offscreen layer at *device* resolution.

        The window can run at a fractional device-pixel-ratio (e.g. 1.5x on a
        1080p handheld @ 150% scaling).  Building a QPixmap at logical size and
        then blitting it with drawPixmap(0, 0, pm) makes Qt upscale the layer by
        the DPR with bilinear filtering -> the whole HUD skin looks soft.  By
        allocating at w*dpr x h*dpr and stamping devicePixelRatio, callers keep
        painting in *logical* (w, h) coordinates and the blit lands 1:1 on
        device pixels -> crisp on both integer (2.0x) and fractional (1.5x) DPR.
        """
        dpr = max(1.0, float(self.devicePixelRatio()))
        pm = QPixmap(int(round(w * dpr)), int(round(h * dpr)))
        pm.setDevicePixelRatio(dpr)
        return pm

    def _rebuild_bg_cache(self) -> None:
        """Pre-render the full-window frosted bg fill into ``_bg_cache``.

        The bg is static (only changes on geometry / theme), so blitting this
        pixmap every breath frame replaces a full-window rounded-path fill in
        software raster.
        """
        if getattr(self, "_offscreen", False):
            return
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        bg = self._make_dpr_pixmap(w, h)
        bg.fill(Qt.GlobalColor.transparent)
        bp = QPainter(bg)
        bp.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, w, h, 16, 16)
        bp.setPen(Qt.PenStyle.NoPen)
        bp.setBrush(self._bg)
        bp.drawPath(path)
        bp.end()
        self._bg_cache = bg

    def _rebuild_glow_cache(self) -> None:
        """Pre-render the breathing neon border + scanline (alpha = 1.0) into
        ``_glow_cache``.  Each breath frame blits it with ``setOpacity(alpha)``
        so we never re-stroke the 4 full-window rounded paths per frame.
        """
        if getattr(self, "_offscreen", False):
            return
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        glow = getattr(self, "_breath_color", None) or getattr(self, "_accent", None)
        if not isinstance(glow, QColor):
            glow = QColor(getattr(glow, "r", 127), getattr(glow, "g", 209),
                          getattr(glow, "b", 255))
        # Bake the *current* unfocus dim into the cached neon layer so the
        # same cheap blit path works whether focused (dim==1) or unfocused
        # (dim<1).  Previously the unfocus case fell back to 4 full-window
        # path strokes every breath frame — that was the 9-17% CPU spike.
        glow = self._dim_color(glow, getattr(self, "_glow_dim", 1.0))
        layer = self._make_dpr_pixmap(w, h)
        layer.fill(Qt.GlobalColor.transparent)
        lp = QPainter(layer)
        lp.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint_breath_glow(lp, w, h, 1.0, glow)
        lp.end()
        self._glow_cache = layer

    def _rebuild_tray_glow(self) -> None:
        """Pre-render the breathing glow (band + border) into an offscreen
        pixmap and blur it by ``_tray_blur``.  Cached in ``_tray_glow_blur`` and
        only rebuilt on geometry / blur / breathing-colour change — never per
        breath frame — so the live HUD stays cheap."""
        if getattr(self, "_offscreen", False):
            return
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        # Reuse the cached neon-border layer (also refreshed here) and blur it
        # for the frosted tray glow, instead of re-stroking the paths.
        self._rebuild_glow_cache()
        glow = getattr(self, "_breath_color", None) or getattr(self, "_accent", None)
        if not isinstance(glow, QColor):
            glow = QColor(getattr(glow, "r", 127), getattr(glow, "g", 209),
                          getattr(glow, "b", 255))
        glow = self._dim_color(glow, getattr(self, "_glow_dim", 1.0))
        layer = self._make_dpr_pixmap(w, h)
        layer.fill(Qt.GlobalColor.transparent)
        lp = QPainter(layer)
        lp.setRenderHint(QPainter.RenderHint.Antialiasing)
        # _rebuild_glow_cache (called above) now bakes the current glow_dim
        # into _glow_cache, so it is always safe to blur the cached layer
        # (no separate dimmed-stroke branch needed).
        if self._glow_cache is not None and not self._glow_cache.isNull():
            lp.drawPixmap(0, 0, self._glow_cache)
        else:
            self._paint_breath_glow(lp, w, h, 1.0, glow)
        # A soft scanline glow along the TOP of the card tray so the frosted
        # breathing is clearly visible *inside* the tray (the window-edge glow
        # alone only bleeds in ~8px at the tray's outer edge).
        vp = getattr(self, "_scroll", None)
        if vp is not None and vp.parent() is not None:
            tp = vp.mapTo(self, QPoint(0, 0))
            band_h = 26
            gr = glow.red()
            gg = glow.green()
            gb = glow.blue()
            grad = QLinearGradient(0, tp.y(), 0, tp.y() + band_h)
            grad.setColorAt(0.0, QColor(gr, gg, gb, 0))
            grad.setColorAt(0.5, QColor(gr, gg, gb, 80))
            grad.setColorAt(1.0, QColor(gr, gg, gb, 0))
            lp.setPen(Qt.PenStyle.NoPen)
            lp.setBrush(grad)
            lp.drawRect(tp.x(), tp.y(), vp.width(), band_h)
        lp.end()
        blurred = blur_pixmap(layer, self._tray_blur)
        self._tray_glow_blur = (
            blurred if (blurred is not None and not blurred.isNull()) else layer)
        self.update()

    def _paint_tray(self, p: Any, w: int, h: int) -> None:
        """Paint the card tray: a translucent rounded panel over the breathing
        glow.  The pre-blurred glow layer shows through (frosted), then the tray
        colour @ opacity, then a crisp border."""
        vp = getattr(self, "_scroll", None)
        if vp is None or vp.parent() is None:
            return
        tp = vp.mapTo(self, QPoint(0, 0))
        tw, th = vp.width(), vp.height()
        if tw <= 0 or th <= 0:
            return
        rr = 14
        path = QPainterPath()
        path.addRoundedRect(tp.x(), tp.y(), tw, th, rr, rr)
        p.save()
        p.setClipPath(path)
        # Mask the crisp exterior glow inside the tray so only the frosted
        # version (below) shows here.
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._bg)
        p.drawRoundedRect(tp.x(), tp.y(), tw, th, rr, rr)
        # Frosted breathing glow behind the tray (pre-blurred, pulses with breath).
        gb = self._tray_glow_blur
        if gb is not None and not gb.isNull():
            p.setOpacity(max(0.0, min(1.0, float(self._breath_alpha))))
            p.drawPixmap(0, 0, gb)
            p.setOpacity(1.0)
        # Tray colour @ opacity.
        r, g, b = self._tray_rgb
        p.setBrush(QColor(r, g, b, self._tray_alpha))
        p.drawRoundedRect(tp.x(), tp.y(), tw, th, rr, rr)
        p.restore()
        # Crisp tray border on top.
        if self._tray_border:
            dim = getattr(self, "_glow_dim", 1.0)
            p.setPen(QPen(QColor(255, 255, 255, max(0, int(40 * dim))), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(tp.x(), tp.y(), tw - 1, th - 1, rr, rr)

    # -- battery --------------------------------------------------------
    def _update_battery(self) -> None:
        status = get_battery_status()
        if status is None:
            self._batt.set_none()
        else:
            self._batt.set_status(*status)

    # -- system tray / minimize-to-tray -------------------------------
    def _init_tray(self) -> None:
        """Create the system-tray icon (only if the platform has one).

        The − button hides the window here; the ✕ button still calls
        ``self.close()`` for a full, clean shutdown.  On headless/offscreen
        runs ``isSystemTrayAvailable()`` is False, so ``self._tray`` stays
        None and the minimize button falls back to ``showMinimized``."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(self._make_tray_icon())
        self._tray.setToolTip("凝冰结魄")
        menu = QMenu(self)
        restore = menu.addAction("恢复")
        restore.triggered.connect(self._restore_from_tray)
        menu.addSeparator()
        quit_act = menu.addAction("退出")
        # 关键修复：已 hide() 到托盘的隐藏窗口直接 close() 在 PyQt6/Windows
        # 下可能不触发 closeEvent、事件循环不退（进程残留）。统一走
        # _quit_app——先收尾再 QApplication.quit() 兜底，确保一定退出。
        quit_act.triggered.connect(self._quit_app)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _quit_app(self) -> None:
        """强制、彻底退出程序（托盘右键『退出』与 ✕ 按钮共用）。

        设计要点：
        - 窗口被 − 按钮 hide() 到托盘后，对它直接 ``close()`` 在 PyQt6/Windows
          下可能出现 no-op：closeEvent 不触发、QApplication 事件循环不退，
          进程残留（托盘消失但任务管理器里仍有进程）。这就是『右键关闭关不掉』
          的根因。
        - 因此这里分两步：先显式 flush 配置并调用 close() 跑标准收尾
          （closeEvent 里的 save_all / 停线程 / 关子进程 / 复位降压等）；
          再用 ``QApplication.instance().quit()`` 兜底，无论 close() 在隐藏窗口
          上是否生效，事件循环都会被强制结束，进程一定退出。
        - ``quit()`` 会先发射 ``aboutToQuit``（已注册 _shutdown_undervolt 等），
          与 closeEvent 收尾互补，不会漏掉清理。"""
        try:
            app_ref = getattr(self, "_app", None)
            if app_ref is not None and hasattr(app_ref, "save_all"):
                app_ref.save_all()
        except Exception:  # noqa: BLE001 - 退出阶段不应因保存失败阻断收尾
            pass
        # 标准收尾（即便窗口隐藏也尽量触发；失败也无妨，下面 quit() 兜底）。
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass
        # 兜底：强制结束事件循环，确保进程退出（覆盖隐藏窗口 close() no-op）。
        qapp = QApplication.instance()
        if qapp is not None:
            qapp.quit()

    def _log_fit(self, msg: str) -> None:
        """Append a one-line diagnostic to logs/window_fit.log.

        Helps verify whether the running code is the new fit logic and what
        screen/DPI values it actually sees on the user's display."""
        try:
            log_dir = Path(__file__).resolve().parents[3] / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "window_fit.log"
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            with log_path.open("a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
        except Exception:
            pass

    def _fit_to_screen(self) -> None:
        """Resize + reposition to hug the right edge of the current screen.

        Height fills the available screen height (minus a small margin) so the
        rail scales with the display — from a short 768p laptop up to a 1440p
        desktop.  A floor keeps it usable on very short screens.  Called on init,
        on every monitor / resolution change (``screenChanged``), and on
        ``showEvent``: the screen is only guaranteed to be known once the window
        is actually on a display, so re-fitting there also fixes multi-monitor
        setups where the window first landed on a different screen than
        ``self.screen()`` reported at ``__init__`` time.

        A hard ``setMaximumHeight`` ceiling = the **available** screen height
        makes it impossible for the window to ever spill off the bottom edge —
        even if ``availableGeometry()`` momentarily over-reports the usable area
        (some taskbar / DPI configurations), if Windows scaling changes the
        logical-pixel mapping, or if a layout change tries to grow the window
        taller.  The HUD's internal QScrollArea keeps every card reachable
        instead of letting the window overflow."""
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            self._log_fit("SKIP: no screen")
            return
        avail = screen.availableGeometry()
        phys = screen.geometry()
        dpr = screen.devicePixelRatio()
        # Use the smaller of work-area / full-screen as the hard boundary.
        # On scaled displays Qt already returns logical pixels; the real ceiling
        # is whatever screen geometry reports, but we keep a bottom safety pad.
        max_h = min(avail.height(), phys.height())
        top_pad = 4
        # 底边留白与顶边对称（4px）：窗口底边恰好停在 Windows 底部任务栏上方，
        # 仅留极窄间隙，不再留下明显空隙。发光/呼吸描边绘制在控件矩形内部，
        # 缩减此留白不会被裁切。
        bot_pad = 4
        height = max_h - top_pad - bot_pad
        height = min(max(320, height), max_h - top_pad - bot_pad)
        # Rail width scales with screen width (clamped to a sane band).
        # 上限从 460 提到 520：外接高分辨率大屏（如 1440p/4K 电视）上侧栏不再
        # 是一条挤在边缘的窄条，元素也跟着更舒展；掌机本屏 avail.width()*0.28
        # 仍落在 360 左右，不受影响。
        width = max(360, min(520, int(avail.width() * 0.28)))
        # Horizontal anchor respects the user's left/right screen-edge choice
        # (persisted as AppConfig.window_align, toggled from the layout editor).
        # Anything other than "left" hugs the right edge (original behaviour).
        align = "right"
        try:
            cfg = self._app.config_manager.config
            if getattr(cfg, "window_align", "right") == "left":
                align = "left"
        except Exception:
            align = "right"
        if align == "left":
            x = avail.x() + 6
        else:
            x = avail.x() + max(0, avail.width() - width - 6)
        y = avail.y() + top_pad
        # Final guard: the frameless window's client rect must fit inside max_h.
        if y + height > max_h:
            height = max(320, max_h - y - bot_pad)
        # Hard ceilings so no later layout pass can grow us off-screen.
        # The explicit minimum (<= max_h) prevents the top-level layout from
        # re-adopting the scroll area's full-content minimum height and pushing
        # the window past max_h (which silently overrode the ceiling before).
        floor = min(360, max_h)
        self.setMinimumHeight(floor)
        self.setMaximumHeight(max_h)
        self.setMaximumWidth(min(520, avail.width()))
        # Guard against feedback loops while a resize is already in flight.
        old = self.geometry()
        if old.width() == width and old.height() == height \
                and old.x() == x and old.y() == y:
            self._log_fit(
                f"NOOP screen={screen.name()} dpr={dpr} avail={avail.width()}x{avail.height()} "
                f"phys={phys.width()}x{phys.height()} geo={old.width()}x{old.height()}@({old.x()},{old.y()})"
            )
            return
        self.setGeometry(x, y, width, height)
        new = self.geometry()
        fits = (new.y() + new.height()) <= max_h + 1 and new.height() <= max_h + 1
        self._log_fit(
            f"FIT screen={screen.name()} dpr={dpr} avail={avail.width()}x{avail.height()} "
            f"phys={phys.width()}x{phys.height()} wanted={width}x{height}@({x},{y}) "
            f"got={new.width()}x{new.height()}@({new.x()},{new.y()}) fits={fits}"
        )

    def _wire_dpr_change(self) -> None:
        """Connect QWindow.devicePixelRatioChanged -> _fit_to_screen.

        Covers *same-screen* scale changes (user drags the Windows 缩放 slider
        while the HUD is open).  Those emit ``devicePixelRatioChanged`` but NOT
        ``screenChanged``, so the existing screenChanged wiring misses them and
        the HUD would keep a stale geometry.  The QWindow handle may not exist
        at ``__init__`` time, so this is also invoked from ``showEvent`` once the
        handle is available; a guard flag prevents double-connecting."""
        if getattr(self, "_dpr_wired", False):
            return
        wh = self.windowHandle()
        if wh is None:
            return
        try:
            wh.devicePixelRatioChanged.connect(self._fit_to_screen)
            self._dpr_wired = True
        except Exception:  # noqa: BLE001
            pass

    def _make_tray_icon(self) -> QIcon:
        """Render a small frosted-cyan glyph icon (no font-glyph dependency)."""
        size = 64
        pm = self._make_dpr_pixmap(size, size)
        pm.fill(QColor(0, 0, 0, 0))
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(20, 24, 33))
        p.drawRoundedRect(5, 5, size - 10, size - 10, 14, 14)
        p.setPen(QPen(self._accent, 3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(5, 5, size - 10, size - 10, 14, 14)
        p.setPen(self._accent)
        f = QFont()
        f.setBold(True)
        f.setPixelSize(44)
        f.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
        p.setFont(f)
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "凝")
        p.end()
        return QIcon(pm)

    # -- minimize / restore fade (compositor-only opacity, 0 geometry cost) ----
    def _fade_out_then_hide(self) -> None:
        """淡出主窗到托盘：只在 compositor 层改整窗 alpha，不重绘任何子控件。

        纯 DWM 合成，单帧零 CPU 控件开销，绝不影响游戏帧。离屏（单测）环境
        下瞬时完成，避免动画空转挂起测试。"""
        if getattr(self, "_offscreen", False):
            self.hide()
            return
        anim = getattr(self, "_fade_anim", None)
        if anim is not None:
            anim.stop()
        anim = QPropertyAnimation(self, b"windowOpacity")
        anim.setDuration(100)
        anim.setEasingCurve(QEasingCurve.Type.InQuad)
        anim.setStartValue(self.windowOpacity())
        anim.setEndValue(0.0)
        anim.finished.connect(self.hide)
        self._fade_anim = anim
        anim.start()

    def _fade_in_from_tray(self) -> None:
        """从托盘淡入主窗：先在 alpha=0 显示，再缓动到 1。同样只动 compositor。

        离屏环境瞬时完成。若窗口已可见（重复触发），直接确保不透明并抢焦点。"""
        if getattr(self, "_offscreen", False):
            self.setWindowOpacity(1.0)
            self.showNormal()
            self.raise_()
            self.activateWindow()
            self._keep_perfmon_on_top()
            return
        self.setWindowOpacity(0.0)
        self.showNormal()
        self.raise_()
        self.activateWindow()
        anim = getattr(self, "_fade_anim", None)
        if anim is not None:
            anim.stop()
        anim = QPropertyAnimation(self, b"windowOpacity")
        anim.setDuration(120)
        anim.setEasingCurve(QEasingCurve.Type.OutQuad)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.finished.connect(self._keep_perfmon_on_top)
        self._fade_anim = anim
        anim.start()

    def _minimize_to_tray(self) -> None:
        """− button: hide to the system tray instead of closing.

        Falls back to a normal taskbar minimize when no tray is available."""
        # Del 键 / − 按钮隐藏主窗口时，一并关闭仍打开的疑惑解答弹窗，
        # 否则它会作为独立 Tool 窗口悬在主窗口之外（隐藏不生效）。
        faq = getattr(self, "_faq", None)
        if faq is not None and faq.is_open():
            faq.close()
        if self._tray is not None:
            # 先淡出再隐藏；若窗口当前不可见（重复触发）则瞬时隐藏。
            if self.isVisible():
                self._fade_out_then_hide()
            else:
                self.hide()
            self._tray.showMessage(
                "凝冰结魄",
                "已最小化到系统托盘，点击托盘图标可恢复",
                QSystemTrayIcon.MessageIcon.Information,
                1500,
            )
        else:
            self.showMinimized()

    def _restore_from_tray(self) -> None:
        self._fade_in_from_tray()

    def _on_tray_activated(self, reason: Any) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._restore_from_tray()

    # -- painting (the frosted skin) ------------------------------------
    def paintEvent(self, ev: Any) -> None:  # noqa: N802 - Qt camelCase
        # Offscreen (headless test surface): skip the custom frosted paint and
        # let Qt paint the widget normally — this avoids the offscreen raster
        # backend crash with a translucent frameless window.
        if self._offscreen:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return

        # Frosted dark fill — blit the cached full-window layer instead of
        # re-filling a rounded path every breath frame.
        if self._bg_cache is None or self._bg_cache.width() != w or self._bg_cache.height() != h:
            self._rebuild_bg_cache()
        if self._bg_cache is not None and not self._bg_cache.isNull():
            p.drawPixmap(0, 0, self._bg_cache)
        else:
            path = QPainterPath()
            path.addRoundedRect(0, 0, w, h, 16, 16)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._bg)
            p.drawPath(path)

        a = self._breath_alpha  # 0.2 .. 1.0

        # Breathing scanline glow + neon border.  Always blit the cached layer
        # (built at the current glow_dim, see _rebuild_glow_cache) with opacity
        # = breath alpha.  This single cheap path now covers BOTH focused and
        # unfocused states; the old per-frame 4-stroke fallback only ran when
        # dim<1 and was the source of the 9-17% unfocus CPU spike.
        if self._glow_cache is None or self._glow_cache.width() != w or self._glow_cache.height() != h:
            self._rebuild_glow_cache()
        if self._glow_cache is not None and not self._glow_cache.isNull():
            p.setOpacity(a)
            p.drawPixmap(0, 0, self._glow_cache)
            p.setOpacity(1.0)
        else:
            glow = self._breath_color if self._breath_color is not None else self._accent
            if not isinstance(glow, QColor):
                glow = QColor(glow.r, glow.g, glow.b)
            glow = self._dim_color(glow, self._glow_dim)
            self._paint_breath_glow(p, w, h, a, glow)

        # Card tray background: translucent rounded panel OVER the breathing
        # glow, so the breathing light shows through (frosted by tray_blur).
        self._paint_tray(p, w, h)
        p.end()

    # -- drag-to-move (header is the handle) ---------------------------
    def mousePressEvent(self, ev: Any) -> None:  # noqa: N802
        # 疑惑解答：仅点击卡片「标题」(_hdr) 才打开该卡片的说明；点击卡片其它
        # 区域（正文 / 列表 / 滑块 / 按钮等）不触发，避免误开。编辑器页面改用
        # ClickableTitle（标题即召唤，见 edit_overlay / button_edit）。
        if getattr(self, "_faq", None) is not None and not self._controller._edit_mode:
            hit = self.childAt(ev.position().toPoint())
            if hit is not None:
                p: Any = hit
                panel: Any = None
                while p is not None:
                    if getattr(p, "block_type", None):
                        panel = p
                        break
                    p = p.parentWidget()
                if panel is not None:
                    hdr = getattr(panel, "_hdr", None)
                    # 只认标题标签本身或其子控件；其余一律不召唤。
                    if hdr is not None and (hit is hdr or hdr.isAncestorOf(hit)):
                        self._faq.open_for(panel.block_type, "鼠标 点击标题")
                        # 打开该卡片的疑惑解答时，同时把操作焦点切到这张卡：
                        # 区块加粗边框 + 键盘/手柄导航都落到本卡，避免「弹窗是 B
                        # 卡、加粗边框/导航焦点还停在 A 卡」的割裂感。走统一入口
                        # pick_widget；若点的正是已聚焦的卡（再次点同卡标题=切换
                        # 关闭弹窗），_notify_card_focus_change 会早返回、不会误关
                        # 刚开的弹窗。
                        try:
                            self._controller.pick_widget(panel)
                        except Exception:
                            pass
                        ev.accept()
                        return
        child = self.childAt(ev.position().toPoint())
        if child is None or not isinstance(child, QPushButton):
            self._drag_offset = (
                ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
        else:
            self._drag_offset = None
        # Mouse navigation focus: clicking a card body / slider / input moves
        # the keyboard+gamepad focus there.  Plain buttons are handled by their
        # own clicked -> controller.activate_widget, so skip them here to avoid
        # a double activation.
        if child is not None and not isinstance(child, QPushButton):
            try:
                self._controller.pick_widget(child)
            except Exception:
                pass
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev: Any) -> None:  # noqa: N802
        if self._drag_offset is not None and (ev.buttons() & Qt.MouseButton.LeftButton):
            self.move(ev.globalPosition().toPoint() - self._drag_offset)
            ev.accept()
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev: Any) -> None:  # noqa: N802
        self._drag_offset = None
        super().mouseReleaseEvent(ev)

    def moveEvent(self, ev: Any) -> None:  # noqa: N802
        # 主窗口被拖动时，疑惑解答弹窗实时跟随（若已打开）。
        # 弹窗以本窗口为父（从属窗口），系统自动跟随移动 / 虚拟桌面；这里再做
        # 一次精确重定位。注意：不要调用 sink()/lower()，那会清除主窗口的
        # WS_EX_TOPMOST 置顶状态，导致整个程序掉到桌面最底层。
        super().moveEvent(ev)
        if getattr(self, "_faq", None) is not None:
            self._faq.reposition()
        # 左侧按钮列表浮窗随主窗口一起精确重定位。
        ov = getattr(self, "_edit_overlay", None)
        if ov is not None:
            bp = getattr(ov, "_button_panel", None)
            if bp is not None:
                bp._list_popup.reposition()

    def _apply_window_mask(self) -> None:
        """Clip the (now opaque) window to a 16px rounded rectangle so the
        corners stay rounded after dropping WA_TranslucentBackground.  A 1-bit
        QRegion mask has hard edges (no anti-alias), but at 16px on a dark
        window that is imperceptible.  No-op under offscreen test surfaces."""
        if getattr(self, "_offscreen", False):
            return
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        path = QPainterPath()
        path.addRoundedRect(0, 0, w, h, 16, 16)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def resizeEvent(self, ev: Any) -> None:  # noqa: N802
        super().resizeEvent(ev)
        # HARD clamp: never let the window grow taller than the usable screen,
        # no matter what a layout pass / the OS / an expanding card tries to do.
        # setMaximumHeight() alone can be overridden by a layout-driven
        # adjustSize() (observed on a 7" 1080p panel at 150% DPI: the window was
        # pushed from 644px to 728px past the 660px ceiling).  This resize-time
        # guard is the final safety net.  Recursion is safe: once we resize down
        # to max_h the next resizeEvent's height == max_h and the guard is False.
        if not getattr(self, "_offscreen", False):
            screen = self.screen() or QApplication.primaryScreen()
            if screen is not None:
                max_h = min(screen.availableGeometry().height(),
                            screen.geometry().height())
                if self.height() > max_h:
                    self.resize(self.width(), max_h)
                    self._log_fit(
                        f"RESIZE-CLAMP height={self.height()}->capped to {max_h}")
                # Symmetric HARD clamp for WIDTH (mirrors the height clamp above
                # and the proven minimumHeight pin at scroll.setMinimumHeight):
                # a card's content minimum width can otherwise propagate through
                # the scroll area's minimumSizeHint and grow the whole frameless
                # window past the screen edge.  Horizontal scroll is OFF, so Qt
                # satisfies that minimum by WIDENING the window instead of
                # clipping — that is exactly the "page wider than screen, must
                # drag it" bug.  Cap width to the usable screen (never above
                # 520) and re-anchor to the right edge so no gap is left.
                # Recursion-safe: once width==max_w the next resizeEvent's
                # condition is False.
                max_w = min(520, screen.availableGeometry().width())
                if self.width() > max_w:
                    # Re-anchor to the user's chosen screen edge (left/right),
                    # mirroring _fit_to_screen, so a width cap never shoves the
                    # window back to the wrong side.
                    try:
                        cfg = self._app.config_manager.config
                        clamp_align = "left" if getattr(
                            cfg, "window_align", "right") == "left" else "right"
                    except Exception:
                        clamp_align = "right"
                    if clamp_align == "left":
                        new_x = screen.availableGeometry().x() + 6
                    else:
                        new_x = screen.availableGeometry().x() + max(
                            0, screen.availableGeometry().width() - max_w - 6)
                    self.setGeometry(new_x, self.y(), max_w, self.height())
                    self._log_fit(
                        f"RESIZE-CLAMP width={self.width()}->capped to {max_w}")
        # The breathing glow layer is window-local, so its shape changes with
        # the window size — rebuild it (debounced) so the frosted tray tracks
        # the new geometry.  Move events don't need this (glow is in window coords).
        if not getattr(self, "_offscreen", False):
            self._glow_timer.start(80)
            self._apply_window_mask()
            # Keep the unfocus scrim covering the whole window and on top.
            scrim = getattr(self, "_scrim", None)
            if scrim is not None:
                scrim.setGeometry(self.rect())
                scrim.raise_()

    # -- refresh driver ------------------------------------------------
    def _collect_tasks(self) -> list[Any]:
        tasks = []
        for p in self.panels:
            fn = p.heavy_refresh()
            if fn is not None:
                tasks.append(fn)
        return tasks

    def _do_refresh(self) -> None:
        if self._worker.isRunning():
            return
        tasks = self._collect_tasks()
        if not tasks:
            return
        self._worker.set_tasks(tasks)
        self._worker.start()

    # -- 负载门控降压（共享 MetricSampler）----------------------------
    def _init_undervolt(self) -> None:
        """装配 MetricSampler + UndervoltController，启动专属 tick 定时器。

        设计要点：
          - MetricSampler 是全应用唯一的指标捕获器（GPU/每核/前台CPU 各一份
            后台流），undervolt、GPU 浮动锁频、未来自动锁帧都读它——零重复采样。
          - 专属 _uv_timer 不进 _pause_aux 暂停集：app 失焦（游戏前台）时
            其它刷新定时器停了，但降压必须继续跑。
          - 默认 enabled=false：状态机照常运行、仅不写 ryzenadj，避免未经
            受控验证就自动改电压。改 config/undervolt.json 的 enabled=true 启用。
        """
        try:
            app_root = Path(__file__).resolve().parents[3]
            cfg_path = app_root / "config" / "undervolt.json"
            self._uv_last = {"state": "", "mv": 0}
            cfg = UndervoltConfig.load(cfg_path)

            # 复用 power 卡片已建的共享 ryzenadj 实例（同一 exe，避免双开）
            power = self._app.blocks.get("power") if getattr(self, "_app", None) else None
            ra = getattr(power, "ryzenadj", None) if power else None
            if ra is None:
                from ..platform.ryzenadj import MockRyzenAdj
                ra = MockRyzenAdj()
            # R5：熔断回调接线——ryzenadj 连续失败熔断时，主线程安全地弹告警
            # 并停用 GPU 浮动锁频（否则功能静默退化、用户毫无察觉）。
            ra.on_circuit_breaker = self._on_ryzenadj_circuit_breaker
            # dry_run 时 backend 也置为只记日志不写电压（双重保险）
            dry_log_path = cfg_path.with_name("undervolt_dryrun.log") if cfg.dry_run else None
            backend = RyzenAdjBackend(ra, dry_run=cfg.dry_run)

            sampler = MetricSampler.instance()
            self._uv_sampler = sampler
            self._uv_controller = UndervoltController(
                backend, sampler, cfg, dry_log_path=dry_log_path)
            self._uv_cfg_path = cfg_path

            def _on_uv_change(state: str, mv: int) -> None:
                self._uv_last["state"] = state
                self._uv_last["mv"] = mv
                tag = {
                    IDLE: "空闲(CO=0)", LIGHT: f"轻载(CO={mv})",
                    HEAVY: f"重载(CO={mv})",
                }.get(state, state)
                prefix = "[undervolt·DRY-RUN]" if cfg.dry_run else "[undervolt]"
                print(f"{prefix} 状态={tag}")

            self._uv_controller.on_change = _on_uv_change

            if sys.platform == "win32":
                sampler.start()
            self._uv_timer = QTimer(self)
            self._uv_timer.timeout.connect(self._uv_tick)
            self._uv_timer.start(1000)

            if cfg.dry_run:
                print(f"[undervolt·DRY-RUN] 观察模式：逐 tick 记录到 "
                      f"{dry_log_path}（绝不写 ryzenadj）。"
                      f" enabled={cfg.enabled} heavy_enabled={cfg.heavy_enabled}")
            elif cfg.enabled:
                print(f"[undervolt] 已启用：offsets={cfg.offsets} "
                      f"heavy_enabled={cfg.heavy_enabled}")
            else:
                print("[undervolt] 已加载但 disabled（enabled=false）。"
                      " 状态机运行中，仅不写 ryzenadj；改 config/undervolt.json 的 "
                      "enabled=true 启用。")

            # R4：纵深防御退出路径——正常 closeEvent 已复位，这里再挂
            # aboutToQuit + atexit，覆盖 QApplication.quit / 解释器级退出的
            # 其它路径（避免残留 CO 不被清）。_shutdown_undervolt 已幂等。
            try:
                qapp = QApplication.instance()
                if qapp is not None:
                    try:
                        qapp.aboutToQuit.disconnect(self._shutdown_undervolt)
                    except Exception:
                        pass
                    qapp.aboutToQuit.connect(self._shutdown_undervolt)
            except Exception:
                pass
            try:
                import atexit
                atexit.register(self._shutdown_undervolt)
            except Exception:
                pass
        except Exception as e:  # pragma: no cover
            print(f"[undervolt] 初始化失败: {e}")
            self._uv_controller = None

    def _uv_tick(self) -> None:
        """专属定时器回调：推进降压状态机（GUI 线程，纯内存读 + 火速派发）。"""
        ctrl = getattr(self, "_uv_controller", None)
        if ctrl is None:
            return
        try:
            ctrl.tick(1.0)
        except Exception as e:  # pragma: no cover
            print(f"[undervolt] tick error: {e}")

    def _shutdown_undervolt(self) -> None:
        """app 退出：复位 CO 到 0（避免残留 -50）+ 停共享采样器。"""
        ctrl = getattr(self, "_uv_controller", None)
        sampler = getattr(self, "_uv_sampler", None)
        try:
            if ctrl is not None:
                ctrl.reset()
                ctrl.close()
        except Exception:
            pass
        try:
            if sampler is not None and sampler.is_running:
                sampler.stop()
        except Exception:
            pass

    # -- R5：ryzenadj 熔断回调（自动关 GPU 浮动 + 告警）---------------------
    def _on_ryzenadj_circuit_breaker(self) -> None:
        """由 ryzenadj 后台回收线程触发，必须回到主线程操作 UI/状态。"""
        QTimer.singleShot(0, self._on_ryzenadj_circuit_breaker_ui)

    def _on_ryzenadj_circuit_breaker_ui(self) -> None:
        """主线程执行：弹告警 + 自动停用 GPU 浮动锁频（避免静默退化）。"""
        power = self._app.blocks.get("power") if getattr(self, "_app", None) else None
        if power is not None:
            try:
                power.show_toast(
                    "⚠ ryzenadj 连续调用失败，已暂停自动下发；GPU 浮动锁频已停用",
                    success=False)
            except Exception:
                pass
            if getattr(power, "gpu_float_enabled", False):
                try:
                    power.toggle_gpu_float()  # 切到 OFF，并 reset desired
                except Exception:
                    pass

    # --- undervolt 开关（编辑面板实时调节）---------------------------
    def _undervolt_cfg_path(self) -> "Path":
        """兜底解析 undervolt.json 路径（控制器未初始化时也能读写）。"""
        p = getattr(self, "_uv_cfg_path", None)
        if p is not None:
            return p
        return Path(__file__).resolve().parents[3] / "config" / "undervolt.json"

    def undervolt_get_flags(self) -> dict:
        """返回当前 enabled / heavy_enabled / heavy_validated（读 live config，无则读文件）。"""
        ctrl = getattr(self, "_uv_controller", None)
        if ctrl is not None:
            cfg = ctrl.config
        else:
            cfg = UndervoltConfig.load(self._undervolt_cfg_path())
        return {"enabled": bool(cfg.enabled),
                "heavy_enabled": bool(cfg.heavy_enabled),
                "heavy_validated": bool(cfg.heavy_validated)}

    def undervolt_set_flag(self, name: str, value: bool) -> None:
        """编辑面板调用：实时修改并持久化 undervolt 开关。

        name ∈ {enabled, heavy_enabled, heavy_validated}。即时改 live config（状态机每个
        tick 读它，故无需重启）；并写回 undervolt.json。关闭总开关时立即把电压归零，
        切换 heavy_enabled / heavy_validated 时按新门控重应用当前档位。
        """
        if name not in ("enabled", "heavy_enabled", "heavy_validated"):
            return
        value = bool(value)
        ctrl = getattr(self, "_uv_controller", None)
        cfg = ctrl.config if ctrl is not None else UndervoltConfig.load(
            self._undervolt_cfg_path())
        if name == "enabled":
            was = bool(cfg.enabled)
            cfg.enabled = value
            if ctrl is not None:
                if was and not value:
                    # 关闭总开关：立即把残留电压归零（否则 -50 会一直挂着）
                    try:
                        ctrl.backend.reset()
                    except Exception:
                        pass
                try:
                    ctrl.reapply()
                except Exception:
                    pass
        else:  # heavy_enabled / heavy_validated —— 都需按新门控重应用当前档位
            setattr(cfg, name, value)
            if ctrl is not None:
                try:
                    ctrl.reapply()
                except Exception:
                    pass
        cfg.save(self._undervolt_cfg_path())

    def undervolt_get_thresholds(self) -> dict:
        """返回当前 hmax / hmean（读 live config，无则读文件）。"""
        ctrl = getattr(self, "_uv_controller", None)
        if ctrl is not None:
            cfg = ctrl.config
        else:
            cfg = UndervoltConfig.load(self._undervolt_cfg_path())
        return {"hmax": float(cfg.hmax), "hmean": float(cfg.hmean)}

    def undervolt_set_threshold(self, name: str, value: float,
                                save: bool = True) -> None:
        """编辑面板调用：实时修改并持久化降压阈值。

        name ∈ {hmax, hmean}。即时改 live config（状态机每个 tick 读它，无需重启）；
        可选 save 落盘 undervolt.json。阈值只是判定边界，改动后下一 tick 自动按新边界
        重判定，无需 reapply（改 enabled / heavy_enabled 才需要）。
        """
        if name not in ("hmax", "hmean"):
            return
        value = float(value)
        ctrl = getattr(self, "_uv_controller", None)
        cfg = ctrl.config if ctrl is not None else UndervoltConfig.load(
            self._undervolt_cfg_path())
        setattr(cfg, name, value)
        if save:
            cfg.save(self._undervolt_cfg_path())

    def undervolt_get_offsets(self) -> dict:
        """返回当前 offsets（idle/light/heavy 三档 CO 偏移 mV）。"""
        ctrl = getattr(self, "_uv_controller", None)
        if ctrl is not None:
            cfg = ctrl.config
        else:
            cfg = UndervoltConfig.load(self._undervolt_cfg_path())
        offs = getattr(cfg, "offsets", None) or {}
        return {
            "idle": int(offs.get("idle", 0)),
            "light": int(offs.get("light", -20)),
            "heavy": int(offs.get("heavy", -50)),
        }

    def undervolt_set_offset(self, name: str, value: int,
                             save: bool = True) -> None:
        """编辑面板调用：实时修改并持久化某档 CO 偏移（idle/light/heavy）。

        value 为 mV（负值=降压）。即时改 live config（状态机每 tick 经
        ``_offset_for`` 读它）；并写回 undervolt.json。**仅当 save=True（滑块
        松手）时才 reapply 当前档位**——否则若正停在目标档（如始终 IDLE）状态
        不变化、``_set_state`` 不重发，新偏移要等下次档位切换才生效。拖动中
        (save=False) 只更新 live config，不写硬件也不落盘。reapply 在 enabled=True
        时才真正写硬件；dry_run 时 backend 只记日志。
        """
        if name not in ("idle", "light", "heavy"):
            return
        value = int(value)
        ctrl = getattr(self, "_uv_controller", None)
        cfg = ctrl.config if ctrl is not None else UndervoltConfig.load(
            self._undervolt_cfg_path())
        cfg.offsets[name] = value
        # 拖动中(save=False)只更新 live config、不写硬件也不落盘；松手(save=True)
        # 才 reapply 当前档位 + 落盘，避免每像素都 spawn 一次 ryzenadj。
        if save:
            if ctrl is not None:
                try:
                    ctrl.reapply()
                except Exception:
                    pass
            cfg.save(self._undervolt_cfg_path())

    def undervolt_pin_static(self, mv: int = -20) -> dict:
        """「关闭降压」按钮：关闭自动控制器，并立即把电压钉在固定档位。

        - 先把 enabled 置 False 并持久化（后续 tick 不再改电压）。
        - **不调用 reapply()**（它会在 enabled=False 时 reset 归零，会覆盖刚写的
          -20），改为直接 backend.apply_coall(mv) 写一次固定值。
        - 这样效果是「一键回到稳定 -20」：自动关闭、电压钉在 -20（你笔记里 -20
          是键盘安全的稳定值），不会因下一 tick 被状态机刷回 0 或 -50。

        dry_run 模式下 backend 只记日志不写电压（双重保险），但 enabled 仍置 False
        并持久化，逻辑开关状态保持一致。

        返回最新 flags 供 UI 同步。
        """
        ctrl = getattr(self, "_uv_controller", None)
        cfg = ctrl.config if ctrl is not None else UndervoltConfig.load(
            self._undervolt_cfg_path())
        was = bool(cfg.enabled)
        cfg.enabled = False
        if ctrl is not None:
            if was:
                # 停掉自动：reset 会归零，但下方会立即重新钉到 mv。
                try:
                    ctrl.backend.reset()
                except Exception:
                    pass
            try:
                ctrl.backend.apply_coall(mv)
            except Exception:
                pass
            ctrl.last_applied_mv = mv
            self._uv_last["state"] = "pinned"
            self._uv_last["mv"] = mv
        cfg.save(self._undervolt_cfg_path())
        return self.undervolt_get_flags()

    def undervolt_status(self) -> dict:
        """返回降压卡片需要的实时状态快照（每 tick 由面板 sync 读取）。

        字段：
          enabled      : 自动控制器总开关
          heavy_enabled: 是否允许 -50 重载档
          state        : 状态机当前档位（idle/light/heavy/pinned）
          mv           : 当前/最近一次实际下发的 CO 偏移（mV）
          heavy_validated: -50 键盘受控验证是否通过
        """
        ctrl = getattr(self, "_uv_controller", None)
        if ctrl is not None:
            cfg = ctrl.config
            state = self._uv_last.get("state") or ctrl.state
            mv = self._uv_last.get("mv", 0)
            if ctrl.config.enabled and state in ("", None):
                # 首帧未触发 on_change：用控制器当前档位兜底
                state = ctrl.state
                mv = ctrl.last_applied_mv
            return {
                "enabled": bool(cfg.enabled),
                "heavy_enabled": bool(cfg.heavy_enabled),
                "heavy_validated": bool(cfg.heavy_validated),
                "state": state or "idle",
                "mv": int(mv),
            }
        cfg = UndervoltConfig.load(self._undervolt_cfg_path())
        return {
            "enabled": bool(cfg.enabled),
            "heavy_enabled": bool(cfg.heavy_enabled),
            "heavy_validated": bool(cfg.heavy_validated),
            "state": "idle",
            "mv": 0,
        }

    # --- 游戏白名单（冻结卡片「标为游戏」实时维护）-------------------
    def undervolt_get_whitelist(self) -> list[str]:
        """返回当前游戏白名单（读 live config，无则读文件）。"""
        ctrl = getattr(self, "_uv_controller", None)
        cfg = ctrl.config if ctrl is not None else UndervoltConfig.load(
            self._undervolt_cfg_path())
        return list(cfg.whitelist)

    def undervolt_add_whitelist(self, exe: str) -> bool:
        """实时把 exe 加入游戏白名单并落盘。已存在则返回 False（幂等）。

        改的是控制器持有的同一份 live config 对象（状态机每 tick 读它），
        故下一秒即生效，无需重启。
        """
        exe = self._norm_whitelist_exe(exe)
        if not exe:
            return False
        ctrl = getattr(self, "_uv_controller", None)
        cfg = ctrl.config if ctrl is not None else UndervoltConfig.load(
            self._undervolt_cfg_path())
        if exe in cfg.whitelist:
            return False
        cfg.whitelist.append(exe)
        cfg.save(self._undervolt_cfg_path())
        return True

    def undervolt_remove_whitelist(self, exe: str) -> bool:
        """实时把 exe 移出游戏白名单并落盘。不存在则返回 False。"""
        exe = self._norm_whitelist_exe(exe)
        if not exe:
            return False
        ctrl = getattr(self, "_uv_controller", None)
        cfg = ctrl.config if ctrl is not None else UndervoltConfig.load(
            self._undervolt_cfg_path())
        if exe not in cfg.whitelist:
            return False
        cfg.whitelist.remove(exe)
        cfg.save(self._undervolt_cfg_path())
        return True

    @staticmethod
    def _norm_whitelist_exe(exe: str) -> str:
        """取 basename + 小写，兼容用户粘完整路径 / 带空格。空串返回 ''。"""
        s = (exe or "").strip()
        if not s:
            return ""
        return os.path.basename(s).lower()

    # --- 性能监控聚合（PerfMon 卡片每 ~1s 只读读取）-------------------
    def perfmon_snapshot(self) -> dict:
        """聚合「性能监控」卡片需要的实时状态快照。

        字段（全部只读，聚合自共享数据源，零额外采样）：
          window          : 前台进程名（game 优先；否则任意前台 exe）
          is_game        : 前台是否为游戏进程
          cpu_avg        : CPU 平均核心占用（%）
          cpu_max        : CPU 最忙核心占用（%）
          gpu            : 系统 GPU 占用（%）
          load           : 负载档位（idle/light/heavy/pinned）
          load_label     : 负载档位中文标签
          undervolt_mv   : 当前/最近一次实际下发的 CO 偏移（mV）
          undervolt_enabled: 自动降压按钮是否开启（控制器总开关 enabled，独立于当前是否已把 CO 写进硬件）
          undervolt_state: 状态机判定档位（idle/light/heavy/pinned）——与 undervolt_mv 对比可知是否被门控拦截
          undervolt_heavy_validated: -50 键盘受控验证是否通过（heavy 档被拦到 -20 时为 False）
          frame_unlocked : 锁帧是否已解锁（未锁帧=True）
          frame_state    : 锁帧状态字符串
          gpu_locked_freq: 当前 GPU 锁定频率（MHz），未锁定为 None
          gpu_lock_state : GPU 锁定状态字符串
          # —— 实测通道（HWiNFO SMI 独立测量参照 + RTSS 共享内存实测帧率）——
          # 「双重检验」：目标 vs 实测。HWiNFO 不在线 / 数据陈旧时实测列回退
          # 占位符，绝不报错、绝不进入控制闭环。
          tel_fresh      : HWiNFO 共享内存数据是否新鲜（免费版 12h 停更守卫）
          tel_ok         : 是否有任一实测通道命中
          gpu_freq_actual: HWiNFO 实测 GPU 频率（MHz，优先 "(有效)"）
          tdp_actual     : HWiNFO 实测封装功耗（W）
          temp_cpu       : HWiNFO 实测 CPU 封装温度（℃）
          temp_gpu       : HWiNFO 实测 GPU 温度（℃）
          gpu_freq_target: 静态锁频目标（MHz；仅 gpu_lock=True 参与偏差判定，
                            浮动有追赶相位差不判定）
          tdp_set        : TDP 滑块设定（W）
          fps_actual     : RTSS 实测帧率（前台游戏 PID 匹配）
          fps_target     : RTSS 锁帧目标（仅 lock_enabled 时有）
          gpu_dev_alert  : 静态锁频偏差 >15% 且连续 3 tick（约 9s）
          fps_dev_alert  : 实测帧率 < 目标 80% 且连续 3 tick
        """
        out: dict = {
            "window": None, "is_game": False,
            "cpu_avg": None, "cpu_max": None, "gpu": None,
            "load": "idle", "load_label": "空闲",
            "undervolt_mv": 0, "undervolt_enabled": False,
            "undervolt_state": "idle", "undervolt_heavy_validated": False,
            "frame_unlocked": True, "frame_state": "未锁帧(解锁)",
            "gpu_locked_freq": None, "gpu_lock_state": "未锁定(自动)",
        }
        # 1) 实时指标（全应用共享 MetricSampler，O(1) 读）
        try:
            snap = MetricSampler.instance().snapshot
            out["cpu_avg"] = snap.mean_core
            out["cpu_max"] = snap.max_core
            out["gpu"] = snap.gpu_pct
        except Exception:
            pass
        # 2) 前台运行窗口（game 优先，否则任意前台 exe）
        try:
            from ..core.game_detect import (find_foreground_game,
                                            find_foreground_pid, get_exe_by_pid)
            g = find_foreground_game()
            if g:
                out["window"] = g[1]
                out["is_game"] = True
            else:
                pid = find_foreground_pid()
                if pid:
                    out["window"] = get_exe_by_pid(pid)
        except Exception:
            pass
        # 3) 负载判定 + 当前降压值（复用降压状态机）
        try:
            uv = self.undervolt_status()
            st = uv.get("state", "idle") or "idle"
            mv = int(uv.get("mv", 0))
            out["load"] = st
            out["load_label"] = {
                "idle": "空闲", "light": "轻载",
                "heavy": "重载", "pinned": "已固定",
            }.get(st, "空闲")
            out["undervolt_mv"] = mv
            out["undervolt_enabled"] = bool(uv.get("enabled"))
            out["undervolt_state"] = st
            out["undervolt_heavy_validated"] = bool(uv.get("heavy_validated"))
        except Exception:
            pass
        # 4) 锁帧状态（rtss 区块）
        try:
            rtss = self._app.blocks.get("rtss")
            if rtss is not None:
                if getattr(rtss, "lock_enabled", False):
                    out["frame_unlocked"] = False
                    out["frame_state"] = f"已锁帧 {int(getattr(rtss, 'fps', 0))} FPS"
                else:
                    out["frame_unlocked"] = True
                    out["frame_state"] = "未锁帧(解锁)"
        except Exception:
            pass
        # 5) GPU 锁定频率（power 区块）
        try:
            power = self._app.blocks.get("power")
            if power is not None:
                if getattr(power, "gpu_lock", False):
                    freq = int(getattr(power, "gpu", 0))
                    out["gpu_locked_freq"] = freq
                    out["gpu_lock_state"] = f"静态锁定 {freq} MHz"
                elif getattr(power, "gpu_float_enabled", False):
                    freq = int(getattr(power, "gpu_current_freq", 0))
                    out["gpu_locked_freq"] = freq
                    out["gpu_lock_state"] = f"浮动锁定 {freq} MHz"
                else:
                    out["gpu_locked_freq"] = None
                    out["gpu_lock_state"] = "未锁定(自动)"
        except Exception:
            pass
        # 6) 实测通道（HWiNFO SMI 独立测量参照 + RTSS 实测帧率）—— 双重检验
        #    数据只用于显示与偏差告警；控制闭环（浮动/自动锁帧/降压）绝不读它。
        try:
            from ..core.hwinfo_smi import read_telemetry
            tel = read_telemetry()
            out["tel_fresh"] = bool(tel.fresh)
            out["tel_ok"] = bool(tel.source_ok)
            out["gpu_freq_actual"] = tel.gpu_freq
            out["tdp_actual"] = tel.tdp
            out["temp_cpu"] = tel.temp_cpu
            out["temp_gpu"] = tel.temp_gpu
            out["gpu_util"] = tel.gpu_util
            out["batt_charge_rate"] = tel.batt_rate
        except Exception:
            pass
        # 6b) 目标值（power 区块设定）。仅「静态锁定」参与 GPU 偏差判定：
        #     浮动锁频本身在追赶目标（HWiNFO 轮询 3s vs 浮动 tick ~1s），
        #     偏差是常态，告警只会误报。
        try:
            power = self._app.blocks.get("power")
            if power is not None:
                if getattr(power, "gpu_lock", False):
                    out["gpu_freq_target"] = int(getattr(power, "gpu", 0) or 0)
                out["tdp_set"] = int(getattr(power, "tdp", 0) or 0)
        except Exception:
            pass
        # 6c) RTSS 实测帧率（按前台游戏 PID）+ 锁帧目标
        try:
            from ..platform.rtss_smi import read_fps
            from ..core.game_detect import find_foreground_pid
            pid = find_foreground_pid()
            out["fps_actual"] = read_fps(pid)
        except Exception:
            out["fps_actual"] = None
        try:
            rtss = self._app.blocks.get("rtss")
            if rtss is not None and getattr(rtss, "lock_enabled", False):
                out["fps_target"] = int(getattr(rtss, "fps", 0) or 0)
        except Exception:
            pass
        # 7) 偏差告警状态（连续 3 tick 超阈值 → 面板黄字 + toast；HWiNFO 轮询
        #    3s，约 9s 才确认一次，避免瞬时抖动误报）
        try:
            dev = self.__dict__.setdefault("_perfmon_dev", {"gpu": 0, "fps": 0})
            tgt_g = out.get("gpu_freq_target")
            act_g = out.get("gpu_freq_actual")
            # 负载门控（2026-08-21 修复误报）：GPU 空闲时有效频率本就会掉到最低
            # 档（省电），与锁定目标的偏差大是正常现象——「锁频未生效」只在 GPU
            # 有真实负载（≥30%）且频率仍远低于目标时才成立。优先用 HWiNFO 自家
            # gpu_util（与 gpu_freq_actual 同源同周期，自洽不打架），读不到时用
            # PDH gpu_pct 兜底；两个都无 → 宁缺毋滥，不告警。
            util_g = out.get("gpu_util")
            if util_g is None:
                util_g = out.get("gpu")  # MetricSampler PDH GPU 占用（兜底）
            gbad = bool(tgt_g and act_g and out.get("tel_fresh")
                        and isinstance(util_g, (int, float))
                        and util_g == util_g  # 非 NaN
                        and util_g >= 30.0
                        and abs(act_g - tgt_g) / max(tgt_g, 1) > 0.15)
            dev["gpu"] = dev["gpu"] + 1 if gbad else 0
            out["gpu_dev_alert"] = dev["gpu"] >= 3
            tgt_f = out.get("fps_target")
            act_f = out.get("fps_actual")
            fbad = bool(tgt_f and act_f and act_f < tgt_f * 0.8)
            dev["fps"] = dev["fps"] + 1 if fbad else 0
            out["fps_dev_alert"] = dev["fps"] >= 3
        except Exception:
            pass
        return out

    # --- 独立性能监控弹窗（全局唯一，置顶，主程序最小化时仍可见） ----
    def toggle_perfmon_popup(self) -> None:
        """切换「独立性能监控弹窗」的显隐（全局唯一）。"""
        if self._perfmon_popup is not None:
            self._perfmon_popup.close()
            self._perfmon_popup = None
            return
        # 定位性能监控卡片在屏幕上的当前位置（像卡片分裂出来），并沿用其实际
        # 显示尺寸作为弹窗初始大小，使纵横比与原卡片一致。
        # 注意：必须用 mapToGlobal(QPoint(0,0)) 取卡片自身原点（局部坐标），
        # 不能传 geometry().topLeft()（那是父坐标，会导致偏移 + Qt 警告）。
        pos = None
        card = None
        for p in self.panels:
            if isinstance(p, PerfMonPanel):
                card = p
                try:
                    pos = p.mapToGlobal(QPoint(0, 0))
                except Exception:
                    pos = None
                break
        try:
            card_size = (card.size() if (card is not None
                         and card.width() > 0 and card.height() > 0) else None)
            popup = PerfMonPopup(self, initial_size=card_size)
            if pos is not None:
                popup.move(pos)
            popup.show()
            popup.raise_()  # 打开即置于主窗口之上（性能监控是「允许在上」的例外）
        except Exception as exc:  # noqa: BLE001
            import traceback
            tb = traceback.format_exc()
            try:
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "..", "..", "launch.log"), "a",
                          encoding="utf-8") as fh:
                    fh.write("[perfmon弹窗] 创建失败，已跳过（不影响主程序）：\n"
                             + tb + "\n")
            except Exception:
                pass
            print("[perfmon弹窗] 创建失败：", exc)
            return
        self._perfmon_popup = popup

    def _close_perfmon_popup(self) -> None:
        """主程序退出时确保弹窗一并关闭（无父对象，需手动清理）。"""
        if self._perfmon_popup is not None:
            try:
                self._perfmon_popup.close()
            except Exception:
                pass
            self._perfmon_popup = None

    def _keep_perfmon_on_top(self) -> None:
        """性能监控弹窗是「允许浮在主窗口之上」的唯一例外。

        主窗口在恢复 / 热键 / 手柄 Start / 被点击激活时都会把自己 ``raise_``
        到最前，可能把同样置顶的性能监控弹窗盖在下面。本方法在这些动作之后把
        弹窗重新抬到主窗口之上，使其始终保持「例外允许在上」的层级。

        只用 ``raise_()``（只改 z 序、不改激活态），因此不会抢走主窗口的键盘
        焦点；弹窗本身始终携带 ``WindowStaysOnTopHint``，故它只高于主窗口、
        不会高于之后打开的其它普通窗口（那部分由被动置顶标志负责）。
        """
        popup = getattr(self, "_perfmon_popup", None)
        if popup is None:
            return
        try:
            if popup.isVisible():
                popup.raise_()
        except Exception:  # noqa: BLE001
            # C++ wrapper may have been deleted without our knowledge; ignore.
            pass

    # --- freeze/unfreeze async runner ------------------------------
    def _run_freeze_async(self, work: Any, done: Any) -> None:
        """Runner injected into FreezeBlock: run ``work()`` on a background
        QThread, then call ``done(result)`` back on the UI thread."""
        worker = _FreezeWorker(work)
        worker._done_cb = done  # type: ignore[attr-defined]
        self._freeze_workers.append(worker)
        worker.done_sig.connect(
            self._on_freeze_worker_done, Qt.ConnectionType.QueuedConnection
        )
        worker.start()

    @pyqtSlot(object)
    def _on_freeze_worker_done(self, res: Any) -> None:
        """Delivered on the UI thread (queued): apply the freeze result via the
        block's callback, then retire the worker."""
        worker = self.sender()
        done = getattr(worker, "_done_cb", None)
        try:
            if done is not None:
                done(res)
        finally:
            if worker is not None:
                worker.quit()
                worker.wait(200)
                try:
                    self._freeze_workers.remove(worker)
                except (ValueError, AttributeError):
                    pass

    # --- panel (re)build -------------------------------------------
    def _clear_inner_layout(self) -> None:
        """Remove every widget AND spacer item from the scroll-content layout.

        The trailing ``addStretch(1)`` spacer must be removed too.  If it
        survives a rebuild, the old panels are pulled out first and the
        orphaned spacer ends up at the TOP of the layout, pushing the new cards
        to the vertical middle of the viewport — the "切页后卡片跑到页面中间"
        regression.  Clearing here guarantees every (re)build has exactly one
        stretch, always at the bottom, so cards stay top-aligned.
        """
        lay = self.inner_layout
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                # 先显式释放面板持有的后台资源（如 SMT 采样线程），再销毁。
                # 否则 deleteLater 会连带销毁仍在运行的 QThread，触发原生崩溃
                # 'QThread: Destroyed while thread '' is still running'。
                rel = getattr(w, "release", None)
                if callable(rel):
                    try:
                        rel()
                    except Exception:
                        pass
                w.setParent(None)
                w.deleteLater()
            # Spacer items carry no widget; dropping the QLayoutItem suffices.

    def _build_panels(self) -> None:
        """Build the panels for the *current page* (or *every page* while the
        button-style editor is open) and reset scroll to top.

        Pagination is driven by ``app.render_pages()`` (the same expanded
        order the layout editor uses), so a card the user moved to page 2 in
        the editor really does land on page 2 here.  Each token may be a base
        block type or ``tile#N`` for an extra tile block; both render as their
        own panel via ``_add_panel_for_token``.

        When ``self._editor_all_pages`` is set (the button-style editor is
        open) we build every page's tokens so the editor's button list can show
        — and style — buttons from cards on *all* pages, not just the visible
        one.  The HUD is hidden behind the editor overlay while this is true, so
        the stacked pages are never seen by the user.
        """
        # Start from a clean slate so a leftover stretch spacer (or any stale
        # panel) never leaks into the new page's layout.  See _clear_inner_layout.
        self._clear_inner_layout()
        app = self._app
        pages = app.render_pages()
        if not pages:
            return
        if getattr(self, "_editor_all_pages", False):
            # Instantiate every page's panels once (block order is a partition,
            # so each token appears at most once across all pages — no dupes).
            tokens: list[str] = []
            seen: set[str] = set()
            for page in pages:
                for tok in page:
                    if tok not in seen:
                        seen.add(tok)
                        tokens.append(tok)
            for token in tokens:
                try:
                    self._add_panel_for_token(token)
                except Exception:
                    # A single card's _setup throwing (bad tile config, transient
                    # EC/hardware probe, etc.) must never abort the whole page
                    # build — that would leave _controller still pointing at the
                    # just-deleted panels and crash the next gamepad poll.
                    continue
        else:
            nav = app.nav
            idx = nav.current_page if nav is not None else 0
            idx = max(0, min(idx, len(pages) - 1))
            for token in pages[idx]:
                try:
                    self._add_panel_for_token(token)
                except Exception:
                    # Same rationale as above: one bad card must not take down
                    # the page switch, or the interaction layer is left dangling.
                    continue
        # Absorb any leftover vertical space so cards keep their natural height
        # instead of being stretched to fill the scroll viewport.
        self.inner_layout.addStretch(1)
        # Always show a (re)built page from the top — QScrollArea otherwise
        # keeps the previous page's scroll offset, which can make the new cards
        # appear mid-viewport.
        QTimer.singleShot(0, lambda: self._scroll.verticalScrollBar().setValue(0))

    def _make_panel_for_token(self, token: str) -> Any:
        """Build (but do NOT register/layout) one panel for an expanded-order
        token and return it.

        ``tile#N`` → the Nth extra tile block panel; any other token → the
        base block panel.  Mirrors the PS1 where each ``New-TileBlockPanel``
        creates a separate ``RoundedPanel`` in the scroll container.

        The panel's ``controller`` is wired to the live controller so its
        ``_setup``/``sync`` populate correctly, but — unlike
        :meth:`_add_panel_for_token` — it is NOT registered with the controller
        nor added to the live ``self.panels`` list.  That keeps it safe to use
        for the offscreen editor scan source (we never want offscreen clones to
        pollute the live panel registry / nav targets).
        """
        app = self._app
        base = token.split("#")[0] if "#" in token else token
        block = app.registry.get(base)
        instance = app.blocks.get(base)
        if block is None or instance is None:
            return None
        if base == "tile" and "#" in token:
            # Extra tile block panel.
            i = int(token.split("#")[1])
            extras = getattr(instance, "extra_blocks", None) or []
            if i < 0 or i >= len(extras):
                return None
            wrapper = _TileBlockPanelProxy(extras[i], instance)
            panel = make_panel(base, wrapper, extras[i].block_name, block.color,
                               block_key=token, cfg=app.config_manager.config)
        else:
            panel = make_panel(base, instance, block.name, block.color,
                               block_key=token, cfg=app.config_manager.config)
        panel.persist = app.save_all
        panel.controller = self._controller
        return panel

    def _add_panel_for_token(self, token: str) -> None:
        """Build one panel for an expanded-order token and add it to the live
        HUD (registered with the controller + tracked in ``self.panels``)."""
        panel = self._make_panel_for_token(token)
        if panel is None:
            return
        self._controller.register_panel(panel)
        self.inner_layout.addWidget(panel)
        self.panels.append(panel)

    def rebuild(self, restyle: bool = True, reload_tiles: bool = True,
                heavy: bool = True) -> None:
        """Rebuild panels.

        ``restyle``      — re-apply the L2 button style + L1 presets to every
                           live button (expensive: full findChildren traversal).
                           Only needed after an editor save / theme change, NOT
                           on a plain page switch (same block types, same style).
        ``reload_tiles`` — re-read the tile block's config from disk.  Only
                           needed when tiles were actually edited, NOT on a page
                           switch.
        ``heavy``        — run the heavy background refresh (subprocess spawn for
                           power/rtss/freeze) immediately after rebuild.  On a
                           page switch we skip this (it caused the "切换页面极度
                           卡顿") and just repaint from current block state; the
                           live timers pick the new page up within one tick.
        """
        if reload_tiles:
            # Refresh tile block's internal state from config (new extra blocks
            # may have been added since boot).
            tile_instance = self._app.blocks.get("tile")
            if tile_instance and hasattr(tile_instance, "load_config"):
                try:
                    tile_instance.load_config()
                except Exception:
                    pass
        # Drop the old page's panels.  The actual widget/layout teardown (and
        # the trailing stretch spacer) is handled inside _build_panels() via
        # _clear_inner_layout(), so we only need to reset our bookkeeping here.
        # --- page-switch crash guard -------------------------------------
        # A page switch (gamepad RB / LB, page-bar button, number key) deletes
        # and recreates every visible panel synchronously.  Under a
        # FramelessWindowHint + WA_TranslucentBackground window this
        # delete -> create -> update races the DWM compositor and raises a native
        # access violation that Python try/except cannot catch (the "switch page
        # -> silent crash" symptom — see _on_page_switched.__doc__).  We
        # neutralise it here by (1) pausing the 30ms gamepad poll so it cannot
        # repaint a half-built window, and (2) coalescing every intermediate
        # repaint with setUpdatesEnabled(False) so the translucent window is only
        # ever painted once, in a consistent state, after the controller has been
        # rebound to the new panels.  The host window also gates navigation with
        # InteractionController.set_rebuilding().
        _ctrl = getattr(self, "_controller", None)
        _was_gp_active = False
        if _ctrl is not None:
            try:
                _was_gp_active = _ctrl._gp_timer.isActive()
                _ctrl.stop_gamepad()
                _ctrl.set_rebuilding(True)
            except Exception:
                pass
        try:
            self.panels.clear()
            try:
                self.setUpdatesEnabled(False)
                self._build_panels()
            finally:
                # Rebind MUST run even if a card's _setup threw mid-build: the
                # old panels have already been deleteLater()'d, so leaving the
                # controller pointing at them would crash the next gamepad poll
                # (native access violation on a destroyed widget — no try/except
                # can catch it).  Rebinding to whatever panels *did* build
                # (possibly an empty list) keeps the interaction layer consistent
                # and the app alive.
                self._controller.rebind()
                self.setUpdatesEnabled(True)
        finally:
            if _ctrl is not None:
                try:
                    _ctrl.set_rebuilding(False)
                    if _was_gp_active:
                        _ctrl.start_gamepad()
                except Exception:
                    pass
        # Apply the new theme background (in case it changed).
        self._bg = self._theme_bg()
        if restyle:
            # Re-apply the active button style (L2) + any bound L1 presets to
            # the freshly created buttons — mirrors PS1 Set-GlobalButtonStyle
            # after a layout rebuild.  Excludes the edit overlay subtree so
            # editor controls are never restyled.
            self._apply_button_styles()
        self.update()
        # Keep the page indicator in sync (page count / current page may have
        # changed after an editor save, layout edit, or tile-block add).
        self._build_page_bar()
        if heavy:
            QTimer.singleShot(0, self._do_refresh)
        else:
            # Cheap repaint from current block state — no subprocess spawn, no
            # full restyle.  This is what a page switch uses.
            QTimer.singleShot(0, self._bridge.sync_all)

    def _force_layout_pass(self) -> None:
        """Lay out the (possibly hidden) HUD so freshly-built buttons report
        their real on-screen widths.

        When the button-style editor opens we rebuild the HUD with every
        page's panels while it is hidden behind the editor overlay.  ``rebuild``
        constructs the widgets, but the Qt layout engine only computes their
        geometry on the next event-loop tick — so a *synchronous* call to
        ``render_canvas`` (which clones each button at its measured width)
        would read the default 640x480 unset size and shrink the preview clones
        down to their text width.

        A plain event-loop tick computes geometry the same way the visible page
        does (natural widths, not over-stretched), and since the HUD is hidden
        behind the overlay at this point it is not repainted — so there is no
        flicker.  ``QLayout.activate()`` was tried but it over-stretches widgets
        to the scroll content's (too-wide) width, so we deliberately use the
        real layout pass instead."""
        QApplication.processEvents()

    # --- page indicator bar ------------------------------------------
    def _page_bar_styles(self) -> tuple[str, str]:
        cur = ("QPushButton { background: rgba(255,215,120,0.22); color: #ffd778;"
               " border: 1px solid rgba(255,215,120,0.55); border-radius: 5px;"
               " font-weight: bold; padding: 2px 0px; }")
        normal = ("QPushButton { background: rgba(255,255,255,0.06); color: #b0bdd0;"
                  " border: 1px solid rgba(255,255,255,0.12); border-radius: 5px;"
                  " padding: 2px 0px; }")
        return cur, normal

    def _build_page_bar(self) -> None:
        """(Re)build the top page indicator: [1] [2] … [N], current page gold.

        Only the numeric page buttons remain (no ‹/› arrows) — switching pages
        is done by clicking a page number.
        """
        if getattr(self, "_page_bar", None) is not None:
            self.outer.removeWidget(self._page_bar)
            self._page_bar.deleteLater()
            self._page_bar = None
        app = self._app
        pages = app.render_pages()
        total = max(1, len(pages))
        nav = app.nav
        cur = nav.current_page if nav is not None else 0
        cur = max(0, min(cur, total - 1))
        cur_style, normal_style = self._page_bar_styles()

        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        for i in range(total):
            b = QPushButton(str(i + 1))
            b.setFixedSize(26, 22)
            # Tag the page indicator buttons so the button-style editor's
            # collect_button_groups skips them — they are HUD navigation chrome,
            # not card buttons, and must not appear (duplicated) in the editor's
            # preview list.
            b.setProperty("page_nav", True)
            b.setStyleSheet(cur_style if i == cur else normal_style)
            b.clicked.connect(lambda _=False, idx=i: self._goto_page(idx))
            lay.addWidget(b)
        lay.addStretch(1)

        self._page_bar = bar
        # Insert just below the header (header is layout index 0).
        self.outer.insertWidget(1, bar)
        # Hide the bar immediately if we are currently inside the editor
        # (e.g. a save/repanel rebuild fired while editing) — otherwise it
        # would flash back to visible.
        self._apply_page_bar_visibility()

    def _apply_page_bar_visibility(self) -> None:
        """Show the top page indicator bar in the HUD, hide it while the layout
        editor is open (so the [1][2]… page-switch buttons don't poke through
        the editor overlay).  Called after every (re)build and every editor
        open/close so a mid-edit repanel can never re-show the bar."""
        if self._page_bar is None:
            return
        edit = bool(getattr(self._controller, "_edit_mode", False))
        self._page_bar.setVisible(not edit)

    def _on_gp_connection_changed(self, connected: bool, name: str) -> None:
        """Update the header gamepad-status icon + tray tooltip when a
        controller connects / disconnects (driven by the SDL backend's hot-plug
        detection).  The header shows a compact glyph (green when connected,
        hidden when detached); the full status lives in the tray tooltip."""
        tip = (f"凝冰结魄 — 手柄: 已连接 ({name})" if (connected and name)
               else "凝冰结魄 — 手柄: 已连接" if connected
               else "凝冰结魄 — 手柄: 未检测到（可用键盘）")
        if getattr(self, "_gp_status", None) is not None:
            try:
                self._gp_status.set_state(connected, name)
            except Exception:
                pass
        tray = getattr(self, "_tray", None)
        if tray is not None:
            try:
                tray.setToolTip(tip)
            except Exception:
                pass

    def _on_card_focus_changed(self, old_panel: int, new_panel: int) -> None:
        """焦点切换到另一个功能卡片时，关闭仍打开的疑惑解答弹窗。

        避免弹窗停在旧卡片的解答内容上；切到新卡片后应让它重新按新卡片触发。

        例外：若弹窗此时正好显示「新卡片」的解答（例如点该卡片标题直接开
        FAQ 并同步把焦点切过去），则不关——否则会立刻把刚打开的弹窗关掉。
        """
        faq = getattr(self, "_faq", None)
        if faq is None or not faq.is_open():
            return
        # 新卡片的 block_type 与弹窗当前显示的 block_type 一致 -> 保持打开。
        panels = getattr(self, "panels", []) or []
        new_bt = None
        if 0 <= new_panel < len(panels):
            new_bt = getattr(panels[new_panel], "block_type", None)
        if faq.current_block() == new_bt:
            return
        faq.close()

    def _goto_page(self, index: int) -> None:
        """Switch to an absolute page (page-bar button click)."""
        app = self._app
        if app.nav is None:
            return
        app.goto_page(index)

    def _on_page_switched(self) -> None:
        """Rebuild the visible panels for the new page + refresh the indicator.

        Driven by both the page-bar buttons and gamepad/keyboard page switches
        (via ``app._page_changed_cb``).

        Uses the LIGHTWEIGHT rebuild path: no button restyle, no tile disk
        reload, no immediate heavy subprocess spawn — those were the three
        contributors to the "切换页面极度卡顿" complaint.  The new page's panels
        are built from current block state and repainted via sync_all(); the
        live refresh timers keep them current.

        While the button-style editor is open (``_editor_all_pages``) every page
        is already instantiated, so a page switch must NOT collapse the HUD back
        to a single page — that would re-hide the other pages' buttons from the
        editor's list.  We simply ignore the switch in that mode.
        """
        # 切换页面时关闭疑惑解答弹窗，避免显示过时内容。
        if getattr(self, "_faq", None) is not None:
            self._faq.close()
        if getattr(self, "_editor_all_pages", False):
            return
        # 非按钮编辑器模式下，关闭左侧按钮列表浮窗（编辑器内时由 hideEvent 处理）。
        ov = getattr(self, "_edit_overlay", None)
        if ov is not None:
            bp = getattr(ov, "_button_panel", None)
            popup = getattr(bp, "_list_popup", None) if bp is not None else None
            if popup is not None:
                try:
                    popup.close_popup()
                except Exception:
                    pass
        # 不在调用方的栈内同步重建。页面切换由高频手柄轮询 QTimer 槽（以及键盘 /
        # 页面条按钮）触发；在 30ms 轮询槽内同步执行「deleteLater 旧面板 + 新建面板
        # + update()」会与 FramelessWindowHint + WA_TranslucentBackground 窗口的 DWM
        # 合成竞争，触发原生崩溃（Python try/except 无法捕获，正是「静默闪退」的表现）。
        # 改为延迟到下一个事件循环 tick 执行，并用 _page_rebuild_pending 去重——
        # 快速连按只触发一次重建。键盘 / 手柄 / 页面条因此走同一条安全路径。
        if getattr(self, "_page_rebuild_pending", False):
            return
        self._page_rebuild_pending = True
        QTimer.singleShot(0, self._do_page_rebuild)

    def _do_page_rebuild(self) -> None:
        self._page_rebuild_pending = False
        if getattr(self, "_editor_all_pages", False):
            return
        self.rebuild(restyle=False, reload_tiles=False, heavy=False)

    def _apply_button_styles(self) -> None:
        """Push the active L2 style + bound L1 presets onto every live HUD button.

        Collects buttons via :func:`collect_button_groups` (grouped by block,
        stable ``block_type#local_index`` keys) so the L1 binding map survives
        panel rebuilds.  The edit overlay is excluded so its own controls keep
        their editor styling, and the floating button-list popup is excluded so
        its preview clones never shift HUD button indices.
        """
        try:
            popup = None
            try:
                popup = getattr(self._edit_overlay._button_panel, "_list_popup", None)
            except Exception:
                pass
            groups = collect_button_groups(self, exclude=self._edit_overlay, popup=popup)
            live: list = []
            key_map: dict[int, str] = {}
            for bt, items in groups.items():
                if bt == "freeze":
                    continue  # freeze buttons keep their native panel styling
                for key, btn in items:
                    live.append(btn)
                    key_map[id(btn)] = key
            apply_styles_to_buttons(live, key_map)
        except Exception:
            pass

    def sync_all(self) -> None:
        """Public repaint hook (used by item editors on dialog close)."""
        self._bridge.sync_all()

    # -- layout editor (in-window overlay) ---------------------------
    def _open_editor(self) -> None:
        """Toggle the full-window edit overlay (F2)."""
        if self._controller._edit_mode:
            self._close_editor()
            return
        # 进入任一编辑界面（布局编辑 / 卡片编辑 / 按钮风格 / 呼吸灯 …）前，先关闭
        # 仍打开的疑惑解答弹窗——它属于主页面某张卡片，进入编辑器即失去上下文。
        faq = getattr(self, "_faq", None)
        if faq is not None:
            faq.close()
        self._edit_overlay.open_layout()
        self._body.setCurrentWidget(self._edit_overlay)
        self._controller.set_edit_mode(True)
        self._controller.set_host(self._edit_overlay)
        self._controller.start_gamepad()
        # Hide the HUD page indicator bar so its [1][2]… buttons don't show
        # through the editor overlay.
        self._apply_page_bar_visibility()

    def _close_editor(self) -> None:
        """Return to the HUD, reflecting any layout / theme changes."""
        # Drop the all-pages mode (set while the button-style editor was open)
        # so the HUD returns to showing only the current page's cards.
        self._editor_all_pages = False
        self._body.setCurrentWidget(self._scroll)
        self._controller.set_edit_mode(False)
        self._controller.set_host(self.inner)
        self.rebuild()

    # -- shell gamepad actions + global hotkey -------------------------
    def _remember_prev_foreground(self) -> None:
        """记录当前前台窗口（通常是游戏窗口）的 HWND，供之后 Start 一键回游戏。

        跳过凝冰结魄自身的窗口句柄。在『唤出 HUD』之前调用，这样下次按 Start
        时 ``_return_to_game`` 才知道该把焦点还给谁。best-effort，异常全部吞掉
        ——记不到也不影响唤出本身。"""
        try:
            self._prev_game_hwnd = 0
            if getattr(self, "_offscreen", False):
                return
            hwnd = int(self.winId())
            fg = ctypes.windll.user32.GetForegroundWindow()
            if fg and fg != hwnd:
                self._prev_game_hwnd = fg
        except Exception:  # noqa: BLE001
            self._prev_game_hwnd = 0

    def _return_to_game(self) -> None:
        """手柄 Start（HUD 已在屏上时）：把 HUD 收进托盘；若记到了有效游戏窗口，则先把焦点还给它。

        关键时序——在 HUD 仍处前台时先用 ``ShowWindow(SW_RESTORE)`` 恢复游戏
        窗口：因为当前进程是前台进程，Windows 允许它把焦点转让给目标窗口，于是
        游戏干净地回到前台（绕开前台锁，比事后强抢稳定得多）；随后再把 HUD 淡出
        隐藏到托盘。这正是『独占全屏游戏被 HUD 抢焦点最小化』的反向操作：不再去
        抢焦点最小化游戏，而是主动把焦点让回游戏（也就是用户要的『一键回游戏』）。

        没有可回的游戏窗口时（记忆失效/重启后清空/从未召唤 HUD 记录过游戏），
        也照样把 HUD 收进托盘——只是不再复原任何原窗口。满足『任何时候（包括
        没打开过任何窗口）按 Start 都能收起 HUD』的用户要求。

        best-effort：任何异常都不影响游戏本体的前台状态。
        """
        try:
            game_hwnd = self._resolve_game_hwnd()
            if game_hwnd:
                # 游戏仍被冻结（suspended）→ 先解冻再回。否则焦点交给一个无法
                # pump 消息的挂起窗口，前台便卡在『冻住的游戏 + HUD 已收起』的
                # 死局，正是用户报告的『摁下 Start 程序卡死』。解冻是异步的
                # （OS NtResumeProcess 秒回），随后把焦点让回已恢复的游戏。
                self._defrost_game_if_frozen(game_hwnd)
                user32 = ctypes.windll.user32
                # HUD 仍是前台时恢复游戏 → 焦点自然让渡，绕开前台锁。
                user32.ShowWindow(game_hwnd, 9)  # SW_RESTORE = 9
                user32.SetForegroundWindow(game_hwnd)
        except Exception:  # noqa: BLE001
            pass
        # HUD 收进托盘（淡出后隐藏）。无论是否有可恢复的游戏窗口都执行——
        # 用户要求『未按过/没打开过窗口时按 Start 也要最小化』。
        try:
            self._minimize_to_tray()
        except Exception:  # noqa: BLE001
            pass

    def _restore_game_only(self) -> None:
        """HUD 已隐藏时，Start 只负责把『被最小化的游戏窗口』恢复到前台，不召唤 HUD。

        仅 best-effort：记不到有效游戏窗口（``_prev_game_hwnd`` 缺失、或句柄已
        失效/被复用/属于桌面外壳）就什么都不做——既不 fallback 抢当前前台窗口
        （重启掌机后会误拉起『关机前的程序』），也绝不召唤 HUD，因为召唤是全局
        热键（Win+Ctrl+F8）的职责，Start 严格只做『回到游戏』。
        """
        try:
            game_hwnd = self._resolve_game_hwnd()
            if not game_hwnd:
                return
            # 游戏仍被冻结 → 先解冻再回（同 _return_to_game 的修复）。
            self._defrost_game_if_frozen(game_hwnd)
            user32 = ctypes.windll.user32
            # SW_RESTORE=9：把被最小化的游戏窗口恢复并交还焦点。HUD 此时已隐藏，
            # 当前进程不是前台进程，但 SetForegroundWindow 在『用户刚按过键（Start）』
            # 的短时间内允许（前台锁对最近输入线程宽松），足够把游戏拉回前台。
            user32.ShowWindow(game_hwnd, 9)
            user32.SetForegroundWindow(game_hwnd)
        except Exception:  # noqa: BLE001
            pass

    def _resolve_game_hwnd(self) -> int:
        """返回经过校验的『游戏窗口』HWND；无效/失效则返回 0。

        校验项：
        * 必须是真实存在的窗口（``IsWindow``）；
        * 不能是桌面/外壳窗口（Progman / WorkerW / Shell_TrayWnd …）；
        * 拥有进程必须仍存活（``OpenProcess`` 可打开）。

        关键：记不到或不合法时**绝不** fallback 到 ``GetForegroundWindow``——
        否则重启掌机（记忆清空）后，Start 会把当前任意一个前台窗口（往往是
        『关机前的程序』重新跑起来）误当作游戏抢焦点，表现为『打开了关机前的
        程序』。Start 的语义是『回到你召唤 HUD 时所在的游戏』，不是『抢当前
        前台窗口』，因此没有有效记忆就什么都不做（HUD 在屏则保持 HUD）。"""
        try:
            self_hwnd = int(self.winId())
            hwnd = getattr(self, "_prev_game_hwnd", 0) or 0
            if hwnd and hwnd != self_hwnd and self._is_window_alive(hwnd):
                return hwnd
        except Exception:  # noqa: BLE001
            pass
        # 记忆失效（重启 / 进程退出 / HWND 被复用）→ 清空，不抢前台。
        try:
            self._prev_game_hwnd = 0
        except Exception:  # noqa: BLE001
            pass
        return 0

    def _is_window_alive(self, hwnd: int) -> bool:
        """窗口是否真实存在、且不是桌面/外壳窗口、且其拥有进程仍存活。

        best-effort：任何异常都按『不可信』返回 False（宁可不让 Start 抢焦点，
        也不要把焦点交给一个失效/被复用的句柄）。"""
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            if not user32.IsWindow(hwnd):
                return False
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, buf, 256)
            cls = buf.value or ""
            # 桌面/外壳/输入法候选等『非游戏』窗口一律排除，避免 Start 把
            # 它们误当游戏抢焦点（重启掌机后前台常是桌面/shell）。
            if cls in ("Progman", "WorkerW", "Shell_TrayWnd",
                       "Shell_SecondaryTrayWnd", "IME", "MSCTFIME UI"):
                return False
            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return False
            # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000：仅探测进程是否存在，
            # 不请求任何危险权限；拿不到句柄即视为进程已退出。
            h = kernel32.OpenProcess(0x1000, False, pid.value)
            if not h:
                return False
            kernel32.CloseHandle(h)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _defrost_game_if_frozen(self, hwnd: int) -> bool:
        """若记忆的游戏窗口所属进程仍被冻结（suspended），经冻结卡片把整组
        解冻，使 Start 能回到一个可响应的游戏（修复『冻结后按 Start 卡死』）。
        best-effort，任何异常都不影响后续的焦点让渡。返回是否发起了解冻。"""
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return False
            fb = self._app.blocks.get("freeze") if getattr(self, "_app", None) else None
            pm = getattr(fb, "pm", None)
            if pm is None or not getattr(pm, "available", False):
                return False
            if not pm.is_suspended(pid.value):
                return False
            if fb is not None and hasattr(fb, "unfreeze_by_pid"):
                return bool(fb.unfreeze_by_pid(pid.value))
        except Exception:  # noqa: BLE001
            pass
        return False

    def _focus_from_start(self) -> None:
        """Gamepad Start = 一键回游戏：最小化凝冰结魄 + 恢复被最小化的游戏窗口。

        严格语义——Start **绝不**召唤 / 呼出凝冰结魄。HUD 的唤出由全局热键
        （Win+Ctrl+F8）负责；Start 只做『回到游戏』这一件事：

        - HUD 在屏（含『游戏前台、HUD 失焦可见』状态）→ 收起 HUD（最小化到托盘）
          并把焦点还给游戏窗口（``_return_to_game``）。
        - HUD 已隐藏/最小化（在托盘）→ 仅 best-effort 把被最小化的游戏窗口恢复
          到前台（``_restore_game_only``），绝不召唤 HUD——否则会把已『回到游戏』
          的状态又翻回 HUD，违背『Start 不呼出』的明确要求。
        """
        try:
            hud_on_screen = self.isVisible() and not (
                self.windowState() & Qt.WindowState.WindowMinimized)
            if hud_on_screen:
                self._return_to_game()
            else:
                # 已隐藏：只恢复游戏窗口（若有记录），绝不召唤 HUD。
                self._restore_game_only()
        except Exception:  # noqa: BLE001
            pass

    def _refocus_after_freeze(self) -> None:
        """冻结成功后把键盘焦点抢回凝冰结魄本体（FreezeBlock 注入的回调）。

        冻结路径里 ``ProcessManager.suspend`` 会先把目标窗口
        ``ShowWindow(SW_MINIMIZE)`` 最小化（挂起后 UI 线程无法处理 SC_MINIMIZE，
        必须先最小化再挂起）。最小化**前台窗口**后 Windows 会把焦点交给别的
        窗口——凝冰结魄虽然置顶（WindowStaysOnTopHint），但『置顶 ≠ 持焦点』，
        于是 HUD 失焦变暗、键盘热键失效，用户没法接着按数字键冻结/解冻下一个。

        这里把焦点拉回来：先走 Qt 原生 ``raise_ + activateWindow``（与托盘恢复
        同一条稳定路径），再延迟 ~100 ms 做 raw-HWND 强抢（AttachThreadInput +
        SetForegroundWindow，复用全局热键恢复的同一套实现）。延迟是为了等
        Windows 的焦点交接稳定——suspend/窗口搬迁刚完成时系统可能还在把焦点
        交给新前台窗口，抢太早会被前台锁拒绝。

        边界：窗口已最小化/隐藏（用户并不在 HUD 上）时不打扰；任何异常都被
        吞掉，绝不干扰冻结结果的收尾（best-effort，与热键恢复同级防护）。
        """
        try:
            if self.isMinimized() or not self.isVisible():
                return
            self.raise_()
            self.activateWindow()
            QTimer.singleShot(100, self._steal_foreground_best_effort)
            self._keep_perfmon_on_top()
        except Exception:  # noqa: BLE001
            pass

    def _hotkey_err(self, where: str, exc: Exception) -> None:
        """Best-effort forensic log for the hotkey-restore path.

        The hotkey callback runs from the Windows message pump (nativeEvent),
        where a stray exception would otherwise vanish silently in the frozen
        build.  Append to ``logs/hotkey_error.log`` so the next 'flash crash'
        leaves a trail even without a console.
        """
        try:
            from pathlib import Path as _P
            root = _P(__file__).resolve().parents[3]
            logdir = root / "logs"
            logdir.mkdir(parents=True, exist_ok=True)
            with open(logdir / "hotkey_error.log", "a", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S")
                         + " [" + where + "] " + repr(exc) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def _hotkey_bring_to_front(self) -> None:
        """Global hotkey: always show + focus the window, even if minimized or
        merely behind another window ('立刻出现并且焦点').

        Qt's ``activateWindow()`` only flashes the taskbar button when another
        window owns the foreground (Windows foreground-lock), so we
        additionally call ``SetForegroundWindow`` directly — but that
        raw-HWND dance is the suspected source of the *occasional* native
        crash when restoring under a fullscreen foreground-locked game.
        Therefore:
          * the Qt-native show/restore is done synchronously (the same stable
            path as the tray restore, which never crashed);
          * the raw ``winId()`` + ``SetForegroundWindow`` + ``AttachThreadInput``
            is deferred ~80 ms via ``QTimer`` so the native handle is stable,
            and is wrapped so any failure degrades to ``activateWindow()``
            instead of taking the process down.
        """
        try:
            # 唤出 HUD 前先记住当前前台窗口（通常是游戏），供之后 Start 一键回游戏。
            self._remember_prev_foreground()
            if self.isMinimized() or not self.isVisible():
                # 与托盘恢复共用同一个 compositor-only 淡入路径（0 几何成本），
                # 避免重复实现两套透明缓动。
                self._fade_in_from_tray()
            else:
                self.raise_()
        except Exception as exc:  # noqa: BLE001
            self._hotkey_err("restore", exc)
        # Defer the risky raw-HWND foreground steal until after the show/restore
        # transition has fully settled (avoids racing Qt's internal handle
        # recreation — the prime suspect for the 0xC0000005 flash crash).
        try:
            QTimer.singleShot(80, self._steal_foreground_best_effort)
        except Exception as exc:  # noqa: BLE001
            self._hotkey_err("schedule_steal", exc)
        try:
            self._keep_perfmon_on_top()
        except Exception as exc:  # noqa: BLE001
            self._hotkey_err("perfmon", exc)

    def _steal_foreground_best_effort(self) -> None:
        """Best-effort foreground steal.  NEVER raises into the message pump."""
        try:
            from PyQt6.sip import isdeleted
            if isdeleted(self):
                return
        except Exception:  # noqa: BLE001
            pass
        if not self.isVisible():
            return
        try:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            fg = user32.GetForegroundWindow()
            fg_thread = user32.GetWindowThreadProcessId(fg, None)
            cur_thread = user32.GetCurrentThreadId()
            attached = False
            if fg_thread and fg_thread != cur_thread:
                attached = bool(user32.AttachThreadInput(fg_thread, cur_thread, True))
            user32.SetForegroundWindow(hwnd)
            if attached:
                user32.AttachThreadInput(fg_thread, cur_thread, False)
        except Exception:  # noqa: BLE001
            # Best-effort only: fall back to the Qt path, never crash.
            try:
                self.activateWindow()
            except Exception:  # noqa: BLE001
                pass

    def _register_global_hotkey(self, token: Optional[str] = None) -> None:
        """Register the customizable 'bring to front' global hotkey with the OS.

        ``token`` is a key token (default ``Win+Ctrl+F8`` when ``None`` or
        unparseable).  Any previously-registered hotkey is unregistered first,
        so editing the binding live re-registers the new combo.
        """
        self._unregister_global_hotkey()
        if getattr(self, "_offscreen", False) or sys.platform != "win32":
            return
        parsed = hotkey_from_token(token) if token else None
        mods, vk = parsed if parsed else (_MOD_WIN | _MOD_CONTROL, _VK_F8)
        try:
            hwnd = int(self.winId())
            if hwnd and ctypes.windll.user32.RegisterHotKey(
                    hwnd, _HOTKEY_ID, mods, vk):
                self._hotkey_id = _HOTKEY_ID
            else:
                # 注册失败：多半是组合键被别的程序抢注，或 hwnd 失效。
                # 之前这里静默吞掉，导致"完全没反应"却无从排查。
                err = ctypes.windll.kernel32.GetLastError()
                print(f"[hotkey] RegisterHotKey 失败: mods={mods:#x} vk={vk:#x} "
                      f"err={err}")
                self._hotkey_id = None
        except Exception as ex:  # noqa: BLE001
            print(f"[hotkey] RegisterHotKey 异常: {ex!r}")
            self._hotkey_id = None

    def _reregister_global_hotkey(self, token: Optional[str] = None) -> None:
        """Re-register the global hotkey (called after the binding changes)."""
        self._register_global_hotkey(token)

    def _unregister_global_hotkey(self) -> None:
        if getattr(self, "_hotkey_id", None) is None:
            return
        try:
            hwnd = int(self.winId())
            ctypes.windll.user32.UnregisterHotKey(hwnd, self._hotkey_id)
        except Exception:
            pass
        self._hotkey_id = None

    def _ensure_gamepad(self) -> None:
        """Keep the gamepad poll alive while the window is visible (even when
        another window is on top, so background shortcuts / Start still work)
        and stop it only when the window is minimized / hidden."""
        ctrl = getattr(self, "_controller", None)
        if ctrl is None:
            return
        visible = self.isVisible() and not (
            self.windowState() & Qt.WindowState.WindowMinimized)
        if visible:
            ctrl.start_gamepad()
        else:
            ctrl.stop_gamepad()

    def _run_inapp_action(self, command: str) -> None:
        """Dispatch an in-app shortcut command to a concrete behaviour."""
        cmd = (command or "").strip().lower()
        if cmd == "next_page":
            self._app.switch_page(1)
        elif cmd == "prev_page":
            self._app.switch_page(-1)
        elif cmd == "focus":
            self._focus_from_start()
        elif cmd == "open_layout_editor":
            self._open_editor()
        elif cmd == "open_shortcut_editor":
            self._open_shortcut_editor()
        elif cmd == "minimize":
            self._minimize_to_tray()

    def _open_shortcut_editor(self) -> None:
        """Open the standalone shortcut-button editor (lazily built)."""
        if getattr(self, "_shortcut_editor", None) is None:
            from .shortcut_editor import ShortcutEditor

            self._shortcut_editor = ShortcutEditor(
                self._app, self._shortcut_store, self._shortcut_engine,
                self._controller, self._on_shortcut_change, parent=self)
        self._shortcut_editor.show()
        self._shortcut_editor.raise_()
        self._shortcut_editor.activateWindow()

    # -- shortcut editor callbacks -------------------------------------
    def _on_shortcut_change(self) -> None:
        """Called on every edit in the shortcut editor: persist + re-sync the
        running engine / global hotkey with the (shared) store."""
        try:
            self._shortcut_store.save()
        except Exception:
            pass
        self._sync_shortcut_system()

    def _sync_shortcut_system(self) -> None:
        """Push the latest system bindings to the interaction controller and
        re-register the global 'bring to front' hotkey from its keyboard token.
        """
        ctrl = getattr(self, "_controller", None)
        store = getattr(self, "_shortcut_store", None)
        if ctrl is not None and store is not None:
            ctrl.set_system_bindings(store.system)
        if store is not None:
            kb = (store.system.get("bring_to_front") or {}).get("keyboard")
            self._reregister_global_hotkey(kb)

    # -- gamepad request routing --------------------------------------
    def _gp_save(self) -> None:
        mode = self._edit_overlay.mode()
        if mode == "tile":
            self._edit_overlay._tile_panel._save()
        elif mode == "procmon":
            self._edit_overlay._procmon_panel._save()
        elif mode == "layout":
            self._edit_overlay._layout_panel._save()
        # "button" mode: the ButtonEditPanel persists on its own action buttons
        # (commit / apply-and-refresh), so Y is a no-op here.

    def _gp_cancel(self) -> None:
        mode = self._edit_overlay.mode()
        if mode == "layout":
            # Already on the layout editor: Esc exits the whole editor back to
            # the HUD (matches the "关闭" button / F2 toggle behaviour).
            self._close_editor()
        else:
            # Any card edit-item sub-page (tile / procmon / freeze / cardcolor /
            # breathing / button): Esc returns to the layout editor, NOT the
            # main HUD — consistent with each panel's "← 返回" button.
            self._edit_overlay.open_layout()

    def _gp_delete(self) -> None:
        mode = self._edit_overlay.mode()
        if mode == "tile":
            self._edit_overlay._tile_panel._del_tile()
        elif mode == "procmon":
            self._edit_overlay._procmon_panel._del()

    # -- keyboard -----------------------------------------------------
    def keyPressEvent(self, ev: Any) -> None:
        # Only react to keyboard input when this window is the foreground /
        # active window.  When minimized, hidden to tray, or another app has
        # focus, ignore everything so the HUD never steals another window's
        # keystrokes.  (keyPressEvent is already delivered only to the focused
        # window, but this guard makes the "foreground only" rule explicit and
        # robust against any future global-shortcut wiring.)
        if not self.isActiveWindow():
            super().keyPressEvent(ev)
            return
        # F1 / H 打开当前选中卡片的疑惑解答（解耦弹窗）。
        if ev.key() in (Qt.Key.Key_F1, Qt.Key.Key_H):
            if getattr(self, "_faq", None) is not None:
                self._faq.toggle_for_current()
            ev.accept()
            return
        # Esc 关闭已打开的疑惑解答弹窗（弹窗不抢焦点，Esc 落到主窗口这里处理）。
        if ev.key() == Qt.Key.Key_Escape and getattr(self, "_faq", None) is not None:
            if self._faq.is_open():
                self._faq.close()
                ev.accept()
                return
        # F2 toggles the edit overlay (independent of the nav model).
        if ev.key() == Qt.Key.Key_F2:
            self._open_editor()
            ev.accept()
            return
        # Del minimizes the window (to tray / taskbar) — a global hotkey that
        # works in both the HUD and the layout editor.  When a text input
        # (QLineEdit) has focus it consumes Delete for character deletion, so
        # typing in an editor field is unaffected.
        if ev.key() == Qt.Key.Key_Delete:
            # Del minimizes to the tray ONLY when no text input is focused, so
            # pressing Delete while typing in an editor field deletes a char
            # instead of vanishing the window.  Honours the layout.json
            # Input.DelMinimizeWhenNoTextFocus flag.
            fw = self.focusWidget()
            if isinstance(fw, (QLineEdit, QTextEdit, QPlainTextEdit)):
                super().keyPressEvent(ev)  # let the field handle Delete
                return
            allow = True
            cfg = getattr(self._app, "config_manager", None)
            if cfg is not None:
                ic = getattr(cfg.config, "input_config", None)
                if ic is not None:
                    allow = bool(ic.del_minimize_when_no_text_focus)
            if allow:
                self._minimize_to_tray()
                ev.accept()
            else:
                super().keyPressEvent(ev)
            return
        # Esc is a dedicated toggle for the layout editor:
        #   - in the HUD            -> open the page-layout editor (mirrors F2),
        #   - in the editor (layout page) -> close back to the HUD,
        #   - in an editor sub-page -> return to the layout page.
        # Previously Esc only CLOSED the editor / cleared focus, so it could
        # never ENTER the editor from the HUD — restored here so "press Esc to
        # enter the page-layout editor" works again.
        if ev.key() == Qt.Key.Key_Escape:
            if self._controller._edit_mode:
                self._gp_cancel()
                ev.accept()
                return
            fw = self.focusWidget()
            if isinstance(fw, (QLineEdit, QTextEdit, QPlainTextEdit)):
                super().keyPressEvent(ev)  # let a focused text field consume Esc
                return
            self._open_editor()
            ev.accept()
            return
        # Keyboard shortcut triggers: record the press for long/short detection
        # (the action fires on release in keyReleaseEvent).  We still pass the
        # key through to the nav model below, so a bound key ALSO does its usual
        # HUD thing — pick non-conflicting keys in the editor if you want it
        # exclusive.  Auto-repeat presses are ignored (one logical press only).
        token = key_token_from_event(ev)
        eng = getattr(self, "_shortcut_engine", None)
        if token and eng is not None and not ev.isAutoRepeat():
            if eng.is_keyboard_trigger(token):
                eng.handle_key_press(token)
        # System keyboard bindings (foreground-only, matching the gamepad
        # shell actions): open the layout editor / focus this window when the
        # user's custom keyboard combo is pressed.  (bring_to_front stays an
        # OS-global hotkey and is handled in winEvent.)
        store = getattr(self, "_shortcut_store", None)
        if token and store is not None and not ev.isAutoRepeat():
            sysd = store.system
            if (sysd.get("open_layout_editor") or {}).get("keyboard") == token:
                self._open_editor()
                ev.accept()
                return
            if (sysd.get("focus_window") or {}).get("keyboard") == token:
                self._focus_from_start()
                ev.accept()
                return
        # Freeze-block digit hotkeys (global, foreground-only): 0/9/8/7/6 freeze
        # the matching candidate slot, 1-5 unfreeze the matching frozen slot —
        # mirroring CAND_KEYS / FROZ_KEYS in FreezeBlock.  Two-press for safety
        # (first press highlights + "再次点击" toast, second press confirms), so
        # an accidental key tap never suspends a live process.  ev.text() is
        # used so the main row AND the numpad (NumLock on) both work; text
        # inputs keep their digits for typing.
        txt = ev.text()
        if (txt and len(txt) == 1 and "0" <= txt <= "9"
                and not self._controller._edit_mode):
            fw = self.focusWidget()
            if not isinstance(fw, (QLineEdit, QTextEdit, QPlainTextEdit)):
                fb = getattr(self._app, "blocks", {}).get("freeze")
                if fb is not None and hasattr(fb, "handle_hotkey"):
                    fb.handle_hotkey(int(txt))
                    try:
                        self._bridge.sync_all()
                    except Exception:
                        pass
                    ev.accept()
                    return
        # Everything else flows through the single unified interaction
        # controller — one navigation/activation logic for all controls
        # (keyboard and gamepad share it).
        try:
            handled = self._controller.handle_key(ev)
        except Exception:
            # An exception escaping a reimplemented Qt virtual (keyPressEvent)
            # is fatal to the whole process under PyQt6.  The navigation logic
            # is already defensively guarded internally, but this is the final
            # backstop so a single unexpected error can never freeze / crash
            # the HUD on a keypress.
            handled = False
        if handled:
            ev.accept()
            return
        super().keyPressEvent(ev)

    def keyReleaseEvent(self, ev: Any) -> None:
        # Fire keyboard shortcut actions on release (long vs short resolved by
        # the engine).  Only while we're the foreground window; auto-repeat
        # releases are ignored.
        if not self.isActiveWindow():
            super().keyReleaseEvent(ev)
            return
        token = key_token_from_event(ev)
        eng = getattr(self, "_shortcut_engine", None)
        if token and eng is not None and not ev.isAutoRepeat():
            if eng.is_keyboard_trigger(token):
                eng.handle_key_release(token)
                ev.accept()
                return
        super().keyReleaseEvent(ev)

    # -- suspend / resume background activity on focus / visibility / lock --
    def _pause_aux(self) -> None:
        """Stop periodic data-refresh timers.

        Deliberately does NOT touch the breathing light (unfocused-but-visible
        keeps it dimmed-alive) and does NOT stop the gamepad: the shell keeps a
        lightweight gamepad listener alive in the background so Start (focus)
        and any user shortcut buttons still fire while a game is in front.
        Only :meth:`_suspend_activity` (minimized / hidden / locked) stops the
        gamepad — see :meth:`_ensure_gamepad`.
        """
        for t in (getattr(self, "_fast", None),
                  getattr(self, "_slow", None),
                  getattr(self, "_batt_timer", None)):
            if t is not None:
                t.stop()

    def _pause_heavy_aux(self) -> None:
        """Partial pause used when the window is *unfocused but still visible*
        (e.g. a game is in front and the HUD overlay is still on screen).

        Stops only the heavier periodic refresh timers — ``_slow`` (the block
        task-collection worker) and ``_batt_timer`` (battery poll) — but KEEPS
        the lightweight 1 s display-refresh timer ``_fast`` running, so the
        perfmon card and other read-only cards keep showing live data (the
        underlying ``MetricSampler`` samples continuously regardless of focus).
        Full suspension, including ``_fast``, still happens on minimize / hide
        via :meth:`_suspend_activity`.
        """
        for t in (getattr(self, "_slow", None),
                  getattr(self, "_batt_timer", None)):
            if t is not None:
                t.stop()

    def _ensure_fast(self) -> None:
        """Ensure the 1 s display-refresh timer (``_fast``) is running.

        Called whenever the window becomes visible — whether focused or
        unfocused-but-visible — so the perfmon / read-only cards keep
        updating.  It is only stopped by :meth:`_suspend_activity` on
        minimize / hide.  Idempotent: a no-op if already active.
        """
        t = getattr(self, "_fast", None)
        if t is not None and not t.isActive():
            t.start(1000)

    def _resume_aux(self) -> None:
        # Only resume when the window is actually visible AND active.
        if not (self.isVisible() and self.isActiveWindow()):
            return
        for t, interval in ((getattr(self, "_fast", None), 1000),
                            (getattr(self, "_slow", None), 2000),
                            (getattr(self, "_batt_timer", None), 2000)):
            if t is not None:
                t.start(interval)
        # Resume gamepad listening (only reached when visible AND active, i.e.
        # we are the foreground window — so controller input is welcome again).
        ctrl = getattr(self, "_controller", None)
        if ctrl is not None:
            ctrl.start_gamepad()

    def _suspend_activity(self, keep_gamepad: bool = True) -> None:
        """Fully pause everything (breathing + data + gamepad) while the window
        is minimized, hidden to tray, or the session is locked — i.e. when it
        is not visible at all, so no point repainting the neon frame.

        ``keep_gamepad`` 默认 True：窗口隐藏到托盘/最小化时保留后台手柄轮询，
        使隐藏态下按手柄 Start 仍能一键唤出 HUD（否则隐藏态 Start 单向失效，
        只能依赖全局热键）。锁屏（WTS_SESSION_LOCK）路径显式传 False——锁屏
        界面不需要也不应响应手柄。
        """
        if getattr(self, "_breath", None) is not None:
            self._breath.pause()
        self._pause_aux()
        # The window is not visible, so the lightweight background gamepad
        # listener normally stops too — UNLESS the caller asked to keep it alive
        # (hidden to tray, where a Start press should still re-summon the HUD).
        ctrl = getattr(self, "_controller", None)
        if ctrl is not None and not keep_gamepad:
            ctrl.stop_gamepad()

    def _resume_activity(self) -> None:
        # Only resume when the window is actually visible AND active; otherwise
        # leave it suspended (minimized, hidden to tray, or another app focused).
        if not (self.isVisible() and self.isActiveWindow()):
            return
        if getattr(self, "_breath", None) is not None:
            self._breath.resume()
        self._resume_aux()
        # Regaining focus always restores full glow brightness + clears the
        # unfocus scrim so the whole page returns to full brightness.
        self._scrim_target = 0.0
        self._start_dim(1.0)

    # -- suspend / resume (hibernation) -----------------------------------
    # PyQt6 exposes no suspend/resume signal.  On Windows the top-level window
    # receives WM_POWERBROADCAST (handled in nativeEvent); we re-initialize the
    # gamepad subsystem on resume so a controller that was opened before the
    # sleep keeps working (see SDLGamepadBackend.reinit for why the handles die).
    def _on_power_suspend(self) -> None:
        """System is about to suspend/hibernate: stop the 30 ms gamepad poll so
        we don't keep reading stale SDL handles while the device stack is torn
        down.  Breathing / aux timers are left to their existing suspend paths
        (session lock / minimize) — we only manage the gamepad here."""
        ctrl = getattr(self, "_controller", None)
        if ctrl is not None:
            ctrl.stop_gamepad()

    def _on_power_resume(self) -> None:
        """System resumed from suspend/hibernate.  The gamepad's SDL handles now
        reference the pre-sleep device context and must be rebuilt.  Device
        enumeration can lag after resume, so we kick the rebuild immediately and
        retry a few times with increasing delays until the backend reports the
        subsystem is back up.  The poll loop is also restarted unconditionally;
        if the reinit ever fails, SDL's own device-change detection still
        self-heals stale handles (see SDLGamepadBackend._refresh)."""
        ctrl = getattr(self, "_controller", None)
        if ctrl is None:
            return
        delays = [150, 700, 1600, 3500]  # ms; first kick is near-immediate

        def attempt(remaining: list[int]) -> None:
            ok = False
            gp = getattr(getattr(ctrl, "_gamepad", None), "reinit", None)
            if callable(gp):
                try:
                    ok = bool(gp())
                except Exception:
                    ok = False
            # The poll loop must be running regardless of reinit outcome.
            try:
                ctrl.start_gamepad()
            except Exception:
                pass
            if not ok and remaining:
                QTimer.singleShot(remaining[0], lambda: attempt(remaining[1:]))

        QTimer.singleShot(delays[0], lambda: attempt(delays[1:]))
        # If the window is actually visible + active, also resume the breathing
        # light / aux timers that a prior suspend may have paused.  If the
        # session is still locked or the window hidden, leave them suspended.
        if self.isVisible() and self.isActiveWindow():
            self._resume_activity()
        # 硬件配置重下发：SMU/EC/RTSS 在休眠后会被重置，休眠前已应用的电源/
        # 风扇/锁帧/SMT 配置需要重新下发。延迟 800ms 让硬件栈就绪（且错开手柄
        # 首个 150ms kick，避免 WinRing0 互斥争用）。
        QTimer.singleShot(800, self._reapply_after_resume)

    def _reapply_after_resume(self) -> None:
        """休眠恢复后重新下发所有「休眠前已应用」的硬件配置。

        一次性下发的配置（power TDP/GPU 锁、fan MANUAL/CURVE/SILENT、rtss 锁帧、
        smt 强制锁定）在系统休眠后会被 SMU/EC/驱动重置；本方法把它们重新下发。
        每个区块自带门控（applied/lock_applied/_last.mode），未应用过的不会
        被擅自下发。降压状态机是持续运行的，但有个隐藏 bug：它只在 state 变化
        时写 ryzenadj，休眠后 self.state 仍是旧值而 SMU 已重置，下一 tick 会因
        new==state 跳过写入——所以这里把 state reset 为哨兵，强制下一 tick 重写。
        """
        app_ref = getattr(self, "_app", None)
        if app_ref is None:
            return
        reapplied: list[str] = []
        # 1) 降压状态机 reset：下一 tick 必然重新写入当前档位。
        try:
            ctrl = getattr(self, "_uv_controller", None)
            if ctrl is not None and getattr(ctrl.config, "enabled", False) \
                    and not getattr(ctrl.config, "dry_run", False):
                ctrl.state = ""
                reapplied.append("降压")
        except Exception:
            pass
        # 2) MetricSampler 健康检查：休眠后 PowerShell 子进程可能失效。
        try:
            sampler = getattr(self, "_uv_sampler", None)
            if sampler is not None and not sampler.is_running:
                sampler.start()
        except Exception:
            pass
        # 3) 广播 OnPowerResume：各 block 按自身门控决定是否重下发。
        try:
            results = app_ref.fire_hook("OnPowerResume")
            label_map = {"power": "电源", "fan": "风扇", "rtss": "锁帧", "smt": "SMT"}
            for btype, res in results:
                if res is None:
                    continue  # no-op（休眠前未应用过）
                reapplied.append(label_map.get(btype, btype))
        except Exception:
            pass
        # 4) 汇总反馈：只弹一条 toast，避免 N 个 block 各弹一次吵到用户。
        if reapplied:
            msg = "已从休眠恢复，重新应用：" + "、".join(reapplied)
            print(f"[power-resume] {msg}")
            try:
                power = app_ref.blocks.get("power")
                if power is not None:
                    power.show_toast("✓ " + msg, success=True)
            except Exception:
                pass

    def changeEvent(self, ev: Any) -> None:  # noqa: N802 - Qt camelCase
        if ev.type() == QEvent.Type.ActivationChange:
            # 手柄隔离：凝冰结魄前台（含自己的 popup）→ 拦截游戏手柄；失焦 → 放行
            self._sync_xinput_gate(self.isActiveWindow() or self._is_own_popup_active())
            if self.isActiveWindow():
                self._resume_activity()
                self._keep_perfmon_on_top()
            else:
                # Unfocused.  If the window is still *visible* (another app is
                # just on top), keep the breathing light alive but ease its glow
                # to the dim floor, idle the data polling — and KEEP the gamepad
                # listener alive (background shortcuts / Start still work).  If
                # the window is minimized / hidden, fully suspend instead.
                if self._is_own_popup_active():
                    # One of our own docked helper popups (FAQ / button-list)
                    # took focus: keep the HUD fully lit + breathing — it is
                    # still the user's primary surface, just with a helper
                    # docked beside it, so do NOT dim like a real alt-tab away.
                    self._scrim_target = 0.0
                    self._start_dim(1.0)
                    if getattr(self, "_breath", None) is not None:
                        self._breath.resume()
                    self._ensure_fast()
                    self._ensure_gamepad()
                elif self.isVisible() and not (self.windowState() &
                                               Qt.WindowState.WindowMinimized):
                    self._scrim_target = self._SCRIM_UNFOCUSED
                    self._start_dim(self._DIM_UNFOCUSED)
                    # Unfocused-but-visible: idle the heavier refresh timers but
                    # keep the 1 s display refresh alive so the perfmon / read-
                    # only cards stay live while a game is in front.
                    self._ensure_fast()
                    self._pause_heavy_aux()
                    self._ensure_gamepad()  # background gamepad still listens
                else:
                    self._suspend_activity()
        elif ev.type() == QEvent.Type.WindowStateChange:
            if self.windowState() & Qt.WindowState.WindowMinimized:
                self._suspend_activity()
            else:
                # De-minimized.  If we're now the active window the resume path
                # handles it; otherwise re-ensure the background gamepad listener
                # (we may have been de-minimized while another app stayed front)
                # and restart the 1 s display refresh so the perfmon card keeps
                # updating while the game stays in front.
                if not self.isActiveWindow():
                    self._ensure_fast()
                    self._ensure_gamepad()
        super().changeEvent(ev)

    def _sync_xinput_gate(self, foreground: bool) -> None:
        """手柄隔离：凝冰结魄前台/失焦时通知 XInputGate。

        启用时：
        - DLL 劫持模式（单机游戏）：set/reset 命名事件，fake DLL 据此拦截
          XInputGetState。无副作用如果没有游戏部署 fake DLL。
        - 设备禁用模式（反作弊游戏）：前台禁用物理手柄，失焦启用。
          只在配置中有 device_disable 模式游戏时才操作。
        未启用时：确保设备恢复 + 事件非信号态（安全降级）。
        """
        gate = self._ensure_xinput_gate()
        if gate is None:
            return
        cfg = getattr(getattr(self, "_app", None), "config", None)
        enabled = bool(cfg and getattr(cfg, "xinput_gate_enabled", False))
        if not enabled:
            # 未启用 → 失焦时确保设备恢复（万一本会话之前禁用过）
            if not foreground:
                gate.set_active(False, "dll_hijack")
                gate.set_active(False, "device_disable")
            return
        # 启用：DLL 劫持模式总是 set/reset 事件
        gate.set_active(foreground, "dll_hijack")
        # 设备禁用模式：只在配置中有 device_disable 游戏时才操作
        has_dd = any(
            g.get("mode") == "device_disable"
            for g in getattr(cfg, "xinput_gate_games", [])
        )
        if has_dd:
            gate.set_active(foreground, "device_disable")
        elif not foreground:
            # 没有设备禁用游戏但之前可能禁用过 → 确保恢复
            gate.set_active(False, "device_disable")

    def _ensure_xinput_gate(self):
        """惰性创建 XInputGate 实例（首次调用时创建，之后复用）。"""
        gate = getattr(self, "_xinput_gate", None)
        if gate is None:
            try:
                from ..platform.xinput_gate import XInputGate
                gate = XInputGate()
                self._xinput_gate = gate
            except Exception:
                self._xinput_gate = None
                return None
        return gate

    def hideEvent(self, ev: Any) -> None:  # noqa: N802 - Qt camelCase
        # Hitting the − button hides to the tray (self.hide()): no point running
        # the 30ms repaint loop for an invisible window.
        self._suspend_activity()
        super().hideEvent(ev)

    def showEvent(self, ev: Any) -> None:  # noqa: N802 - Qt camelCase
        # Resume only if we're the active window; otherwise keep the background
        # gamepad listener alive (another app may still be in front).
        if self.isActiveWindow():
            self._resume_activity()
        else:
            # Shown while another app stays in front (e.g. restored from tray):
            # keep the gamepad listener alive AND restart the 1 s display refresh
            # so the perfmon card updates live while the window is merely unfocused.
            self._ensure_fast()
            self._ensure_gamepad()
        super().showEvent(ev)
        # The screen (and therefore the correct available geometry) is only
        # guaranteed known once we're actually shown.  Re-fit here so the window
        # hugs the right edge / fills the screen height correctly even when the
        # display wasn't resolvable at __init__ time (e.g. multi-monitor setups).
        self._fit_to_screen()
        # Re-check once after the event loop settles: on some scaled / multi-mon
        # setups the first showEvent still sees stale geometry.  A short single-
        # shot timer catches the correct values without blocking show.
        QTimer.singleShot(150, self._fit_to_screen)
        # QWindow 句柄此刻已就绪，补连 devicePixelRatioChanged（同屏缩放变化）。
        self._wire_dpr_change()
        # 一次性管理员权限警告：ryzenadj（GPU 锁频 / 浮动 / CPU 降压）需要
        # 管理员权限加载 ring0 驱动；否则调用会静默失败，用户只看到“没效果”。
        if not getattr(self, "_admin_warned", False):
            self._admin_warned = True
            try:
                import ctypes

                if not ctypes.windll.shell32.IsUserAnAdmin():
                    from PyQt6.QtWidgets import QMessageBox

                    QMessageBox.warning(
                        self, "需要管理员权限",
                        "GPU 锁频 / 浮动 / CPU 降压 依赖 ryzenadj，需要管理员权限才能"
                        "加载硬件驱动。\n当前以普通权限运行，这些功能会静默失效。\n\n"
                        "请关闭本程序，右键『以管理员身份运行』重新启动。",
                    )
            except Exception:
                pass
        # Geometry is final now — (re)build the pre-blurred tray glow layer.
        if not getattr(self, "_offscreen", False):
            self._rebuild_tray_glow()
        # 任何一次显示（首启 / 托盘恢复 / 热键唤出）后，若性能监控弹窗仍打开，
        # 重新把它抬到主窗口之上（它是「允许在上」的唯一例外）。
        self._keep_perfmon_on_top()

    def nativeEvent(self, eventType, message):  # noqa: N802 - Qt camelCase
        # PyQt6 removed QWidget.winEvent(); the native message hook is now
        # nativeEvent(eventType, message) and it returns (handled, result) —
        # note the argument/return order is the REVERSE of the old winEvent,
        # which returned (result, handled).  The previous winEvent override was
        # dead code in PyQt6, so WM_HOTKEY (the global "bring to front" hotkey)
        # was silently never received — that is why the hotkey did nothing.
        if eventType != b"windows_generic_MSG":
            return False, 0
        try:
            msg = ctypes.cast(int(message), ctypes.POINTER(ctypes.wintypes.MSG)).contents
        except Exception:
            return False, 0
        if _WTS_AVAILABLE and msg.message == _WM_WTSSESSION_CHANGE:
            if msg.wParam == _WTS_SESSION_LOCK:
                # 锁屏：连后台手柄轮询也停掉，锁屏界面不应响应 Start。
                self._suspend_activity(keep_gamepad=False)
            elif msg.wParam == _WTS_SESSION_UNLOCK:
                self._resume_activity()
            return True, 0
        if getattr(self, "_hotkey_id", None) is not None \
                and msg.message == _WM_HOTKEY \
                and msg.wParam == self._hotkey_id:
            try:
                self._hotkey_bring_to_front()
            except Exception as exc:  # noqa: BLE001
                try:
                    self._hotkey_err("nativeEvent_dispatch", exc)
                except Exception:  # noqa: BLE001
                    pass
            return True, 0
        if msg.message == _WM_POWERBROADCAST:
            wp = msg.wParam
            if wp == _PBT_APMSUSPEND:
                self._on_power_suspend()
            elif wp in (_PBT_APMRESUMEAUTOMATIC, _PBT_APMRESUMESUSPEND):
                self._on_power_resume()
            return True, 0
        return False, 0

    def _register_session_lock_notifier(self) -> None:
        if not _WTS_AVAILABLE or getattr(self, "_offscreen", False):
            return
        try:
            hwnd = int(self.winId())
            if hwnd and _wtsapi.WTSRegisterSessionNotification(hwnd, 0):
                self._wts_hwnd = hwnd
        except Exception:
            self._wts_hwnd = None

    def _unregister_session_lock_notifier(self) -> None:
        if not _WTS_AVAILABLE or getattr(self, "_wts_hwnd", None) is None:
            return
        try:
            _wtsapi.WTSUnRegisterSessionNotification(self._wts_hwnd)
        except Exception:
            pass
        self._wts_hwnd = None

    # -- clean shutdown ------------------------------------------------
    def closeEvent(self, ev: Any) -> None:
        self._unregister_session_lock_notifier()
        self._unregister_global_hotkey()
        if getattr(self, "_breath", None) is not None:
            self._breath.stop()
        self._slow.stop()
        self._fast.stop()
        if getattr(self, "_uv_timer", None) is not None:
            self._uv_timer.stop()
        if getattr(self, "_batt_timer", None) is not None:
            self._batt_timer.stop()
        # 退出前把内存中尚未落盘的配置统一写回 layout.json。
        # 焦点蒙层（focus_color / focus_opacity）等字段仅靠 ButtonEditPanel
        # 的 hideEvent 触发保存，而 hideEvent 在「直接退出程序（未先点返回）」
        # 时不会触发——此时只有顶层 closeEvent 执行，导致这些改动被丢弃，
        # 下次启动又恢复默认值。这里兜底 flush 一次，覆盖所有优雅退出路径。
        try:
            app_ref = getattr(self, "_app", None)
            if app_ref is not None and hasattr(app_ref, "save_all"):
                app_ref.save_all()
        except Exception:  # noqa: BLE001 - 退出阶段不应因保存失败而阻断收尾
            pass
        if getattr(self, "_controller", None) is not None:
            self._controller.stop_gamepad()
        # 手柄隔离：恢复物理手柄 + 关闭命名事件
        _xgate = getattr(self, "_xinput_gate", None)
        if _xgate is not None:
            _xgate.close()
        # 对称回收：各区块注销挂到共享 MetricSampler 的回调（与 __init__ /
        # _connect_sampler 的 register_callback 配对）。app 退出时若不注销，
        # 若区块被重建（如配置重载）旧回调会持续堆积，持有已废弃对象引用。
        # 即便本次退出不会重建，这也让回调生命周期与区块实例严格对称。
        for _blk in list(getattr(self._app, "blocks", {}).values()):
            _dispose = getattr(_blk, "dispose", None)
            if callable(_dispose):
                try:
                    _dispose()
                except Exception:  # noqa: BLE001
                    pass
        # 终止直连 GPU 利用率监控的 PowerShell 子进程（防孤儿进程；硬崩溃场景
        # 由 _GpuUtilMonitor 内部 Job Object 兜底，见 platform/ryzenadj.py）。
        try:
            _GpuUtilMonitor.instance().stop()
        except Exception:  # noqa: BLE001
            pass
        self._shutdown_undervolt()
        # 关闭快捷按钮编辑器（独立窗口），避免退出主程序后它残留。
        ed = getattr(self, "_shortcut_editor", None)
        if ed is not None:
            try:
                ed.close()
            except Exception:
                pass
        if self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(3000)
        for w in list(getattr(self, "_freeze_workers", [])):
            try:
                if w.isRunning():
                    w.quit()
                    w.wait(1000)
            except Exception:
                pass
        # 方案 1 清理：若后台专用桌面已无冻结进程占用则销毁它，避免退出后
        # 残留空桌面；仍有冻结进程时不删（否则窗口会被 Windows 强制迁移）。
        freeze = self._app.blocks.get("freeze") if getattr(self, "_app", None) else None
        if freeze is not None and hasattr(freeze, "dispose_vdesktop"):
            try:
                freeze.dispose_vdesktop()
            except Exception:
                pass
        # Hide (don't leak) the tray icon on a real quit.
        tray = getattr(self, "_tray", None)
        if tray is not None:
            tray.hide()
        super().closeEvent(ev)


def build_overlay(app: Any) -> QWidget:
    """Build and return the overlay window (caller owns the QApplication loop)."""
    return _OverlayWindow(app)


def run_app(app: Any) -> int:
    """Convenience: own the QApplication loop and run the overlay."""
    from PyQt6.QtWidgets import QApplication

    qapp = QApplication.instance() or QApplication([])
    build_overlay(app).show()
    return qapp.exec()
