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
import math
import os
import struct
import sys
import time
import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from ..core import profile  # opt-in perf profiler (NBJ_PROFILE); no-op when disabled

from PyQt6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QRect,
    QEasingCurve,
    QThread,
    QTimer,
    Qt,
    pyqtSignal,
    pyqtSlot,
)
# NOTE: QLinearGradient 于 2026-08-29 从本模块移除——托盘顶部的呼吸扫描线
# 光带被用户要求「完全取消」后已无任何线性渐变绘制；方案 A 只用 QRadialGradient。
from PyQt6.QtGui import (QBrush, QColor, QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QRadialGradient, QRegion)
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
    QMenu,
    QPushButton,
    QSizePolicy,
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

from .button_edit import collect_button_groups
from .edit_overlay import EditOverlay
from .gamepad import create_backend
from .interaction import InteractionController
from .keyutil import key_token_from_event
from .panels import make_panel, PerfMonPanel, PerfMonPopup, _FitScrollArea
from .page_transition import (
    DEFAULT_TRANSITION_MS,
    SAFETY_MARGIN_MS,
    TRANSITION_MODES,
    PageIndicator,
    PageTransition,
    TransitionVeil,
)
from ..core.metrics import MetricSampler
from ..platform.ryzenadj import _GpuUtilMonitor
from ..platform.undervolt import (
    HEAVY, IDLE, LIGHT,
    RyzenAdjBackend, UndervoltConfig, UndervoltController,
)

# Minimum breathing-brightness change (absolute alpha) required before we
# bother repainting the window-edge glow.  Steps below this are imperceptible,
# so skipping them avoids needless DWM re-composites.  Lowered from 0.02 to
# 0.006 (≈ 1.5× an 8-bit colour step) to remove the visible freeze at the sine
# apex/trough while still coalescing flat dwells.
_BREATH_SKIP_DA = 0.006
# Maximum time (seconds) between breath frames, even when the alpha delta is
# below _BREATH_SKIP_DA.  Prevents the sine apex/trough from ever holding a
# frame for more than ~100 ms.
_BREATH_SKIP_MAX_S = 0.10
# 呼吸灯（方案 C，2026-08-29 修订）：静态中心底光画在主窗背景层（卡片之后，
# _paint_breath_frame），天然不抢点击、主窗不透明；边框脉动改画在独立顶层窗口
# _GlowBorderOverlay（呼吸边框，windowOpacity 驱动；位于失焦黑罩之上、与失焦暗化
# 解耦）+ _EditorDimOverlay（编辑态托盘暗化，恒定 opacity，位于卡片之上）。
# 脉动只重绘那个很小的半透明表面（DWM 合成，GPU 廉价），主窗与 6 张卡片完全不
# 参与呼吸重绘 → 性能回到 0829「零呼吸」基线，且游戏前台也清晰可见。
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
# AC/DC 电源切换（插拔电）。捕获 PBT_POWERSETTINGCHANGE 中 GUID_ACDC_POWER_SOURCE
# 这一项，可在 OS 插拔电自动切计划时（即便 HUD 隐藏）即时刷新电源计划卡片，
# 全程事件驱动、零轮询。注册由 __init__ 的 _register_power_source_notifier 完成。
_PBT_POWERSETTINGCHANGE = 0x8013
# GUID_ACDC_POWER_SOURCE = {5D3E9A59-E9D5-4B00-A6BD-FF34FF516548}，小端 16 字节。
_ACDC_GUID_BYTES = struct.pack(
    "<LHH8B", 0x5D3E9A59, 0xE9D5, 0x4B00,
    0xA6, 0xBD, 0xFF, 0x34, 0xFF, 0x51, 0x65, 0x48)

if sys.platform == "win32":
    try:
        _wtsapi = ctypes.windll.wtsapi32
        _WTS_AVAILABLE = True
    except Exception:
        _WTS_AVAILABLE = False


# -- Win32 前台窗口（焦点）助手 ------------------------------------------
# Windows「前台锁」：非前台进程调用 ``SetForegroundWindow`` 会被**静默拒绝**
# （只闪一下任务栏），锁定时长由注册表 ``HKCU\Control Panel\Desktop\
# ForegroundLockTimeout`` 决定，默认 200000 ms ≈ 200 s。托盘点开 HUD 时前台多半
# 是游戏或桌面，而 Qt 的 ``activateWindow()`` 底层正是 ``SetForegroundWindow``，
# 于是表现为「HUD 显示出来了，但键盘 / 手柄仍然归别人」——这也是全局热键唤出
# 路径早就额外补了原生抢焦点的原因（见 ``_steal_foreground_best_effort``）。
#
# 绕法：``AttachThreadInput`` 把本线程挂到前台线程上，前台锁随即把本线程当成
# 前台进程的一部分，``SetForegroundWindow`` 立即生效；**用完必须解绑**，否则两
# 个线程会长期共享输入队列，导致鼠标/键盘行为错乱。
#
# ⚠️ ctypes 默认把 Python int 压成 32 位 C int，而 64 位 Windows 的 HWND 是
# 64 位值（句柄 > 2^31 时会被截断甚至符号反转），所以必须显式声明
# argtypes/restype。这里统一声明一次，本模块所有 HWND 调用（含 Start 回游戏的
# ``ShowWindow`` / ``SetForegroundWindow``）都跟着受益。
_USER32_READY = False
_user32 = None
_kernel32 = None
if sys.platform == "win32":
    try:
        _user32 = ctypes.windll.user32
        # 返回 HWND 的接口：restype 必须是 c_void_p，否则 64 位句柄被截成 int。
        for _name in ("GetForegroundWindow", "GetFocus", "SetActiveWindow",
                      "SetFocus"):
            getattr(_user32, _name).restype = ctypes.c_void_p
        # 入参带 HWND 的接口。
        for _name in ("SetForegroundWindow", "BringWindowToTop",
                      "SetActiveWindow", "SetFocus", "IsIconic",
                      "IsWindowVisible", "IsWindow", "IsChild",
                      "GetWindowThreadProcessId", "GetClassNameW"):
            _fn = getattr(_user32, _name)
            if _name == "GetClassNameW":
                _fn.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
            elif _name == "IsChild":
                _fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            else:
                _fn.argtypes = [ctypes.c_void_p]
        _user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p,
                                                     ctypes.c_void_p]
        _user32.GetWindowThreadProcessId.restype = ctypes.c_uint
        _user32.GetClassNameW.restype = ctypes.c_int
        _user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _user32.ShowWindow.restype = ctypes.c_bool
        _user32.AttachThreadInput.argtypes = [ctypes.c_uint, ctypes.c_uint,
                                              ctypes.c_bool]
        _user32.AttachThreadInput.restype = ctypes.c_bool
        _user32.SetForegroundWindow.restype = ctypes.c_bool
        _user32.BringWindowToTop.restype = ctypes.c_bool
        _user32.IsIconic.restype = ctypes.c_bool
        _user32.IsWindowVisible.restype = ctypes.c_bool
        _user32.IsWindow.restype = ctypes.c_bool
        _user32.IsChild.restype = ctypes.c_bool
        # ⚠️ ``GetCurrentThreadId`` 是 **kernel32** 的导出，不是 user32 的！
        # 旧代码写的是 ``user32.GetCurrentThreadId()``，ctypes 取属性时直接抛
        # AttributeError —— 整个抢焦点函数被外层 except 吞掉、悄悄退化成
        # ``activateWindow()``，原生抢焦点其实**一次都没跑过**（热键唤出偶尔
        # 「抢不到焦点」的真凶）。这里单独从 kernel32 取，并显式声明 restype。
        _kernel32 = ctypes.windll.kernel32
        _kernel32.GetCurrentThreadId.restype = ctypes.c_uint
        _USER32_READY = True
    except Exception:
        _USER32_READY = False
        _user32 = None
        _kernel32 = None

# 抢焦点失败后的单次补刀延迟（ms）：托盘右键菜单刚点下时菜单仍在「active」状态，
# SetForegroundWindow 会被拒；等菜单关掉再补一次即可成功。
_STEAL_FG_RETRY_MS = 120


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


class _SwitchProfiler:
    """非侵入式切页计时器。

    仅当环境变量 ``NBJ_PROFILE=1`` 时启用，正常构建/运行零开销。
    每次 ``rebuild()`` 记录各阶段 ``perf_counter`` 时间戳，追加一行到
    ``logs/switch_profile.csv``（行缓冲、逐行 flush），用于跨设备对比
    切页延迟（warmup vs 稳态），定位卡顿主导阶段。
    """
    _COLS = ["ts", "seq", "heavy", "restyle", "reload", "n_panels",
             "total_ms", "reload_ms", "build_ms", "repaint_ms", "pagebar_ms"]

    def __init__(self) -> None:
        self.enabled = os.environ.get("NBJ_PROFILE") == "1"
        self._t: dict[str, float] = {}
        self._fh = None
        if self.enabled:
            try:
                os.makedirs("logs", exist_ok=True)
                path = os.path.join("logs", "switch_profile.csv")
                self._fh = open(path, "a", encoding="utf-8", buffering=1)
                if os.fstat(self._fh.fileno()).st_size == 0:
                    self._fh.write(",".join(self._COLS) + "\n")
            except Exception:
                self.enabled = False
                self._fh = None

    def reset(self) -> None:
        self._t.clear()

    def mark(self, name: str) -> None:
        if self.enabled:
            self._t[name] = time.perf_counter()

    def commit(self, seq: int, n: int, heavy: bool, restyle: bool,
               reload: bool) -> None:
        if not self.enabled or self._fh is None:
            return
        t = self._t
        if "start" not in t:
            return

        def _d(a: str, b: str):
            return round((t[b] - t[a]) * 1000.0, 3) if (a in t and b in t) else ""

        total = round((time.perf_counter() - t["start"]) * 1000.0, 3)
        row = [
            time.strftime("%H:%M:%S"), seq,
            int(heavy), int(restyle), int(reload), n, total,
            _d("start", "after_reload"),
            _d("after_reload", "after_build"),
            _d("before_update", "after_update"),
            _d("after_update", "after_pagebar"),
        ]
        try:
            self._fh.write(",".join(str(x) for x in row) + "\n")
        except Exception:
            pass


class _SyncBridge(QObject):
    """Lives on the UI thread; repaints panels when the worker signals."""

    def __init__(self, panels: list[Any], controller: Any,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._panels = panels
        self._controller = controller

    @pyqtSlot()
    def sync_all(self) -> None:
        # 重建期（切页）面板正在被 deleteLater + 重建，绝不在此窗口内触碰面板，
        # 否则会 deref 半销毁控件 → 原生崩溃（try/except 接不住）。重建守卫由
        # _controller._rebuilding 表示（_on_page_switched 在切页触发的同一调用栈
        # 内即置位）；跳过并让下一个 tick 重试，重建结束后守卫复位、刷新恢复正常。
        _t0 = time.monotonic()
        _ctrl = getattr(self, "_controller", None)
        if _ctrl is not None and getattr(_ctrl, "_rebuilding", False):
            profile.tick_sync_all((time.monotonic() - _t0) * 1000.0)
            return
        # 滚动活跃期（滚轮 / 方向键后 180ms 内）整窗跳过刷新：controller.refresh()
        # 会重建 freeze/procmon/ls/tile 列表行 → 卡片高度变化 → 重排 + 整视口重绘
        # + 滚动位置跳动，与滚动抢主线程（「连续顺滑卡」被缓存解决后，这成了新的
        # 可见周期顿挫）。缓存只降卡片皮肤光栅，碰不到这笔回流。复用 _mark_scrolling
        # 已设的 _scroll_freeze_until（原本只用来冻结呼吸 overlay，现一并用于刷新）。
        # 滚动停止 180ms 后下一个 tick 自然恢复，数值延迟 <0.2s，用户无感。
        if time.monotonic() < getattr(self, "_scroll_freeze_until", 0.0):
            profile.tick_sync_all((time.monotonic() - _t0) * 1000.0)
            return
        for p in self._panels:
            try:
                p.sync()
            except Exception:
                pass
        # Keep the unified focus model in sync with dynamic panel content
        # (freeze / procmon / ls / tile lists change every refresh).
        _tr = time.monotonic()
        try:
            self._controller.refresh()
        except Exception:
            pass
        profile.tick_refresh((time.monotonic() - _tr) * 1000.0)
        profile.tick_sync_all((time.monotonic() - _t0) * 1000.0)


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
        self._power_level = 0  # SDL_JOYSTICK_POWER_* tier
        self._color = QColor(91, 227, 139)  # green until battery is known
        self.setFixedSize(26, 14)
        self.setCursor(Qt.CursorShape.WhatsThisCursor)
        self.setVisible(False)

    def set_state(self, connected: bool, name: str = "", battery: int = 0) -> None:
        self._connected = connected
        self._name = name or ""
        self._power_level = battery
        self._color = self._battery_color(battery)
        self.setToolTip(self._tooltip())
        # Save space: only occupy the header when a pad is actually present.
        self.setVisible(connected)
        self.update()

    def set_battery(self, level: int) -> None:
        """Recolor the glyph to the SDL power tier (green -> red) without
        touching visibility.  No-op when the tier is unchanged, so a connected
        pad never repaints on every 33 ms poll."""
        if level == self._power_level:
            return
        self._power_level = level
        self._color = self._battery_color(level)
        self.setToolTip(self._tooltip())
        self.update()

    @staticmethod
    def _battery_color(level: int) -> QColor:
        """Map an SDL_JOYSTICK_POWER_* tier to the glyph colour.

        Green = full / wired / unknown (assume ok); ramps through amber and
        orange to red as the pad drains.  The HUD thus shows battery at a
        glance with no text (per the no-text-container decision).
        """
        ramp = {
            0: (91, 227, 139),    # unknown -> green (keep old "connected" look)
            1: (229, 72, 72),     # empty  -> red
            2: (230, 150, 70),    # low    -> orange
            3: (214, 200, 90),    # medium -> amber
            4: (91, 227, 139),    # full   -> green
            5: (91, 227, 139),    # wired  -> green
        }
        r, g, b = ramp.get(level, (91, 227, 139))
        return QColor(r, g, b)

    def _tooltip(self) -> str:
        labels = {
            0: "电量未知",
            1: "电量: 空",
            2: "电量: 低",
            3: "电量: 中",
            4: "电量: 满",
            5: "有线供电",
        }
        batt = labels.get(self._power_level, "电量未知")
        if self._connected and self._name:
            return f"手柄: 已连接 ({self._name}) · {batt}"
        if self._connected:
            return f"手柄: 已连接 · {batt}"
        return "手柄: 未检测到（可用键盘）"

    def paintEvent(self, ev: Any) -> None:  # noqa: D401 - Qt override
        if not self._connected:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._draw(p)
        p.end()  # 关键：必须显式 end()，否则高频重绘后 GC 析构 painter 会触发
                 # QBackingStore::endPaint() with active painter -> abort()

    def _draw(self, p: QPainter) -> None:
        """Draw the controller glyph onto ``p`` (injected painter so the same
        routine is unit-testable without a live widget).

        Slim symmetric silhouette (top edge narrow, two grips tucked in,
        centre valley) — matches the redesigned HTML mockup.  Fixed green =
        "connected" (semantic, not the breathing-light accent).
        """
        col = self._color  # recoloured by battery tier (green -> red)

        # The glyph is authored in a fixed 34x18 design space, then scaled to
        # the widget's real (smaller) size so it stays crisp and proportional
        # when the caller shrinks the icon (e.g. setFixedSize(26, 14)).
        dw, dh = 34.0, 18.0
        s = min(self.width() / dw, self.height() / dh)
        p.save()
        p.translate((self.width() - dw * s) / 2.0,
                    (self.height() - dh * s) / 2.0)
        p.scale(s, s)

        pen = QPen(col)
        pen.setWidthF(2.0)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)

        # --- controller body outline (slim shell + two tucked grips) -----
        body = QPainterPath()
        body.moveTo(12, 1)
        body.lineTo(22, 1)
        body.quadTo(26, 1, 27, 4)
        body.quadTo(31, 7, 32, 13)
        body.quadTo(33, 17, 29, 17)
        body.quadTo(25, 17, 23, 12)
        body.quadTo(21, 9, 17, 9)
        body.quadTo(13, 9, 11, 12)
        body.quadTo(9, 17, 5, 17)
        body.quadTo(1, 17, 2, 13)
        body.quadTo(3, 7, 7, 4)
        body.quadTo(8, 1, 12, 1)
        p.drawPath(body)

        # --- D-pad cross (left) ------------------------------------------
        p.setBrush(col)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(5.4, 5.2, 5.2, 2.2), 0.9, 0.9)
        p.drawRoundedRect(QRectF(6.9, 3.8, 2.2, 5.2), 0.9, 0.9)

        # --- twin analog sticks (centre) --------------------------------
        sp = QPen(col)
        sp.setWidthF(1.3)
        p.setPen(sp)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for sx in (13, 21):
            p.drawEllipse(QPointF(sx, 6), 1.6, 1.6)

        # --- four face buttons (right), diamond layout ------------------
        p.setBrush(col)
        p.setPen(Qt.PenStyle.NoPen)
        for dx, dy in ((2, -1.8), (4, 0), (2, 1.8), (0, 0)):
            p.drawEllipse(QPointF(24 + dx, 6 + dy), 1.3, 1.3)

        p.restore()  # undo the design-space scale/translate


class _BatteryBar(QLabel):
    """Header battery readout — mirrors the original 5-cell energy bar."""

    def __init__(self) -> None:
        super().__init__()
        self.set_none()

    def set_none(self) -> None:
        self.setText("🔌 N/A")
        self.setStyleSheet("color: #6b7689; font-size: 11px;")

    def set_status(self, pct: int, charging: bool, available: bool) -> None:
        if not available:
            self.set_none()
            return
        n = max(0, min(5, round(pct / 20)))
        bar = "▮" * n + "▯" * (5 - n)
        icon = "⚡" if charging else "🔋"
        self.setText(f"{icon} {pct}% {bar}")
        color = "#4be38a" if pct >= 60 else ("#ffd166" if pct >= 20 else "#ff6b6b")
        self.setStyleSheet(f"color: {color}; font-size: 11px;")


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
        a = max(0.0, min(1.0, float(a)))
        visible = a > 0.002
        if self._alpha == a and self.isVisible() == visible:
            return
        self._alpha = a
        self.setVisible(visible)
        if visible:
            self.update()

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


class _GlowBorderOverlay(QWidget):
    """Breathing-border overlay (independent top-level window owned by the main
    window, transparent for mouse events).

    The breathing *border pulse* lives HERE, raised ABOVE the unfocus ``_scrim``
    and decoupled from ``_glow_dim`` so it keeps breathing — and stays visible —
    even while a game is in front (the HUD's real use case).

    YeManCC-style optimisation (2026-08-29, "彻底对齐"): the border is painted
    ONCE at full brightness (see ``_paint_breath_border``) and the *breathing*
    is driven by ``setWindowOpacity`` from the main thread — the DWM composites
    the alpha, so the main GUI thread does NO per-frame QPainter work and the 6
    cards never repaint for breathing.  The editor-dim layer is a SEPARATE
    window (``_EditorDimOverlay``) because its opacity must stay constant (it is
    not modulated by the breath)."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._parent = parent
        # 由主窗拥有的独立顶层窗口：位于主窗之上、失焦黑罩(_scrim)之上。呼吸边框
        # 必须浮在外部游戏窗口之上（HUD 真实用例），且呼吸动画由 setWindowOpacity
        # 驱动（DWM 合成）——对子控件 setWindowOpacity 无效，故绝不能降级为子控件。
        # ⚠️ 必须带 WindowStaysOnTopHint：主窗自身就是 topmost，若本 overlay 不加
        # topmost，Windows Z 序会把非 topmost 的它整体压在主窗之下 → 呼吸边框完全
        # 不可见（"调最大亮度也没用"根因）。
        # 编辑蒙版 _editor_dim 现为主窗子控件（lower() 到 _body 之下），不再是与本
        # 窗口抢 topmost 的同级窗口，故加 topmost 无副作用。仅画呼吸边框脉动
        # （满亮度静态，由 windowOpacity 调制呼吸），编辑暗化交给 _EditorDimOverlay。
        self.setParent(
            parent,
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)

    def paintEvent(self, ev: Any) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            p.end()
            return
        # 独立窗口无主窗圆角 mask，手动裁到圆角矩形，避免边框描边在圆角处溢出。
        clip = QPainterPath()
        clip.addRoundedRect(0, 0, w, h, 16, 16)
        p.setClipPath(clip)
        # 边框只画一次、恒定满亮度；呼吸由主窗 _on_breath 调 setWindowOpacity 让
        # DWM 混合（不重绘本窗口 → 主线程零每帧软件绘制，对齐 YeManCC「动画交给
        # 合成器」思路）。该层在 _scrim 之上，游戏前台也可见、不被黑罩压暗。
        self._parent._paint_breath_border(p, w, h)
        p.end()


class _EditorDimOverlay(QWidget):
    """Editor-dim overlay — a plain child widget of the main window (NOT an
    independent top-level window), positioned BEHIND ``_body`` so it dims only
    the background / tray while the editor controls (buttons, sliders) render
    on top and stay bright and operable.

    Paints ONLY the editor dim rectangle (see ``_paint_tray_border``).  Its dim
    depth is controlled by ``_editor_dim_alpha`` inside the paint, not by
    windowOpacity, so it is never modulated by the breathing pulse.  It must
    stay a SEPARATE layer from the breathing border (``_GlowBorderOverlay``,
    whose windowOpacity is driven by the breath) — but it does NOT need to be a
    top-level window: as a child widget it follows the main window's fade /
    move / resize automatically and, crucially, never floats above the editor
    controls (the old top-level + raise_() design covered them and went black)."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._parent = parent
        # 主窗子控件（非顶层窗口）：用 lower() 放到 _body 之下，让编辑蒙版只压暗
        # 背景/托盘，而编辑器控件（按钮/滑块）渲染在蒙版之上、保持明亮可操作——
        # 修掉「蒙版被做成独立顶层窗口 → 成黑窗、随主窗移动有延迟、点击抢焦点、破坏
        # 主窗置顶」的整串 bug。鼠标穿透、不拦点击；几何用窗口局部坐标 self.rect()，
        # 随主窗 move/resize 自动跟随（无延迟）；作为子控件它物理上不是独立窗口，
        # 绝不抢焦点、绝不浮在其他应用窗口之上，主窗 WindowStaysOnTopHint 置顶恢复正常。
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)

    def paintEvent(self, ev: Any) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            p.end()
            return
        clip = QPainterPath()
        clip.addRoundedRect(0, 0, w, h, 16, 16)
        p.setClipPath(clip)
        # 仅编辑态托盘暗化蒙版（位于卡片之上、呼吸边框之下），不随呼吸调制。
        self._parent._paint_tray_border(p, w, h)
        p.end()


def _palette_from_accent(accent: "QColor") -> dict:
    """从 accent 派生 6 色 HSL 色板：accent 三档亮 + 互补色三档。

    用于托盘图标三面渐变（顶/左/右）：accent 变则整套配色自动跟着走，
    与呼吸灯主题保持联动。"""
    h, s, l, _ = accent.getHsl()

    def _make(deg: int = 0, light_mul: float = 1.0) -> QColor:
        new_h = (h + deg) % 360
        new_l = max(0, min(255, int(l * light_mul)))
        return QColor.fromHsl(new_h, s, new_l)

    return {
        "accent_lit":  _make(0, 1.20),
        "accent":      _make(0, 1.00),
        "accent_dark": _make(0, 0.70),
        "comp":        _make(180, 1.00),
        "comp_lit":    _make(180, 1.20),
        "comp_dark":   _make(180, 0.70),
    }


def _draw_ice_cube(p: "QPainter", size: int, accent: "QColor") -> None:
    """在 size×size 透明画布上绘制全息冰块立方体（托盘/窗口/ICO 共用设计）。

    内部在 64 逻辑坐标系绘制，再按 size/64 整体缩放，使同一份代码可渲染
    任意分辨率而不丢失比例。accent 变则整图配色联动（见 _palette_from_accent）。
    """
    k = size / 64.0
    p.save()
    p.scale(k, k)

    # 1) 圆角底框（暗）+ accent 描边外框
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(20, 24, 33))
    p.drawRoundedRect(5, 5, 54, 54, 14, 14)
    p.setPen(QPen(accent, 2.4))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(5, 5, 54, 54, 14, 14)

    # 2) 中央等距立方体冰块：HSL palette 三面渐变
    pal = _palette_from_accent(accent)
    cx, top, w, h, side = 32.0, 10.0, 18.0, 10.5, 20.0
    top_face = QPolygonF([
        QPointF(cx, top), QPointF(cx + w, top + h),
        QPointF(cx, top + 2 * h), QPointF(cx - w, top + h),
    ])
    left_face = QPolygonF([
        QPointF(cx - w, top + h), QPointF(cx, top + 2 * h),
        QPointF(cx, top + 2 * h + side), QPointF(cx - w, top + h + side),
    ])
    right_face = QPolygonF([
        QPointF(cx + w, top + h), QPointF(cx, top + 2 * h),
        QPointF(cx, top + 2 * h + side), QPointF(cx + w, top + h + side),
    ])

    # 左面：accent_dark → accent（深冷→主色，横向）
    g_left = QLinearGradient(cx - w, 0, cx, 0)
    g_left.setColorAt(0.0, pal["accent_dark"])
    g_left.setColorAt(1.0, pal["accent"])
    # 右面：comp_lit → comp_dark（暖亮→深暖，横向）
    g_right = QLinearGradient(cx, 0, cx + w, 0)
    g_right.setColorAt(0.0, pal["comp_lit"])
    g_right.setColorAt(1.0, pal["comp_dark"])
    # 顶面：高光主色 → 互补 → 互补亮（横向三色）
    g_top = QLinearGradient(cx - w, 0, cx + w, 0)
    g_top.setColorAt(0.0, pal["accent_lit"])
    g_top.setColorAt(0.5, pal["comp"])
    g_top.setColorAt(1.0, pal["comp_lit"])

    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(g_right)); p.drawPolygon(right_face)
    p.setBrush(QBrush(g_left));  p.drawPolygon(left_face)
    p.setBrush(QBrush(g_top));   p.drawPolygon(top_face)

    # 3) 棱线（白色细描边，提轮廓，16px 下靠它撑住形状）
    edge = QPen(QColor(255, 255, 255, 240), 1.4)
    edge.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(edge)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPolygon(top_face)
    p.drawPolygon(left_face)
    p.drawPolygon(right_face)

    p.restore()


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
        # 窗口/任务栏/Alt-Tab 图标 = 与托盘同一冰块设计；运行时即覆盖 exe 默认图标，
        # 无需重建打包即可在源码/portable 模式生效。accent 变更时在两处刷新路径同步更新。
        try:
            self.setWindowIcon(self._make_window_icon())
        except Exception:  # noqa: BLE001
            pass
        self._bg = QColor(16, 16, 16, 235)
        # Cached paint layers (see _rebuild_bg_cache): the full-window bg fill
        # is pre-rendered once (on geometry / theme / colour change) and blitted
        # every breath frame instead of re-filling the rounded path in software
        # raster.  The breathing glow (Plan C) is painted directly in the main
        # window's background layer and needs no cache.
        self._bg_cache = None
        self._bg_cache_key = None
        # Last alpha value we actually painted at.  Used to coalesce
        # imperceptible breathing steps (see _on_breath) so we don't re-composite
        # the full translucent window on frames the user can't see change.
        self._last_paint_alpha = None

        # -- custom card background (prototype: opaque self-rendered) -----
        # When enabled, the card tray base is painted with a solid colour or a
        # user image (app-rendered on a stable surface) instead of the default
        # dark fill.  This keeps text crisp (no live-game-frame composite, so
        # no grayscale-AA / fractional-DPR upscaling) and costs no extra frame
        # budget.  Off by default; inert under offscreen.  Read from config if
        # available (it is loaded before the window is built).
        self._card_bg_enabled = False
        self._card_bg_mode = "solid"
        self._card_bg_color = QColor(14, 17, 23)
        self._card_bg_image = ""
        self._card_bg_pixmap = None  # cached, tray-sized (logical px), lazy-built
        self._card_bg_pixmap_key = None  # (tw, th, dpr) 缓存失效判定键（DPR 一致）
        # Per-card fill colour (card background) — default mirrors the previous
        # hardcoded QColor(20, 24, 33, 165); overridden from config below.
        self._card_fill_color = QColor(20, 24, 33)
        self._card_fill_alpha = 165
        _cfg = getattr(self._app, "config_manager", None)
        _cfg = getattr(_cfg, "config", None)
        if _cfg is not None:
            self._card_bg_enabled = bool(getattr(_cfg, "card_bg_enabled", False))
            self._card_bg_mode = str(getattr(_cfg, "card_bg_mode", "solid")) or "solid"
            try:
                self._card_bg_color = QColor(
                    "#" + str(getattr(_cfg, "card_bg_color", "0e1117") or "0e1117"))
            except Exception:
                self._card_bg_color = QColor(14, 17, 23)
            self._card_bg_image = str(getattr(_cfg, "card_bg_image", "") or "")
            # per-card fill colour + opacity
            try:
                self._card_fill_color = QColor(
                    "#" + str(getattr(_cfg, "card_fill_color", "141821") or "141821"))
            except Exception:
                self._card_fill_color = QColor(20, 24, 33)
            self._card_fill_alpha = int(
                getattr(_cfg, "card_fill_alpha", 165) or 165)

        # -- unfocus dim state --------------------------------------------
        # When the window loses focus but stays visible, the breathing light
        # keeps playing (it is NOT paused) but the glow colour brightness is
        # eased toward ``_DIM_UNFOCUSED`` so the neon border + frosted tray
        # glow dim to a "sleeping" state.  ``_glow_dim`` is the live value
        # (1.0 = focused, ~0.3 = dim) animated by ``_dim_timer``.
        self._glow_dim = 1.0
        self._last_tray_dim = 1.0
        self._DIM_UNFOCUSED = 0.3  # glow brightness floor when unfocused-visible
        self._dim_timer = QTimer(self)
        self._dim_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._dim_timer.setInterval(33)  # ~30fps: smooth fade, but sustainable on
                                        # fractional-DPR (1.5x) handhelds — a 60fps
                                        # rate outran the full-window translucent
                                        # repaint and starved scroll input.
        self._dim_timer.timeout.connect(self._dim_tick)
        # Focus/unfocus dim animation envelope (time-based, see _dim_tick).
        # Captured at the start of every transition so an interrupted fade
        # (regaining focus mid-dim) reverses seamlessly from the live value.
        self._dim_from = 1.0
        self._scrim_from = 0.0
        self._dim_to = 1.0
        self._scrim_to = 0.0
        self._dim_start_ts = 0.0

        # Unfocus page-dim scrim: a separate full-window dark overlay (the
        # ``_FocusScrim`` widget) whose alpha is eased toward 0 by the same
        # ``_dim_timer`` when focused, and toward ``_SCRIM_UNFOCUSED`` when a
        # foreign app is on top.  0.45 ≈ "half-transparent black veil" when
        # unfocused; 0 when focused.  The breathing glow also drops to
        # ``_DIM_UNFOCUSED`` (double dim → unmistakable inactive look).
        self._SCRIM_UNFOCUSED = 0.45
        self._scrim_alpha = 0.0
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

        # Opt-in perf profiler heartbeat (no-op unless NBJ_PROFILE is set).
        profile.install_heartbeat()

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

        # Gamepad connection indicator: a compact vector gamepad glyph (no
        # text).  Connected -> green glyph (tooltip shows the pad name);
        # disconnected -> hidden entirely to save header space.  Sits to the
        # LEFT of the battery readout per spec.  Driven by the
        # InteractionController's on_connection_changed callback.
        # Gamepad + battery form one tight status cluster: a 3px gap keeps the
        # connected-pad glyph right next to the device battery readout (the
        # header's default spacing was wider).  Per spec the glyph sits LEFT of
        # the battery.
        status_cluster = QWidget()
        status_layout = QHBoxLayout(status_cluster)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(3)

        self._gp_status = GamepadIcon()
        status_layout.addWidget(self._gp_status)

        self._batt = _BatteryBar()
        status_layout.addWidget(self._batt)

        header.addWidget(status_cluster)

        self._edit_btn = QPushButton("⚙️")
        self._edit_btn.setFlat(True)
        self._edit_btn.setAutoDefault(False)
        self._edit_btn.setDefault(False)
        self._edit_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.10); color: #dfe6f0; "
            "border: 1px solid rgba(255,255,255,0.15); border-radius: 7px; "
            "padding: 4px 10px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.18); }")
        self._edit_btn.clicked.connect(self._open_editor)
        header.addWidget(self._edit_btn)

        self._min_btn = QPushButton("−")  # 普通减号 U+2212：最小化到系统托盘
        self._min_btn.setFlat(True)
        self._min_btn.setAutoDefault(False)
        self._min_btn.setDefault(False)
        self._min_btn.setStyleSheet(
            "QPushButton { background: rgba(127,209,255,0.12); color: #a9d8ff; "
            "border: 1px solid rgba(127,209,255,0.28); border-radius: 7px; "
            "padding: 4px 9px; }"
            "QPushButton:hover { background: rgba(127,209,255,0.22); }")
        self._min_btn.clicked.connect(self._minimize_to_tray)
        header.addWidget(self._min_btn)

        self._exit_btn = QPushButton("✕")  # 真正的退出入口，触发 closeEvent 优雅收尾
        self._exit_btn.setFlat(True)
        self._exit_btn.setAutoDefault(False)
        self._exit_btn.setDefault(False)
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
        # 用 _FitScrollArea（而非裸 QScrollArea）：它把滚动区的「水平最小尺寸」
        # 钉死在一个很小的值（60px），这样内部任何卡片的超宽内容（长进程名 /
        # 长预设名 / 长状态串）都无法把滚动区、进而把整个窗口撑得比屏幕更宽 ——
        # 正是「主页面卡片宽度超过列表宽度、两边被遮挡」的根因。其它所有滚动
        # 区（卡片内的进程/预设列表）早已用 _FitScrollArea，独独主列表漏了。
        # 窗口自身的 setMaximumWidth(min(520, avail)) 仍是最终硬上限。
        scroll = _FitScrollArea()
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
        # 主页面卡片间距 + 外圈留白：2026-08-29 起改为可配置（排版效果页），
        # 默认值 4px / 8px 落 layout.json 的 CardGap / CardOuterMargin。
        _cfg_gap = 4
        _cfg_margin = 8
        try:
            _cm = getattr(self._app, "config_manager", None)
            if _cm is not None:
                _cfg_gap = max(0, int(getattr(_cm.config, "card_gap", 4) or 4))
                _cfg_margin = max(0, int(
                    getattr(_cm.config, "card_outer_margin", 8) or 8))
        except Exception:
            pass
        inner_layout.setSpacing(_cfg_gap)
        # Card-region background margin: leave an 8px tray frame AROUND the
        # cards so the "卡片区背景" colour / opacity / border are clearly
        # visible (previously 0 -> the tray was fully hidden behind the opaque
        # cards, making its settings look like they did nothing).
        inner_layout.setContentsMargins(_cfg_margin, _cfg_margin, _cfg_margin, _cfg_margin)
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
        # 编辑页蒙版深度（页面布局编辑 + 其所有子页面叠在共享托盘之上的纯黑
        # 层）。原先硬编码 QColor(0, 0, 0, 118)，2026-08-29 起改为可配置——
        # 用户可在「呼吸灯自定义 → 编辑页蒙版深度」滑块实时调节。0 = 不压暗，
        # 255 = 全黑；越界值 clamp 到 [0, 255]。0 是合法值，故不能用 `or`。
        _dim_raw = getattr(self._app.config_manager.config,
                           "editor_dim_alpha", 118)
        try:
            self._editor_dim_alpha = max(0, min(255, int(_dim_raw)))
        except (TypeError, ValueError):
            self._editor_dim_alpha = 118
        self._tray_rgb = (14, 17, 23)
        self._tray_alpha = 219
        self._tray_border = True
        self._tray_border_width = 2
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
        _bw_cfg = getattr(_tray_cfg, "tray_border_width", None)
        if _bw_cfg is None:
            _bw_cfg = 2 if bool(getattr(_tray_cfg, "tray_border", True)) else 0
        self.apply_tray(
            str(getattr(_tray_cfg, "tray_color", "0e1117") or "0e1117"),
            int(getattr(_tray_cfg, "tray_opacity", 86) or 86),
            bool(getattr(_tray_cfg, "tray_border", True)),
            int(getattr(_tray_cfg, "tray_blur", 18) or 18),
            border_width=max(0, min(4, int(_bw_cfg))),
        )
        # 页面底色（2026-08-30）：page_bg_color 非空时覆盖主题 bg_app。
        self.apply_page_bg(
            str(getattr(_tray_cfg, "page_bg_color", "") or ""))

        self.panels: list[Any] = []
        # 每页面板容器缓存（见 _build_page_container / _show_page）。
        # 每个顶层页面拥有一个 QWidget 容器，里面装着该页所有面板(+结尾 stretch)。
        # 切页只做「把旧容器从 inner_layout 移除 + 把新容器加进 inner_layout」的
        # 廉价交换，面板永不被销毁重建——这是消除「切换页面极度卡顿」的根治手段
        # （旧实现每次切页都 deleteLater 全部面板再 _setup 重建一遍）。只有
        # 真正的全量重建（设置返回/主题变更/磁贴编辑）才走 _teardown_all_pages()
        # 丢弃缓存。注意：self.panels 这个列表对象被 _SyncBridge 持有引用，
        # 因此切页只能原地 clear()+extend()，绝不能重新赋值，否则 sync_all 会
        # 永远遍历到旧空列表、新页面板永远不同步。
        self._page_containers: dict[int, Any] = {}
        self._page_panels: dict[int, list] = {}
        self._current_page_idx: int = -1
        # Page-slide animation state (see _show_page).
        self._page_strip: Any = None
        self._page_strip_layout: Any = None
        self._slide_anim: Any = None
        self._sliding: bool = False
        self._transitioning: bool = False
        # --- 页面转场（Phase 0 指示条 + Phase 1 快照位图）-------------------
        # 详见 ui/page_transition.py 的模块文档。要点：转场期间真实 widget
        # 一动不动，只在一个覆盖层上画两张页面快照，每帧成本 O(1)。
        self._page_indicator: Any = None        # PageIndicator（页码条子控件）
        self._page_transition: Any = None       # PageTransition（动画驱动器）
        self._page_veil: Any = None             # TransitionVeil（转场覆盖层）
        self._pending_old_snapshot: Any = None  # 旧页快照 QPixmap（转场待用）
        self._pending_transition_dir: int = 1   # +1 新页从右进 / -1 从左进
        # 转场「已置位但动画尚未结束」。rebuild() 的 finally 看到它会把
        # 重建守卫 / 手柄轮询的复位延期到动画结束，避免 30ms 轮询在转场期间
        # 去导航一张用户看不见的页面。
        self._transition_active: bool = False
        # 独立状态：进入「页面布局编辑」的转场（HUD ↔ 编辑器全窗切换）。
        # 与上方卡片分页转场**完全独立**，共用 TransitionVeil / PageTransition
        # 与两阶段盖罩时序，但用各自的状态，互不干扰（见 _open_editor /
        # _close_editor 与 _arm_editor_transition / _run_editor_transition）。
        self._editor_transition: Any = None       # PageTransition（编辑器转场驱动器）
        self._editor_veil: Any = None             # TransitionVeil（编辑器转场覆盖层）
        self._editor_old_snapshot: Any = None     # 旧屏快照 QPixmap（HUD 或编辑器）
        self._editor_transition_dir: int = 1      # +1 编辑器从右进 / -1 编辑器向右出
        self._editor_transition_active: bool = False
        self._rebuild_deferred_ctrl: Any = None
        self._rebuild_deferred_gp: bool = False
        self._hud_signature_open: Optional[str] = None
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
        self._controller.on_gamepad_battery = self._on_gp_battery
        # 主页面：焦点切换到另一个功能卡片时，关闭已打开的疑惑解答弹窗。
        self._controller.on_card_focus_change = self._on_card_focus_changed
        # Shell gamepad actions: Select toggles the layout editor, Start focuses
        # the window.  (_open_editor already toggles, so it serves both enter
        # and exit.)  Wired here so the controller can call back into the shell.
        self._controller.on_toggle_layout = self._open_editor
        self._controller.on_focus_window = self._focus_from_start
        self._build_panels()
        self._controller.refresh()

        # 启动钩子：LS 路径自动探测（空或失效→扫描进程；全程静默 + try/except）。
        # 延后到事件循环启动后执行，避免拖慢开机，且绝不抛异常影响启动。
        QTimer.singleShot(0, self._startup_ls_detect)

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
        self._page_bar_buttons: list = []  # refs to page-bar buttons (see _ensure_page_bar)
        self._build_page_bar()
        # Gamepad/keyboard page switches (app.switch_page / goto_page) and the
        # page-bar buttons converge here to rebuild the visible page.
        self._app._page_changed_cb = self._on_page_switched
        self._controller.set_edit_overlay(self._edit_overlay)
        # Expose the controller to the editor so a reorder can immediately
        # re-point the navigation cursor (cursor follows the moved card).
        self._edit_overlay._controller = self._controller
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
        # 呼吸谷值 / 冲亮参数（2026-08-30 新增，缺省自动回落旧行为）。
        self._breath_floor = getattr(_bcfg, "breath_floor", None)
        self._breath_flash_ms = int(getattr(_bcfg, "breath_flash_ms", 180) or 180)
        self._breath_hold_ms = int(getattr(_bcfg, "breath_hold_ms", 120) or 120)
        self._breath_scrim_ms = int(getattr(_bcfg, "breath_scrim_ms", 200) or 200)
        self._breath = BreathingController(
            period_s=_period, max_brightness=_max,
            enabled=bool(getattr(_bcfg, "breath_enabled", True)),
            floor=self._breath_floor)
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
        # 窗口/任务栏图标随 accent 联动（与托盘同一设计）
        try:
            self.setWindowIcon(self._make_window_icon())
        except Exception:  # noqa: BLE001
            pass
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
        # 注册 AC/DC 电源切换通知（插拔电即时刷新电源计划卡片）。
        self._acdc_handle = None
        self._register_power_source_notifier()
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
        self._prof = _SwitchProfiler()
        self._prof_seq = 0
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

        # 磁贴：启动目标程序成功后把 HUD 收进托盘 / 任务栏（走同一个
        # _minimize_to_tray，带淡出动画 + 托盘气泡）。开关见
        # AppConfig.tile_minimize_after_launch，失败启动不触发。
        tile_block = self._app.blocks.get("tile")
        if tile_block is not None and hasattr(tile_block, "set_minimize_self_callback"):
            tile_block.set_minimize_self_callback(self._minimize_to_tray)

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
        # 滚动冻结窗口：最近一次滚动输入后的短时间内，呼吸辉光暂停整窗重绘
        # （见 _mark_scrolling / _on_breath），消除「滚动 + 半透明层重绘」叠加卡顿。
        self._scroll_freeze_until = 0.0
        # 淡入/淡出进行中：此时辉光窗口透明度由淡出/淡入动画独占，呼吸/暗化
        # 定时器不得擅自改写（否则会打断动画），故用此守卫屏蔽 _on_breath/_dim_tick。
        self._fading = False
        # 丙方案(2026-08-29, 失焦柔和渐隐)：_breath_doused=True 表示呼吸边框当前
        # 处于「已淡出熄灭」态（opacity=0 且呼吸驱动已 pause，故不会被下一 tick
        # 重新点亮）；_breath_anim 持有渐隐/渐显动画，重复触发时先 stop 再重启，
        # 避免两个动画同时写 windowOpacity 打架。
        self._breath_doused = False
        self._breath_anim = None
        # 锁步淡入淡出（最小化/唤醒）专用的光效窗 opacity 动画句柄。与失焦淡出
        # (_breath_anim)、呼吸定时器共用同一个 windowOpacity 属性，必须保证同一时刻
        # 只有一个 writer——_animate_glow_to 起新动画前会 stop 掉这两个旧 writer。
        self._glow_fade_anim = None

        # --- Unfocus scrim (whole-window dim when not the active window) ---
        # Built last so it is the top-most child; keep it raised above the
        # content in resizeEvent too.  Skipped on the offscreen test surface.
        if not self._offscreen:
            # 2026-08-29→修订（修复「游戏前台看不到呼吸」）：呼吸边框脉动现画在
            # _GlowBorderOverlay 上，且必须位于失焦黑罩 _scrim 之上——否则游戏前台
            # 时 45% 黑罩会把呼吸压暗、看不到。呼吸边框/失焦黑罩均 WA_TransparentForMouseEvents，
            # 抬升 z 序不影响点击。z 序（低→高）：编辑蒙版(_editor_dim, 主窗子控件在
            # _body 之下) < 主窗内容 < 黑罩(_scrim, 主窗子控件, 置顶) < 呼吸边框
            # (_glow_border, 独立顶层窗口, 居最顶)。呼吸边框由 windowOpacity 驱动（见
            # _on_breath），不再每帧重绘 → 对齐 YeManCC「动画交给 DWM 合成」。
            # 编辑蒙版层(_editor_dim)已改为「主窗子控件 + lower()」：只压暗背景/托盘，
            # 编辑器控件在其上保持明亮；不再用顶层窗口 raise_()（那会盖住控件→整片黑）。
            self._editor_dim = _EditorDimOverlay(self)
            self._editor_dim.setGeometry(self.rect())
            self._editor_dim.show()
            self._glow_border = _GlowBorderOverlay(self)
            self._glow_border.setGeometry(self.geometry())
            self._glow_border.setWindowOpacity(getattr(self, "_breath_alpha", 1.0))
            self._glow_border.show()
            self._scrim = _FocusScrim(self)
            self._scrim.setGeometry(self.rect())
            self._scrim.setVisible(False)
            self._scrim.raise_()
            self._editor_dim.lower()
            self._glow_border.raise_()

    # -- theme helpers --------------------------------------------------
    def _theme_bg(self) -> QColor:
        """App background colour from the active theme.  Painted fully opaque
        (alpha 255) — the window is no longer translucent, so the DWM does NOT
        re-composite it against the desktop every frame; the frosted feel comes
        from this dark fill plus the cards / breathing glow layered on top.

        2026-08-30: 若配置了 ``page_bg_color``（「主题与蒙版」页的页面底色），
        则优先使用该覆盖值；空串 = 跟随主题（默认行为不变）。"""
        _pg = str(getattr(self._app.config_manager.config,
                          "page_bg_color", "") or "").strip().lstrip("#")
        if len(_pg) == 6:
            try:
                return QColor(int(_pg[0:2], 16), int(_pg[2:4], 16),
                              int(_pg[4:6], 16), 255)
            except ValueError:
                pass
        theme = getattr(self._app.config_manager.config, "theme", None)
        csv = getattr(theme, "bg_app", "16,16,16") if theme else "16,16,16"
        parts = [p.strip() for p in csv.split(",")]
        if len(parts) == 3:
            try:
                return QColor(int(parts[0]), int(parts[1]), int(parts[2]), 255)
            except ValueError:
                pass
        return QColor(16, 16, 16, 255)

    def apply_page_bg(self, color_hex: str) -> None:
        """Live-apply the page background colour (2026-08-30 新增).

        ``color_hex`` 为 "rrggbb"；空串 = 跟随主题（回落 ``_theme_bg``）。
        背景 ``_bg`` 是整窗填充 + 托盘基色（自定义背景关闭时），改后必须
        丢弃 ``_bg_cache`` 让下一个 paintEvent 重建缓存层。
        """
        hexv = str(color_hex or "").strip().lstrip("#")
        if len(hexv) == 6:
            try:
                self._bg = QColor(int(hexv[0:2], 16), int(hexv[2:4], 16),
                                  int(hexv[4:6], 16), 255)
            except ValueError:
                self._bg = self._theme_bg()
        else:
            self._bg = self._theme_bg()
        self._bg_cache = None
        self._last_paint_alpha = None
        self.update()

    # -- breathing ------------------------------------------------------
    def _on_breath(self, alpha: float) -> None:
        self._breath_alpha = alpha
        if self._offscreen:
            return
        # 2026-08-29→修订（修复「游戏前台看不到呼吸 / 掌机卡顿」）：
        # 呼吸边框脉动已迁到独立顶层窗口 ``_GlowBorderOverlay``（见
        # ``_paint_breath_border``），该窗口位于失焦黑罩 ``_scrim`` 之上、且亮度
        # **不**受 ``_glow_dim`` 失焦暗化调制——所以即使游戏在前台（HUD 非激活），
        # 呼吸也持续、且不被 45% 黑罩压暗，这正是 HUD 的真实使用场景。
        # 早期这里对非激活窗口 early-return 冻结呼吸，导致「游戏前台完全看不到呼吸」；
        # 旧理由是「避免昂贵整窗重绘」，但如今呼吸只改那个很小的半透明 overlay 的
        # windowOpacity（DWM 混合、不重绘），主窗与 6 张卡片完全不参与，故可安全
        # 去掉冻结。
        # Scroll freeze: while the user is actively scrolling, skip the breath
        # repaint to avoid any list jank.  The breath phase keeps advancing
        # above, so it resumes seamlessly.
        if time.monotonic() < getattr(self, "_scroll_freeze_until", 0.0):
            return
        # Coalesce imperceptible breathing steps (brightness barely moved).
        now = time.monotonic()
        last = getattr(self, "_last_paint_alpha", None)
        last_ts = getattr(self, "_last_paint_ts", None)
        if (last is not None
                and abs(alpha - last) < _BREATH_SKIP_DA
                and last_ts is not None
                and (now - last_ts) < _BREATH_SKIP_MAX_S):
            return
        self._last_paint_alpha = alpha
        self._last_paint_ts = now
        # YeManCC 式：只设呼吸 overlay 的 windowOpacity（DWM 负责混合），本窗口
        # 不重绘、主线程零每帧软件绘制；主窗与 6 张卡片完全不参与呼吸 → 性能回到
        # 0829「零呼吸」顺滑基线；同时该层在 _scrim 之上，游戏前台也清晰可见。
        if not getattr(self, "_fading", False):
            b = getattr(self, "_glow_border", None)
            if b is not None:
                try:
                    b.setWindowOpacity(alpha)
                except Exception:
                    pass

    # -- 锁步光效（最小化/唤醒与主窗同一条时钟）---------------------------
    def _animate_glow_to(self, target: float, duration_ms: int,
                         easing: "QEasingCurve.Type") -> None:
        """让呼吸边框 overlay 的 windowOpacity 缓动到 ``target``，与调用方（主窗
        淡入/淡出）共用同一 duration/easing，实现主窗与光效「同帧出现/消失」。

        先停掉所有可能写同一属性的旧动画——失焦淡出 ``_breath_anim``、上一次锁步
        动画 ``_glow_fade_anim``——保证只有一个 writer 在动 opacity，杜绝抢戏/重影。
        """
        b = getattr(self, "_glow_border", None)
        if b is None or getattr(self, "_offscreen", False):
            return
        ba = getattr(self, "_breath_anim", None)
        if ba is not None:
            ba.stop()
        ga = getattr(self, "_glow_fade_anim", None)
        if ga is not None:
            ga.stop()
        anim = QPropertyAnimation(b, b"windowOpacity")
        anim.setDuration(duration_ms)
        anim.setEasingCurve(easing)
        anim.setStartValue(b.windowOpacity())
        anim.setEndValue(float(target))
        self._glow_fade_anim = anim
        anim.start()

    def _resume_breath_after_fade(self) -> None:
        """锁步淡入结束时调用：若此前因失焦熄灭，解熄灭并恢复呼吸驱动（不额外冲亮）。

        光效 opacity 已由锁步动画带到当前呼吸相位 ``_breath_alpha``，呼吸从这里无缝
        续接。与 ``_fade_breath_in`` 的「冲亮」是不同意图——后者用于普通聚焦/热键唤出，
        不在最小化/唤醒路径上跑（由 changeEvent 的 ``_fading`` 守卫避让，避免两个动画
        抢同一个 windowOpacity）。
        """
        breath = getattr(self, "_breath", None)
        if breath is None:
            return
        if getattr(self, "_breath_doused", False):
            self._breath_doused = False
        try:
            breath.resume()
        except Exception:
            pass

    def _breath_edge_region(self) -> Any:
        """保留的几何辅助：返回「窗口 − 托盘」的边框带 QRegion。

        注：2026-08-29 修订后呼吸边框脉动已迁到独立 overlay（_paint_breath_border），
        不再经由主窗 ``self.update(region)`` 路径，故本方法当前已无调用方，仅留作
        几何参考 / 防回归。主窗静态底光位于托盘区，仅在主窗重绘时刷新，零每帧成本。
        """
        r = QRegion(self.rect())
        tray = self._tray_region()
        if tray is not None and not tray.isEmpty():
            r = r.subtracted(tray)
        return r

    def _tray_region(self) -> Any:
        """返回卡片托盘/编辑器蒙层在窗口坐标下的 QRegion。

        该区域包含所有卡片和编辑面板，因此只在真正需要重绘托盘内容时调用。
        （历史：曾用于把托盘整块排除在呼吸每帧重绘之外；2026-08-29 修订后呼吸
        边框脉动已迁到独立 overlay，_breath_edge_region 不再参与呼吸路径。）
        """
        tr = self._scrim_rect()
        if tr is None:
            return QRegion()
        return QRegion(tr)

    def _tray_rect(self) -> Any:
        """卡片托盘在窗口坐标下的 QRect；取不到时返回 None（不要抛异常）。

        调用方很杂（每帧的呼吸回调、缓存 key 计算），这里必须吞掉所有异常：
        一旦抛出，_on_breath / _dim_tick 的 except 分支会退化成**整窗 update()**，
        反而造成严重卡顿。
        """
        vp = getattr(self, "_scroll", None)
        if vp is None:
            return None
        try:
            if vp.parent() is None:
                return None
            tp = vp.mapTo(self, QPoint(0, 0))
            tw, th = vp.width(), vp.height()
            if tw > 0 and th > 0:
                return QRect(tp.x(), tp.y(), tw, th)
        except Exception:
            pass
        return None

    def _scrim_rect(self) -> Any:
        """Unified card-tray / editor scrim rectangle in window coordinates.

        HUD mode: the scroll viewport fills ``self._body`` exactly, so this
        is identical to ``_tray_rect()``.  Editor mode: ``_scroll`` is hidden
        and keeps its old geometry, while ``_body`` (the QStackedWidget that
        holds both pages) fills the same area in both modes.  Anchoring the
        scrim to ``_body`` removes the 30 px mismatch that produced the visible
        seam at the bottom of the layout editor.
        """
        body = getattr(self, "_body", None)
        if body is not None and body.parent() is not None:
            try:
                tp = body.mapTo(self, QPoint(0, 0))
                bw, bh = body.width(), body.height()
                if bw > 0 and bh > 0:
                    return QRect(tp.x(), tp.y(), bw, bh)
            except Exception:
                pass
        return self._tray_rect()

    def _is_editor_active(self) -> bool:
        """Return True when the edit overlay (layout / tile / procmon / ...)
        is the current page of ``self._body``."""
        body = getattr(self, "_body", None)
        if body is None:
            return False
        try:
            return body.currentWidget() is self._edit_overlay
        except Exception:
            return False

    def _mark_scrolling(self) -> None:
        """Note recent scroll input and freeze the breathing-glow repaint.

        Called from the viewport event filter on wheel / arrow-key events.
        The breathing glow lives on a full-window translucent overlay that
        repaints at ~20fps; repainting it *while the user is scrolling the card
        list* makes the scroll janky (WorkBuddy has no such always-on
        translucent layer, which is why its scroll stays smooth).  Holding the
        glow steady for a short window after each scroll input removes that
        overlap; ``_on_breath`` keeps advancing the phase and resumes the pulse
        seamlessly once scrolling settles.
        """
        self._scroll_freeze_until = time.monotonic() + 0.18
        # 同时冻住呼吸相位：滚轮/方向键会让主线程繁忙、饿死呼吸 tick，
        # 若不冻相位，操作结束后亮度会突跳到墙钟该到的位置。冻住则卡在
        # 当前亮度、结束后无缝续接（配合 BreathingController.note_activity）。
        breath = getattr(self, "_breath", None)
        if breath is not None:
            try:
                breath.note_activity(0.18)
            except Exception:
                pass

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
        """Start a fixed-duration (fast) brightness/scrim transition.

        ``target`` is the *glow brightness* (1.0 focused, ~0.3 dim).  The
        unfocus scrim target is derived from the same focus state by the
        caller (see ``changeEvent`` / ``_resume_activity``), so here we only
        drive ``_glow_dim`` and let the scrim ease in lock-step toward
        ``_scrim_to``.
        """
        target = max(0.0, min(1.0, float(target)))
        scrim_to = 0.0 if target >= 1.0 else self._SCRIM_UNFOCUSED
        # Snapshot the live values as the animation start so an interrupted
        # fade (regaining focus mid-dim) reverses smoothly from where it is.
        self._dim_from = self._glow_dim
        self._scrim_from = self._scrim_alpha
        self._dim_to = target
        self._scrim_to = scrim_to
        self._dim_start_ts = time.monotonic()
        # Invalidate the paint-coalescing tracker so the focus/unfocus
        # transition is never swallowed by an imperceptible-step skip.
        self._last_paint_alpha = None
        if not self._dim_timer.isActive():
            self._dim_timer.start()

    def _dim_tick(self) -> None:
        # Fixed-duration (180 ms) smoothstep so focus gain/lose is uniformly
        # snappy — the old per-tick exponential ease decelerated asymptotically
        # and crawled through the final ~20% of the change (felt ~0.5s slow).
        ts = time.monotonic()
        span = ts - self._dim_start_ts
        raw = span / 0.18
        if raw >= 1.0:
            raw = 1.0
        # smoothstep: 3t^2 - 2t^3 — fast start, gentle settle, no tail crawl.
        e = raw * raw * (3.0 - 2.0 * raw)
        nxt = self._dim_from + (self._dim_to - self._dim_from) * e
        self._glow_dim = nxt
        # 2026-08-29→修订：呼吸边框脉动已迁到独立 overlay（_paint_breath_border），
        # 且其亮度**不**受 _glow_dim 调制，故失焦暗化过渡无需重绘呼吸层——呼吸由
        # _on_breath 持续驱动。整窗暗化仍由下方 _FocusScrim.set_alpha 负责。

        # Ease the unfocus scrim alpha toward its target in lock-step.
        scrim = getattr(self, "_scrim", None)
        snxt = self._scrim_from + (self._scrim_to - self._scrim_from) * e
        self._scrim_alpha = snxt
        if scrim is not None:
            scrim.set_alpha(snxt)

        if raw >= 1.0:
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
                     color_hex: str,
                     floor: float | None = None,
                     flash_ms: int | None = None,
                     hold_ms: int | None = None,
                     scrim_ms: int | None = None) -> None:
        """Live-apply breathing settings changed in the editor sub-page.

        The four trailing arguments are optional (2026-08-30) so existing
        three-argument call sites keep working unchanged.  ``None`` means
        "leave the current value alone" for the timings, while ``floor=None``
        means "fall back to ``core.timers._BREATH_FLOOR``".
        """
        if getattr(self, "_breath", None) is not None:
            self._breath.set_period(period_s)
            self._breath.set_max_brightness(max_brightness)
            self._breath.set_floor(floor)
        # Cache the tuning knobs so _fade_breath_in can use them without
        # reaching back into the config object on every focus change.
        self._breath_floor = floor
        if flash_ms is not None:
            self._breath_flash_ms = int(flash_ms)
        if hold_ms is not None:
            self._breath_hold_ms = int(hold_ms)
        if scrim_ms is not None:
            self._breath_scrim_ms = int(scrim_ms)
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
        # 窗口/任务栏图标随 accent 联动（与托盘同一设计）
        try:
            self.setWindowIcon(self._make_window_icon())
        except Exception:  # noqa: BLE001
            pass
        # The tray's frosted glow layer uses the breathing colour — rebuild it.
        self._rebuild_tray_glow()
        if not self._offscreen:
            self.update()
        # The breathing colour is the single theme-accent source: re-skin every
        # block + card from it so the change is visible everywhere at once.
        self._refresh_all_accents()

    def set_breath_enabled(self, enabled: bool) -> None:
        """Live toggle the breathing pulse.  When disabled the glow is held at a
        steady (peak) brightness instead of pulsing — the theme colour still
        applies everywhere; only the animation stops."""
        if getattr(self, "_breath", None) is not None:
            self._breath.set_enabled(bool(enabled))
            self._last_paint_alpha = None
            if not self._offscreen:
                # 关闭时 _breath_alpha=1.0（峰值），边框由 windowOpacity=1.0 保持
                # 恒定满亮度（不脉动）；开启时由呼吸定时器经 setWindowOpacity 驱动。
                if not enabled:
                    self._breath_alpha = 1.0
                b = getattr(self, "_glow_border", None)
                if b is not None:
                    b.setWindowOpacity(self._breath_alpha)
                    b.show()
                else:
                    self.update()

    def _repaint_glow(self) -> None:
        """主题色/几何变更时刷新静态层：主窗底光 + 呼吸边框(满亮度) + 编辑蒙版。

        呼吸边框为独立顶层窗口、编辑蒙版为主窗子控件(位于 _body 之下)、主窗底光走
        背景层路径；呼吸动画由 windowOpacity 驱动，不在此重绘。
        """
        # 主窗整体重绘：主题色变更时静态底光也要刷新（它位于托盘区）。
        self.update()
        # 呼吸边框(满亮度静态)：几何/主题变更时重描一次。
        b = getattr(self, "_glow_border", None)
        if b is not None:
            b.update()
        # 编辑蒙版层(_editor_dim)：几何/主题变更时重描。
        d = getattr(self, "_editor_dim", None)
        if d is not None:
            d.update()

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
                if hasattr(p, "set_card_fill"):
                    p.set_card_fill(self._card_fill_color, self._card_fill_alpha)
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
        self._bg_cache = None
        self._last_paint_alpha = None
        self.update()
        self._repaint_glow()

    def apply_tray(self, color_hex: str, opacity: int, border: bool,
                   blur: int = 18, border_width: int | None = None) -> None:
        """Live-apply the card-tray background (colour / opacity / border / blur).

        The tray is a translucent rounded panel painted in ``paintEvent``
        *over* the breathing-light window, so the breathing glow shows through
        it (frosted by ``blur``).  This restores the original "tray lets the
        breathing light through" behaviour and replaces the old opaque desktop
        snapshot that hid the glow.

        Only colour / opacity / border / blur are stored here; the actual
        blurred breathing-glow layer behind the tray is rebuilt via
        ``_rebuild_tray_glow``.

        2026-08-30: 外框线从 开/关 布尔升级为可调宽度（0=关，1..4px）。
        ``border_width`` 为 None 时从旧布尔参数派生（True→2px，False→0px），
        旧调用（呼吸灯自定义页）零改动兼容。
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
        if border_width is None:
            border_width = 2 if bool(border) else 0
        self._tray_border_width = max(0, min(4, int(border_width)))
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
        self._repaint_glow()

    def apply_card_layout(self, gap: int, outer_margin: int) -> None:
        """Live-apply the main-page card spacing + outer tray margin.

        Mirrors apply_tray/apply_breath: pushed by the 排版效果 editor on save
        (and read from config at startup).  ``gap`` = vertical gap between the
        stacked functional cards; ``outer_margin`` = the tray-frame padding
        around the card cluster.

        CRITICAL — where the spacing actually lives
        --------------------------------------------
        The functional cards are NOT direct children of ``inner_layout``.
        ``inner_layout`` only ever hosts the single *active page container*
        widget (page switches swap containers in/out — see _apply_panels /
        the _page_containers cache).  Each container owns its own QVBoxLayout
        whose spacing was copied from ``inner_layout.spacing()`` at BUILD time
        (``_build_page_container`` → ``cl.setSpacing(self.inner_layout.spacing())``).
        So changing ``inner_layout`` alone does NOTHING visible: we must also
        push the new spacing into every built container's internal layout.
        The outer margin (gap between the container and the window edge) is
        still driven by ``inner_layout``'s contentsMargins, handled below.
        """
        g = max(0, int(gap))
        m = max(0, int(outer_margin))
        lay = getattr(self, "inner_layout", None)
        if lay is not None:
            lay.setSpacing(g)
            lay.setContentsMargins(m, m, m, m)
        # Real card spacing lives inside each page container's layout.
        # Update every built container so the change is visible immediately
        # (both the active page and the cached neighbours that will show later).
        for cont in getattr(self, "_page_containers", {}).values():
            if cont is None:
                continue
            cl = cont.layout()
            if cl is not None:
                cl.setSpacing(g)
                cl.invalidate()
                cl.activate()
        inner = getattr(self, "inner", None)
        scroll = getattr(self, "_scroll", None)
        if inner is not None:
            inner.update()
        if scroll is not None:
            scroll.update()
        self.update()

    def apply_editor_dim(self, alpha: int) -> None:
        """Live-apply the editor dim-overlay depth (0..255).

        ``_paint_tray_border`` paints a pure-black layer over the shared tray
        whenever the layout editor (or any sub-page) is open, so editor controls
        keep contrast against the breathing glow.  ``alpha`` is the QColor alpha
        of that layer: 0 = no dim (glow fully visible through the editor),
        255 = fully black.  Cheap: only a repaint, no cache rebuild (the dim is
        painted directly, it is not part of ``_bg_cache``).
        """
        try:
            self._editor_dim_alpha = max(0, min(255, int(alpha)))
        except (TypeError, ValueError):
            return
        self.update()
        self._repaint_glow()

    def apply_card_bg(self, enabled: bool, mode: str, color_hex: str,
                      image_path: str) -> None:
        """Live-apply the custom card background (prototype).

        ``enabled=False`` keeps the original frosted tray.  When enabled, the
        tray base is painted with a solid colour or a user image (self-rendered,
        opaque — visually identical to a stable surface so text stays crisp),
        and the dark tray-overlay colour is skipped so the custom base shows
        through.  The breathing glow + border are preserved either way.
        """
        self._card_bg_enabled = bool(enabled)
        self._card_bg_mode = "image" if str(mode) == "image" else "solid"
        try:
            self._card_bg_color = QColor("#" + str(color_hex).lstrip("#"))
        except Exception:
            self._card_bg_color = QColor(14, 17, 23)
        self._card_bg_image = str(image_path or "")
        # 强制下次 paint 重建缓存；同时清 key，保证基于 (tw,th,dpr) 的失效判定生效。
        self._card_bg_pixmap = None
        self._card_bg_pixmap_key = None
        self.update()

    def apply_card_fill(self, color_hex: str, alpha: int) -> None:
        """Live-apply the per-card fill colour + opacity to every card.

        The card fill is painted inside each ``BlockPanel`` (not the window),
        so we store the value and broadcast it to all panels; the window
        itself needs no special paint for this.  ``alpha`` is 0-255.
        """
        try:
            self._card_fill_color = QColor(
                "#" + str(color_hex).lstrip("#"))
        except Exception:
            self._card_fill_color = QColor(20, 24, 33)
        self._card_fill_alpha = max(0, min(255, int(alpha)))
        # NOTE: 这里曾有两处调试 print（每次切页都会走 _show_page → 本函数，
        # 向 stderr 写日志会明显拖慢切页）。已移除；如需调试请改用
        # ``NBJ_PROFILE=1`` 的 _SwitchProfiler 或文件日志。
        for p in getattr(self, "panels", []) or []:
            try:
                if hasattr(p, "set_card_fill"):
                    p.set_card_fill(self._card_fill_color, self._card_fill_alpha)
            except Exception:
                pass
        self.update()

    # -- card tray (translucent, breathing-light shows through) --------
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

    def _layer_key(self, w: int, h: int) -> tuple:
        """缓存层的身份标识：逻辑尺寸 + 设备像素比。

        ⚠️ 千万不要用 ``cache.width() != w`` 判断缓存是否失效。
        ``QPixmap.width()`` 返回的是**设备像素**宽度，不是逻辑像素：
        DPR=1.5、逻辑宽 420 时它返回 630。原代码正是这么写的，于是缓存永远
        判定失效 —— 每个 paintEvent 都要重建两张整窗缓存（背景 1 次整窗路径
        填充 + 辉光 4 次整窗路径描边 + 两张 device 分辨率 QPixmap 分配）。
        呼吸灯 25fps + 调光 30fps ≈ 每秒 55 次，这是 2026-08-29 用户报的
        「异常卡顿」的直接根因。改用显式 key 比对。
        """
        try:
            dpr = round(float(self.devicePixelRatio()), 3)
        except Exception:
            dpr = 1.0
        return (int(w), int(h), dpr)

    def _bg_layer_key(self, w: int, h: int) -> tuple:
        key = self._layer_key(w, h)
        bg = getattr(self, "_bg", None)
        try:
            key = key + ((bg.red(), bg.green(), bg.blue(), bg.alpha()),)
        except Exception:
            key = key + (None,)
        return key

    def _layer_stale(self, attr: str, key: tuple) -> bool:
        """``self.<attr>`` 对应的缓存层是否需要重建。"""
        cache = getattr(self, attr, None)
        if cache is None:
            return True
        try:
            if cache.isNull():
                return True
        except Exception:
            return True
        return getattr(self, attr + "_key", None) != key

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
        self._bg_cache_key = self._bg_layer_key(w, h)

    def _rebuild_tray_glow(self) -> None:
        """Trigger a repaint of the tray scanline when geometry/colour changes.

        The tray glow is now painted directly inside ``_paint_tray`` as a local
        gradient (no pre-blurred full-window pixmap).  This avoids the colour
        bleed / mysterious overlay bugs caused by Gaussian blur spreading the
        scanline into the header/page-bar area.  We keep this helper so every
        existing caller (geometry changes, blur/colour settings, etc.) still
        has a well-defined hook to refresh the tray region.
        """
        if getattr(self, "_offscreen", False):
            return
        # Clear the old blurred layer if any; it is no longer used.
        self._tray_glow_blur = None
        self.update(self._tray_region())

    def _paint_tray(self, p: Any, w: int, h: int) -> None:
        """Paint the card tray / editor backdrop: a translucent rounded panel
        over the breathing glow.  The rectangle now comes from ``_scrim_rect()``
        so the HUD and the editor share one unified backdrop — eliminating the
        double-scrim seam reported at the bottom of the layout editor.

        When the layout editor (or any of its sub-pages) is active, a dark dim
        overlay is painted on top (see ``_paint_tray_border``) so the editor
        controls remain readable against the shared backdrop.

        2026-08-29：托盘顶部的「呼吸扫描线」光带已**永久移除**——用户明确要求
        主页面与编辑页都不再出现任何蓝色渐变条带。原先它只在非编辑态绘制，
        因此主页面仍会露出一条 26px 高的青蓝色渐变带。现在托盘只有纯色底 +
        托盘色两层，视觉更干净。"""
        rect = self._scrim_rect()
        if rect is None or rect.isEmpty():
            return
        tp, tw, th = rect.topLeft(), rect.width(), rect.height()
        rr = 14
        path = QPainterPath()
        path.addRoundedRect(tp.x(), tp.y(), tw, th, rr, rr)
        p.save()
        p.setClipPath(path)
        # Tray base.  Custom background (prototype) replaces the default dark
        # fill when enabled; otherwise the original frosted dark base.
        if self._card_bg_enabled:
            self._paint_card_bg_base(p, tp, tw, th)
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._bg)
            p.drawRoundedRect(tp.x(), tp.y(), tw, th, rr, rr)

        # 2026-08-29 移除：托盘顶部「呼吸扫描线」光带（26px 青蓝渐变）。
        # 用户反馈主页面出现"奇怪的蓝色光带"，要求完全取消。此处不再绘制任何
        # 渐变条带，托盘保持纯色。

        # Tray colour @ opacity.  ALWAYS painted so the "卡片区背景" editor
        # card (colour + opacity) is effective even when a custom tray base is
        # active — the old `if not self._card_bg_enabled` guard skipped this
        # layer, which made the 卡片区背景 colour/opacity controls look dead
        # whenever the user had 自定义背景 enabled.  When 自定义背景 is on, its
        # base paints first (above) and this tray colour overlays it; set the
        # tray opacity low (or match the colours) if you want the custom base
        # to dominate.  Keeping both layers live means every tray control in
        # the editor actually does something.
        r, g, b = self._tray_rgb
        p.setBrush(QColor(r, g, b, self._tray_alpha))
        p.drawRoundedRect(tp.x(), tp.y(), tw, th, rr, rr)
        # 外框线（2026-08-30）：宽度用户可调（0=关，1..4px）。颜色跟随呼吸灯
        # 主题色（与 mini-HUD 预览一致）。此前 _tray_border 只存不画——真正
        # 可见的描边一直来自呼吸边框层；现在托盘自描边在托盘色之上绘制。
        _bw = int(getattr(self, "_tray_border_width", 0) or 0)
        if _bw > 0:
            glow = getattr(self, "_breath_color", None) or getattr(
                self, "_accent", None)
            if not isinstance(glow, QColor):
                try:
                    glow = QColor(glow.r, glow.g, glow.b)
                except Exception:
                    glow = QColor(57, 211, 196)
            p.setBrush(Qt.BrushStyle.NoBrush)
            pen = QPen(QColor(glow.red(), glow.green(), glow.blue(), 190))
            pen.setWidthF(float(max(1, _bw)))
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            # 收半个线宽画描边，避免粗线被裁剪区吃掉外半圈。
            _inset = _bw / 2.0
            p.drawRoundedRect(
                QRectF(float(tp.x()) + _inset, float(tp.y()) + _inset,
                       float(tw) - _bw, float(th) - _bw),
                float(rr) - _inset, float(rr) - _inset)
        p.restore()

    def _paint_tray_border(self, p: Any, w: int, h: int) -> None:
        """Editor dim, drawn in the dedicated ``_EditorDimOverlay`` (above the
        cards, below the breathing border / unfocus scrim).  The crisp tray
        border is part of the breathing frame painted on ``_GlowBorderOverlay``
        (see ``_paint_breath_frame``), so it no longer lives here.

        The dim must sit *above* the tray (and above the cards) so the editor
        controls keep enough contrast while editing.
        """
        rect = self._scrim_rect()
        if rect is None or rect.isEmpty():
            return
        # Editor-mode dim overlay: darken the shared tray while the editor is
        # open so editor controls keep enough contrast.  Depth is user-
        # configurable (呼吸灯自定义 → 编辑页蒙版深度); 0 disables the layer
        # entirely so the breathing glow shows through the editor unchanged.
        if self._is_editor_active() and self._editor_dim_alpha > 0:
            tp, tw, th = rect.topLeft(), rect.width(), rect.height()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, self._editor_dim_alpha))
            p.drawRoundedRect(tp.x(), tp.y(), tw, th, 14, 14)

    def _paint_breath_frame(self, p: Any, w: int, h: int) -> None:
        """方案 C（修订）：静态中心底光，画在主窗背景层（卡片之后、托盘区内）。

        - 恒定不随呼吸变（呼吸色，低 alpha），透出 65% 不透明卡片形成氛围背光。
        - 位于托盘区，仅在主窗重绘（主题/几何变更）时刷新——零每帧成本，与呼吸
          动画彻底解耦。
        - 呼吸「边框脉动」已迁到独立顶层窗口 ``_GlowBorderOverlay``
          （见 ``_paint_breath_border``），不再画在主窗/卡片绘制路径上。
        """
        glow = self._breath_color if getattr(self, "_breath_color", None) is not None else self._accent
        if not isinstance(glow, QColor):
            try:
                glow = QColor(glow.r, glow.g, glow.b)
            except Exception:
                glow = QColor(127, 209, 255)
        # 1) 静态中心底光（恒定，不随呼吸变）。
        tr = self._scrim_rect()
        if tr is not None and not tr.isEmpty():
            cx, cy = float(tr.center().x()), float(tr.center().y())
            rad = max(float(tr.width()), float(tr.height())) * 0.62
            grad = QRadialGradient(cx, cy, rad)
            r, g, b = glow.red(), glow.green(), glow.blue()
            grad.setColorAt(0.0, QColor(r, g, b, 55))
            grad.setColorAt(1.0, QColor(r, g, b, 0))
            p.setBrush(grad)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(tr, 14, 14)

    def _paint_breath_border(self, p: Any, w: int, h: int) -> None:
        """方案 C（修订）：呼吸「边框脉动」，画在独立顶层窗口 ``_GlowBorderOverlay``。

        该窗口位于失焦黑罩 ``_scrim`` 之上、主窗卡片绘制路径之外：
        - 可见性：亮度**不**受 ``_glow_dim`` 失焦暗化调制，故游戏在前台（HUD 非激活）
          时仍持续呼吸、不被 45% 黑罩压暗——这是 HUD 的真实使用场景。
        - 性能：脉动只重绘这一个很小的半透明表面（DWM 合成，GPU 廉价），主窗与
          6 张卡片完全不参与呼吸重绘 → 回到 0829「零呼吸」顺滑基线；这正是
          YeManCC 之类「背景视频」工具不卡的原因（动画层交给 GPU 合成，主线程零重绘）。
        """
        glow = self._breath_color if getattr(self, "_breath_color", None) is not None else self._accent
        if not isinstance(glow, QColor):
            try:
                glow = QColor(glow.r, glow.g, glow.b)
            except Exception:
                glow = QColor(127, 209, 255)
        # 满亮度静态边框：呼吸由窗口 windowOpacity 调制（见 _on_breath），故这里
        # 不乘 _breath_alpha，几何/主题变更时才重绘一次。
        # 2026-08-29 (甲方案): 三层描边 α60/105/165 -> α80/130/190、宽度 5/3/2 ->
        # 6/3.5/2.5，让边框在任意呼吸相位都读作清晰辉光（与 _BREATH_FLOOR 0.2->0.1
        # 一起加大呼吸可见度）。仍只画一次，性能零变化。
        r, g, b = glow.red(), glow.green(), glow.blue()
        for off, aw, wdt in ((2.0, 80, 6.0), (4.0, 130, 3.5), (6.0, 190, 2.5)):
            col = QColor(r, g, b, int(aw))
            pen = QPen(col)
            pen.setWidthF(wdt)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            # 2026-08-29: 改为 QRectF 形式 — PyQt6 的 drawRoundedRect 不接受 6 个
            # float 重载，前 4 个位置参数必须是 int 或整体是 QRectF；用 QRectF
            # 保留亚像素精度，避免某些 PyQt6 构建下报 TypeError 导致整个
            # paintEvent 中断、border 完全画不出（z 序修复后仍看不到的真正
            # 第二层根因）。
            p.drawRoundedRect(
                QRectF(float(off), float(off),
                       float(w) - 2.0 * off, float(h) - 2.0 * off),
                16.0 - off, 16.0 - off)

    def _paint_card_bg_base(self, p: Any, tp: Any, tw: int, th: int) -> None:
        """Paint the custom tray base: a solid colour, or a cover-scaled user
        image.  Falls back to the solid colour when the image is missing."""
        if self._card_bg_mode == "image" and self._card_bg_image:
            # DPR 一致缓存失效判定：pixmap.width() 返回设备像素（已按 DPR 缩放），
            # 不能直接拿它和逻辑像素 tw/th 比（DPR≠1 时永远不等 → 每帧重建 → 卡顿）。
            # 改用显式 (tw, th, dpr) key，与 _layer_key 同一套路。
            _dpr = max(1.0, float(self.devicePixelRatio()))
            if (self._card_bg_pixmap is None
                    or self._card_bg_pixmap_key != (tw, th, _dpr)):
                self._rebuild_card_bg_cache(tw, th)
            if self._card_bg_pixmap is not None and not self._card_bg_pixmap.isNull():
                p.drawPixmap(tp.x(), tp.y(), self._card_bg_pixmap)
                # 2026-08-30 修复：图片蒙版色（40% 不透明）叠加 —— 与蒙版页
                # 预览保持一致。旧实现画完图直接 return，「图片蒙版色」色板
                # 在 image 模式下没有任何效果，亮图上卡片文字看不清。
                mc = QColor(self._card_bg_color)
                mc.setAlpha(102)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(mc)
                p.drawRoundedRect(tp.x(), tp.y(), tw, th, 14, 14)
                return
        # solid base (or image unavailable)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._card_bg_color)
        p.drawRoundedRect(tp.x(), tp.y(), tw, th, 14, 14)

    def _rebuild_card_bg_cache(self, tw: int, th: int) -> None:
        """Pre-render the custom card background into ``_card_bg_pixmap`` at
        *device* resolution (DPR-correct, like _make_dpr_pixmap) so blitting
        each paint frame is cheap and the image stays crisp on fractional DPR
        (e.g. 1.5x handheld).  Rebuilt lazily when the tray geometry changes."""
        if getattr(self, "_offscreen", False) or tw <= 0 or th <= 0:
            self._card_bg_pixmap = None
            return
        dpr = max(1.0, float(self.devicePixelRatio()))
        pm = self._make_dpr_pixmap(tw, th)
        pm.fill(Qt.GlobalColor.transparent)
        pp = QPainter(pm)
        pp.setRenderHint(QPainter.RenderHint.Antialiasing)
        img = QImage(self._card_bg_image)
        if not img.isNull():
            # Render the source at *device* resolution so the blit into the
            # DPR-tagged pixmap lands 1:1 on device pixels (no 1.5x upscale blur).
            dw, dh = int(round(tw * dpr)), int(round(th * dpr))
            scaled = img.scaled(
                dw, dh,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation)
            sx = max(0, (scaled.width() - dw) // 2)
            sy = max(0, (scaled.height() - dh) // 2)
            pp.drawImage(QRect(0, 0, tw, th), scaled, QRect(sx, sy, dw, dh))
            pp.end()
            self._card_bg_pixmap = pm
            self._card_bg_pixmap_key = (tw, th, dpr)
        else:
            # Invalid image -> no cached pixmap; the tray falls back to the
            # solid-colour base in _paint_card_bg_base (and we avoid reloading
            # a known-bad path every paint frame).
            pp.end()
            self._card_bg_pixmap = None
            self._card_bg_pixmap_key = (tw, th, dpr)

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
        """托盘图标：全息冰块立方体（与窗口/任务栏图标同一设计）。

        中央图形从「凝」字改为等距立方体冰块：顶面三色横向渐变（HSL palette
        从 accent 派生），左右两面分别是 accent 主色和互补色系；accent 变
        则整套配色自动跟着走，和呼吸灯主题联动。
        """
        size = 64
        pm = self._make_dpr_pixmap(size, size)
        pm.fill(QColor(0, 0, 0, 0))
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        _draw_ice_cube(p, size, self._accent)
        p.end()
        return QIcon(pm)

    def _make_window_icon(self) -> QIcon:
        """窗口/任务栏/Alt-Tab 图标：与托盘同一冰块设计，多分辨率渲染。

        QIcon 内嵌 16..256 多档位图，Windows 任务栏（小/大）、Alt-Tab
        大缩略图、标题栏均按各自尺寸取用，accent 变则整体跟随换色。
        """
        icon = QIcon()
        accent = self._accent
        for sz in (16, 20, 24, 28, 32, 40, 48, 56, 64, 96, 128, 256):
            pm = QPixmap(sz, sz)
            pm.fill(QColor(0, 0, 0, 0))
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            _draw_ice_cube(p, sz, accent)
            p.end()
            icon.addPixmap(pm)
        return icon


    # -- minimize / restore fade (compositor-only opacity, 0 geometry cost) ----
    def _clear_fading(self) -> None:
        """淡入/淡出动画结束：解除 _fading 守卫，呼吸/暗化恢复驱动辉光透明度。"""
        self._fading = False

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
        # Avoid forcing opacity to 1.0 here: that toggles WS_EX_LAYERED on/off
        # on Windows and causes the first fade frame to hitch.  The animation's
        # explicit start/end values are sufficient; if the window is already
        # partially transparent we simply fade from there.
        # Temporarily clear the window mask: SetWindowRgn + layered opacity
        # transitions on Windows cause the first fade frames to hitch.
        self._push_mask_suppress()
        anim = QPropertyAnimation(self, b"windowOpacity")
        anim.setDuration(120)
        anim.setEasingCurve(QEasingCurve.Type.InQuad)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.finished.connect(self.hide)
        anim.finished.connect(self._pop_mask_suppress)
        anim.finished.connect(self._clear_fading)
        self._fading = True
        self._fade_anim = anim
        # 呼吸边框(_glow_border)独立顶层窗口，淡出主窗时不会自动跟随。改为与主窗
        # 同一条时钟「锁步淡出」：设 _fading=True 抑制呼吸定时器、stop 失焦淡出动画
        # (_breath_anim)，再让光效 120ms InQuad 从当前呼吸相位缓到 0，与主窗 120ms
        # InQuad 完全对齐——消除「光效先熄、本体还有残影」的错位。changeEvent 失焦分支
        # 见 _fading 守卫会跳过 _fade_breath_out，避免两套动画抢同一 windowOpacity。
        # 编辑蒙版(_editor_dim)已改为主窗子控件，随主窗淡出自动跟随。
        ba = getattr(self, "_breath_anim", None)
        if ba is not None:
            ba.stop()
        b = getattr(self, "_glow_border", None)
        if b is not None:
            # 淡出必须锁步到 0（不是当前呼吸相位 _breath_alpha，否则辉光在 120ms
            # 内几乎不淡、直到 anim.finished 才 hide，出现「主窗先消失、呼吸灯后消失」。
            # 注释原写「缓到 0」但调用处误传 _breath_alpha，此处修正为 0.0。
            self._animate_glow_to(0.0, 120, QEasingCurve.Type.InQuad)
        anim.finished.connect(
            lambda: [ov.hide() for ov in (
                getattr(self, "_glow_border", None),
                getattr(self, "_editor_dim", None)) if ov is not None])
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
        # 已经在前台且完全不透明：无需淡入，避免重复触发时闪烁。
        if self.isVisible() and self.windowOpacity() >= 0.99:
            self.raise_()
            self.activateWindow()
            self._keep_perfmon_on_top()
            return
        anim = getattr(self, "_fade_anim", None)
        if anim is not None:
            anim.stop()
        # Temporarily clear the window mask during the layered fade to avoid
        # the DWM hitch when SetWindowRgn is combined with opacity transitions.
        self._push_mask_suppress()
        self._fading = True
        self.setWindowOpacity(0.0)
        self.showNormal()
        self.raise_()
        self.activateWindow()
        # 恢复呼吸边框(_glow_border)独立窗口（淡出时已被隐藏）：其 opacity 回到当前
        # 相位(或关闭时的 1.0)；编辑蒙版(_editor_dim)已改为主窗子控件，随主窗淡入自动
        # 跟随，这里仅确保它重新显示。_fading 期间呼吸定时器不写 opacity，动画结束后
        # 由 _on_breath 续接。
        d = getattr(self, "_editor_dim", None)
        if d is not None:
            d.setWindowOpacity(1.0)
            d.show()
        # 呼吸边框(_glow_border)独立顶层窗口：与主窗同一条时钟「锁步淡入」。
        # 先以 opacity=0 显示，再 150ms OutQuad 缓动到当前呼吸相位(_breath_alpha)，
        # 与主窗 150ms OutQuad 完全对齐——消除「光效先满亮、本体后淡入」的错位。
        # _fading=True 期间呼吸定时器不写 opacity，动画独占；changeEvent 激活分支见
        # _fading 守卫会跳过 _fade_breath_in（避免两个动画抢同一 windowOpacity）。淡入
        # 结束后 _resume_breath_after_fade 解熄灭并恢复呼吸，从这里无缝续接。
        b = getattr(self, "_glow_border", None)
        if b is not None:
            b.setWindowOpacity(0.0)
            b.show()
            self._animate_glow_to(getattr(self, "_breath_alpha", 1.0),
                                  150, QEasingCurve.Type.OutQuad)
            ga = getattr(self, "_glow_fade_anim", None)
            if ga is not None:
                ga.finished.connect(self._resume_breath_after_fade)
        anim = QPropertyAnimation(self, b"windowOpacity")
        anim.setDuration(150)
        anim.setEasingCurve(QEasingCurve.Type.OutQuad)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.finished.connect(self._keep_perfmon_on_top)
        anim.finished.connect(self._pop_mask_suppress)
        anim.finished.connect(self._clear_fading)
        self._fade_anim = anim
        # 呼吸已画回主窗背景层，随主窗整体 opacity 从 0 缓动回 1；边框脉动由
        # _breath_alpha 驱动，无需单独动画辉光窗口。期间由 _fading 守卫。
        anim.start()
        # 从托盘唤醒到前台：事件驱动刷新电源计划（插拔电可能已切走系统计划）。
        QTimer.singleShot(0, self._refresh_powerplan)

    # -- 丙方案：失焦柔和渐隐 / 激活渐显（2026-08-29）------------------------
    # 呼吸边框 overlay 是独立顶层窗口，其 opacity 平时由 _on_breath 每 50ms 写
    # 一次。要「柔和」渐变就必须独占 opacity 的写入权：复用既有的 _fading 守卫
    # （_on_breath 里 `if not self._fading` 直接 return），动画期间呼吸驱动自动
    # 让路。动画结束后 pause/resume 驱动交接：渐隐完成 pause → 定时器停 → 灯稳定
    # 停在 0（不会被下一 tick 点亮）；渐显完成 resume → 呼吸从当前相位接续。
    # ⚠️ 刻意不使用 _fading 守卫来独占 opacity。_fading 属于主窗淡入淡出/暗化
    # 系统：boot() 期间事件循环尚未运行，动画 finished 永不触发，一旦在此置位
    # 就永久为真，会连带屏蔽 _dim_tick 使启动流程卡死（2026-08-29 实测踩坑）。
    # 改为「先 pause 呼吸驱动」——驱动停了就不会每 50ms 覆盖 opacity，动画自然
    # 独占，且完全不干扰主窗那套淡入淡出状态机。
    def _fade_breath_out(self) -> None:
        """失焦：把呼吸边框从当前亮度柔和淡到 0（熄灭），不是硬 hide。"""
        b = getattr(self, "_glow_border", None)
        if b is None or getattr(self, "_offscreen", False):
            return
        if getattr(self, "_breath_doused", False):
            return  # already doused
        self._breath_doused = True
        anim = getattr(self, "_breath_anim", None)
        if anim is not None:
            anim.stop()
        # 先停驱动：动画期间由动画独占 opacity（否则呼吸 tick 会不断覆盖）。
        if getattr(self, "_breath", None) is not None:
            self._breath.pause()
        anim = QPropertyAnimation(b, b"windowOpacity")
        anim.setDuration(120)
        anim.setEasingCurve(QEasingCurve.Type.InQuad)
        anim.setStartValue(b.windowOpacity())
        anim.setEndValue(0.0)
        # 动画结束后不 resume：灯稳定停在熄灭态（驱动停着，不会被重新点亮）。
        self._breath_anim = anim
        anim.start()

    def _fade_breath_in(self) -> None:
        """重新激活：呼吸灯先「快速冲到最亮」→ 停一下 → 从最亮处开始呼吸。

        2026-08-30（对应用户演示 html 的时序）：
          1. 冲亮：用 breath_flash_ms 从当前透明度升到**峰值**（不是淡回当前
             呼吸相位值），与失焦黑罩的淡出同时发生 —— 视觉上是「窗口亮起来的
             同时灯唰地到顶」。
          2. 停留：在最亮处停 breath_hold_ms，给一个「点亮」的确认感。
          3. 交接：把呼吸相位**钉到峰值**（π/2，sin=1 即最亮）后 resume，于是
             脉动从最亮处自然往下走，不会跳到随机相位。
        呼吸被全局禁用时不做冲亮后的脉动（set_breath_enabled(False) 语义为
        恒定满亮），但仍然执行冲亮，保证「开灯」的反馈感一致。
        """
        b = getattr(self, "_glow_border", None)
        if b is None or getattr(self, "_offscreen", False):
            return
        if not getattr(self, "_breath_doused", False):
            return  # already lit
        self._breath_doused = False
        anim = getattr(self, "_breath_anim", None)
        if anim is not None:
            anim.stop()
        flash = max(0, int(getattr(self, "_breath_flash_ms", 180) or 180))
        hold = max(0, int(getattr(self, "_breath_hold_ms", 120) or 120))
        # 峰值 = 呼吸最大亮度（呼吸关闭时恒为 1.0 满亮）。
        breath = getattr(self, "_breath", None)
        peak = 1.0
        if breath is not None:
            if getattr(breath, "_enabled", True):
                try:
                    peak = float(breath._max)
                except Exception:
                    peak = 1.0
            else:
                # 脉动被关闭：语义是恒定满亮（set_breath_enabled(False)），峰值取 1.0
                peak = getattr(self, "_breath_alpha", 1.0) or 1.0
        # 动画期间由动画独占 opacity（否则呼吸 tick 会不断覆盖）。
        if breath is not None:
            breath.pause()
        anim = QPropertyAnimation(b, b"windowOpacity")
        anim.setDuration(flash)
        # 先快后缓：前段迅速拉高形成「冲」的感觉，末段收住避免过冲刺眼。
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(b.windowOpacity())
        anim.setEndValue(peak)

        def _done() -> None:
            # 停留结束后把相位钉到峰值再交还驱动 —— 关键一步，保证「从最亮
            # 开始往下呼吸」而不是接上随机相位。
            def _hand_off() -> None:
                if breath is not None:
                    try:
                        breath.set_phase(math.pi / 2.0)
                    except Exception:
                        pass
                    breath.resume()

            if hold > 0:
                QTimer.singleShot(hold, _hand_off)
            else:
                _hand_off()

        anim.finished.connect(_done)
        self._breath_anim = anim
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
        """托盘『恢复』/ 单击托盘图标：淡入 HUD **并真正抢到键盘焦点**。

        光靠 ``_fade_in_from_tray`` 里的 ``raise_()`` + ``activateWindow()`` 在
        Windows 上不够——前台锁会把 ``activateWindow()`` 降级成「闪烁任务栏」
        （详见 ``_steal_foreground_best_effort`` 的说明），于是 HUD 出来了却
        吃不到键盘 / 手柄。这里在淡入动画跑完（150 ms 之后，此时窗口已完全
        可见、原生句柄已稳定）再补一次原生抢焦点，与全局热键唤出走同一套逻辑。
        """
        self._fade_in_from_tray()
        # ⚠️ PyQt6 无三参重载（见 _steal_foreground_best_effort 内的说明）。
        try:
            QTimer.singleShot(180, self._steal_foreground_best_effort)
        except Exception:  # noqa: BLE001
            pass

    def _on_tray_activated(self, reason: Any) -> None:
        # 单击(Trigger) / 双击(DoubleClick) 托盘图标都唤出 HUD 并抢焦点。
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
        _t0 = time.monotonic()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            p.end()
            return

        # Frosted dark fill — blit the cached full-window layer instead of
        # re-filling a rounded path every breath frame.
        # 注意：判断缓存失效必须用 _layer_stale()（显式 key 比对），
        # 不能写 cache.width() != w —— QPixmap.width() 返回设备像素，
        # 在 DPR=1.5 的掌机上恒不等于逻辑宽度，会导致每帧重建（见 _layer_key）。
        if self._layer_stale("_bg_cache", self._bg_layer_key(w, h)):
            self._rebuild_bg_cache()
        if self._bg_cache is not None and not self._bg_cache.isNull():
            p.drawPixmap(0, 0, self._bg_cache)
        else:
            path = QPainterPath()
            path.addRoundedRect(0, 0, w, h, 16, 16)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._bg)
            p.drawPath(path)

        # Card tray background — painted in the main (background) layer, BEHIND
        # the cards.  The breathing *static centre base light* is painted HERE in
        # the background layer (see _paint_breath_frame) — constant, zero per-frame
        # cost.  The breathing *border pulse* now lives on the independent
        # translucent overlay ``_GlowBorderOverlay`` (see _paint_breath_border),
        # raised above the unfocus scrim and decoupled from the unfocus dim — so it
        # keeps breathing (and stays visible) even while a game is in front, and
        # its repaint touches only that small DWM-composited surface, never the
        # main window or the 6 cards.  This is what keeps the handheld smooth
        # (the 0829 "非常卡顿" came from breathing on the main-window paint path).
        self._paint_tray(p, w, h)
        self._paint_breath_frame(p, w, h)
        p.end()
        profile.tick_main_paint((time.monotonic() - _t0) * 1000.0)

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
        # 呼吸边框(_glow_border)为独立顶层窗口，用屏幕坐标 self.geometry() 随主窗
        # 拖动精确重定位；编辑蒙版(_editor_dim)已改为主窗子控件，用窗口局部坐标
        # self.rect() 重定位（见 __init__ 注释）。
        b = getattr(self, "_glow_border", None)
        if b is not None:
            b.setGeometry(self.geometry())
            b.raise_()
        d = getattr(self, "_editor_dim", None)
        if d is not None:
            d.setGeometry(self.rect())
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
        window that is imperceptible.  No-op under offscreen test surfaces.

        The mask is temporarily cleared during fade-in/fade-out animations to
        avoid the DWM hitching that occurs when SetWindowRgn is combined with
        layered opacity transitions (see _push_mask / _pop_mask)."""
        if getattr(self, "_offscreen", False):
            return
        if getattr(self, "_mask_suppressed", False):
            self.clearMask()
            return
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        path = QPainterPath()
        path.addRoundedRect(0, 0, w, h, 16, 16)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def _push_mask_suppress(self) -> None:
        """Temporarily clear the window mask for the duration of a fade."""
        self._mask_suppressed = True
        self.clearMask()

    def _pop_mask_suppress(self) -> None:
        """Restore the rounded window mask after a fade completes."""
        self._mask_suppressed = False
        self._apply_window_mask()

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
        # 呼吸边框(_glow_border)为独立顶层窗口，需随主窗 resize 用屏幕坐标
        # self.geometry() 重新覆盖主窗并置顶；编辑蒙版(_editor_dim)已改为主窗子控件，
        # 用窗口局部坐标 self.rect() 重定位并 lower() 到 _body 之下；失焦蒙版(_scrim
        # 为主窗子控件)沿用窗口局部坐标 self.rect()。
        if not getattr(self, "_offscreen", False):
            self._glow_timer.start(80)
            self._apply_window_mask()
            # 呼吸边框(_glow_border)独立顶层窗口：屏幕坐标覆盖主窗，置顶保持 z 序。
            b = getattr(self, "_glow_border", None)
            if b is not None:
                b.setGeometry(self.geometry())
                b.raise_()
            d = getattr(self, "_editor_dim", None)
            if d is not None:
                d.setGeometry(self.rect())
                d.lower()
            # 失焦蒙版(_scrim 为主窗子控件)：沿用窗口局部坐标 self.rect()。
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
        # 重建期不采集/下发刷新任务：切页会拆建面板，此刻抓到的面板引用可能在任务
        # 于后台线程执行时已被销毁 → 原生崩溃。守卫置位时直接跳过，下一个 2s tick
        # 自然重试（重建结束后守卫复位）。
        if getattr(getattr(self, "_controller", None), "_rebuilding", False):
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

            # 开机自动应用持久化固定档（降压 -30mV 状态按钮）：无需用户操作，
            # 启动即把 CO 钉到 pinned_mv 一次（dry_run 只观察不写）。与自动降压
            # 互斥——pinned_mv 非零时强制 enabled=False。
            if cfg.pinned_mv != 0:
                cfg.enabled = False
                if not cfg.dry_run:
                    try:
                        self._uv_controller.backend.apply_coall(cfg.pinned_mv)
                        self._uv_controller.last_applied_mv = cfg.pinned_mv
                        self._uv_last["state"] = "pinned"
                        self._uv_last["mv"] = cfg.pinned_mv
                    except Exception:
                        pass

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
            # 互斥：开「自动降压」即清掉「降压 -30mV」持久状态（二者不可并存，
            # 否则每 tick 状态机会把 -30 覆盖掉）。
            if value:
                cfg.pinned_mv = 0
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
        # 一次性固定档（-20）不算持久状态：清掉「降压 -30mV」持久标志，
        # 否则休眠恢复仍会重钉 -30、与本次 -20 选择冲突。
        cfg.pinned_mv = 0
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

    def undervolt_set_pinned(self, mv: int = -30) -> dict:
        """「降压 -30mV」状态按钮激活：钉固定档 + 持久化（开机/恢复自动重钉）。

        - 关掉自动控制器（enabled=False，与自动降压互斥）。
        - 立即把电压钉在 mv（默认 -30），并写回 pinned_mv 持久态。
        - dry_run 时 backend 只记日志不写电压，但 pinned_mv 仍持久化，逻辑一致。

        返回最新 flags 供 UI 同步。
        """
        ctrl = getattr(self, "_uv_controller", None)
        cfg = ctrl.config if ctrl is not None else UndervoltConfig.load(
            self._undervolt_cfg_path())
        cfg.enabled = False
        cfg.pinned_mv = int(mv)
        if ctrl is not None:
            try:
                ctrl.backend.apply_coall(mv)
            except Exception:
                pass
            ctrl.last_applied_mv = mv
            self._uv_last["state"] = "pinned"
            self._uv_last["mv"] = mv
        cfg.save(self._undervolt_cfg_path())
        return self.undervolt_get_flags()

    def undervolt_clear_pinned(self) -> dict:
        """「降压 -30mV」状态按钮关闭：立即归零 + 清持久态。

        - 关掉自动控制器（保持 enabled=False）。
        - 把电压复位到 0（释放固定档），并清掉 pinned_mv 持久态。

        返回最新 flags 供 UI 同步。
        """
        ctrl = getattr(self, "_uv_controller", None)
        cfg = ctrl.config if ctrl is not None else UndervoltConfig.load(
            self._undervolt_cfg_path())
        cfg.enabled = False
        cfg.pinned_mv = 0
        if ctrl is not None:
            try:
                ctrl.backend.reset()
            except Exception:
                pass
            ctrl.last_applied_mv = 0
            self._uv_last["state"] = "pinned"
            self._uv_last["mv"] = 0
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
                "pinned_mv": int(cfg.pinned_mv),
                "state": state or "idle",
                "mv": int(mv),
            }
        cfg = UndervoltConfig.load(self._undervolt_cfg_path())
        return {
            "enabled": bool(cfg.enabled),
            "heavy_enabled": bool(cfg.heavy_enabled),
            "heavy_validated": bool(cfg.heavy_validated),
            "pinned_mv": int(cfg.pinned_mv),
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
          gpu_freq_target: 静态锁频目标（MHz，仅记录用；GPU 实际 vs 锁定
                            频率冲突告警已取消，不再参与判定）
          tdp_set        : TDP 滑块设定（W）
          fps_actual     : 实测帧率（HWiNFO 呈现帧率(avg) 优先，RTSS 全局兜底）
          fps_source     : 帧率来源 "HWiNFO" / "RTSS" / None
          fps_target     : RTSS 锁帧目标（仅 lock_enabled 时有）
          gpu_dev_alert  : 已停用（实际频率是芯片降功耗动态激活频率，锁定
                            频率只是幅度上限，二者本就不要求一致）
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
            out["fps_hwinfo"] = tel.fps_hwinfo
        except Exception:
            pass
        # 6b) 目标值（power 区块设定）。GPU 实际 vs 锁定频率冲突告警已取消，
        #     故只取 TDP 设定用于展示。
        try:
            power = self._app.blocks.get("power")
            if power is not None:
                out["tdp_set"] = int(getattr(power, "tdp", 0) or 0)
        except Exception:
            pass
        # 6c) 实测帧率：HWiNFO「呈现帧率(avg)」优先（全局、不依赖前台 PID，
        #     规避 HUD/NBJ 置顶时误读 NBJ 自身帧率）；RTSS 全局帧率兜底。
        #     旧实现只取 RTSS 按前台 PID 读数，RTSS 未跑/未挂钩即 None → 永远「—」；
        #     而 HWiNFO 呈现帧率虽已被读入 tel.fps_hwinfo 却从未接出（孤儿字段）。
        try:
            fps_val = None
            fps_src = None
            if out.get("tel_fresh") and out.get("fps_hwinfo") is not None:
                fps_val = out["fps_hwinfo"]
                fps_src = "HWiNFO"
            if fps_val is None:
                try:
                    from ..platform.rtss_smi import read_fps_global
                    rv = read_fps_global()
                    if rv is not None:
                        fps_val = rv
                        fps_src = "RTSS"
                except Exception:
                    pass
            out["fps_actual"] = fps_val
            out["fps_source"] = fps_src
        except Exception:
            out["fps_actual"] = None
            out["fps_source"] = None
        try:
            rtss = self._app.blocks.get("rtss")
            if rtss is not None and getattr(rtss, "lock_enabled", False):
                out["fps_target"] = int(getattr(rtss, "fps", 0) or 0)
        except Exception:
            pass
        # 7) 偏差告警状态（连续 3 tick 超阈值 → 面板黄字 + toast；HWiNFO 轮询
        #    3s，约 9s 才确认一次，避免瞬时抖动误报）。
        #    注：GPU 频率不再做「实际 vs 锁定/目标」冲突告警——实际频率是 GPU
        #    实际激活频率（芯片为降功耗而动态变化），锁定频率只是给频率变化幅度
        #    设的上限，二者本就不要求一致，告警属于误报，故取消该监测与提醒。
        try:
            dev = self.__dict__.setdefault("_perfmon_dev", {"gpu": 0, "fps": 0})
            dev["gpu"] = 0
            out["gpu_dev_alert"] = False
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

    # -- per-page panel container cache ---------------------------------
    # Each top-level page owns a QWidget "container" holding that page's panels
    # (+ trailing stretch) in its own layout.  Switching pages only swaps which
    # container is in ``inner_layout`` (removeWidget old + addWidget new) — the
    # panels are NEVER destroyed/recreated, so a page switch is now a cheap
    # layout swap instead of a full teardown+rebuild (the old source of the
    # "切换页面极度卡顿" complaint).  A *full* rebuild (settings return, theme
    # change, tile edit) still discards the cache via _teardown_all_pages().
    def _build_page_container(self, idx: int) -> None:
        """Lazily build (and cache) the QWidget container for page ``idx``.

        The container holds that page's panels in its own QVBoxLayout (mimicking
        the old direct-to-inner_layout layout, including the trailing stretch),
        so swapping containers preserves the exact card spacing/alignment.
        """
        if idx in self._page_containers:
            return
        app = self._app
        pages = app.render_pages()
        if not pages or idx < 0 or idx >= len(pages):
            return
        container = QWidget(self.inner)
        cl = QVBoxLayout(container)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(self.inner_layout.spacing())
        panels: list = []
        for token in pages[idx]:
            try:
                panel = self._make_panel_for_token(token)
            except Exception:
                # A single card's _setup throwing (bad tile config, transient
                # EC/hardware probe, etc.) must never abort the whole page
                # build — that would leave _controller still pointing at the
                # just-deleted panels and crash the next gamepad poll.
                continue
            if panel is None:
                continue
            self._controller.register_panel(panel)
            # 兜底：允许卡片在窗口宽度不足时被「压」到窗口宽度（而非反过来把窗口
            # 撑宽）。卡片内部超宽文字应自行 word-wrap；若某控件不可折行，最坏情况
            # 是卡片内局部裁切，而不会再把整个窗口顶出屏幕。
            panel.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            cl.addWidget(panel)
            panels.append(panel)
        # Absorb any leftover vertical space so cards keep their natural height
        # instead of being stretched to fill the scroll viewport.
        cl.addStretch(1)
        container.setVisible(False)
        self._page_containers[idx] = container
        self._page_panels[idx] = panels

    def _detach_containers_from_layout(self) -> None:
        """Remove every page container from ``inner_layout`` WITHOUT destroying
        it (so it can be re-added on the next page switch)."""
        # If a slide is in progress, the containers live inside the transient
        # _page_strip; tear that down first so containers return to self.inner.
        if self._page_strip is not None:
            self._stop_slide()
        while self.inner_layout.count():
            item = self.inner_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                self.inner_layout.removeWidget(w)
                w.setParent(None)

    def _stop_slide(self) -> None:
        """Abort an in-progress page slide and return containers to self.inner.

        Does NOT change the logical current page; callers decide whether to
        finish at the old or new page.
        """
        if self._slide_anim is not None:
            try:
                self._slide_anim.stop()
            except Exception:
                pass
            self._slide_anim = None
        if self._page_strip is not None:
            # Reparent every live container back to self.inner before the strip
            # is destroyed, otherwise deleteLater would also delete the panels.
            for i in range(self._page_strip_layout.count()):
                item = self._page_strip_layout.itemAt(i)
                if item is not None:
                    w = item.widget()
                    if w is not None:
                        w.setParent(self.inner)
                        w.setVisible(False)
            self._page_strip.deleteLater()
            self._page_strip = None
            self._page_strip_layout = None
        self._sliding = False

    def _teardown_all_pages(self) -> None:
        """Destroy every cached page container + release its panels' resources.

        Used by a *full* rebuild (settings return / theme change / tile edit),
        never by a plain page switch.
        """
        # 全量重建会销毁所有面板，未完成的转场快照随之失效，先取消掉。
        self._cancel_page_transition()
        # Detach first so the layout no longer references widgets we are about
        # to deleteLater (otherwise the pending-deletion widgets linger in the
        # layout and the next addWidget stacks a ghost under the new page).
        self._detach_containers_from_layout()
        for idx, container in self._page_containers.items():
            for p in self._page_panels.get(idx, []):
                rel = getattr(p, "release", None)
                if callable(rel):
                    try:
                        rel()
                    except Exception:
                        pass
            try:
                container.deleteLater()
            except Exception:
                pass
        self._page_containers.clear()
        self._page_panels.clear()
        # NOTE: mutate in place — _SyncBridge holds a reference to this list.
        self.panels.clear()
        self._current_page_idx = -1

    def _finish_slide(self, new_idx: int, new_c: Any) -> None:
        """Clean up after a page-slide animation completes."""
        self._slide_anim = None
        if self._page_strip is not None:
            # Reparent every container still in the strip back to self.inner
            # before deleting the strip, so cached pages survive.
            for i in range(self._page_strip_layout.count()):
                item = self._page_strip_layout.itemAt(i)
                if item is not None:
                    w = item.widget()
                    if w is not None:
                        w.setParent(self.inner)
                        w.setVisible(False)
            self._page_strip.deleteLater()
            self._page_strip = None
            self._page_strip_layout = None
        self._sliding = False
        self.inner_layout.addWidget(new_c)
        new_c.setVisible(True)
        self.panels.clear()
        self.panels.extend(self._page_panels[new_idx])
        self._current_page_idx = new_idx
        QTimer.singleShot(0, lambda: self._scroll.verticalScrollBar().setValue(0))
        self._controller.rebind()
        try:
            self.apply_card_fill(self._card_fill_color.name(),
                                 self._card_fill_alpha)
        except Exception:
            pass
        self._did_rebind = True

    def _show_page(self, idx: int, editor_all: bool = False) -> None:
        """Make page ``idx`` visible.  Cheap container swap — no teardown."""
        app = self._app
        pages = app.render_pages()
        if not pages:
            return
        if editor_all:
            # Build + show every page's container (HUD hidden behind overlay).
            for i in range(len(pages)):
                self._build_page_container(i)
            self._detach_containers_from_layout()
            all_panels: list = []
            for i in range(len(pages)):
                c = self._page_containers[i]
                self.inner_layout.addWidget(c)
                c.setVisible(True)
                all_panels.extend(self._page_panels[i])
            # Mutate in place (see _teardown_all_pages note).
            self.panels.clear()
            self.panels.extend(all_panels)
            self._current_page_idx = max(0, min(idx, len(pages) - 1))
            self._sliding = False
        else:
            old_idx = self._current_page_idx
            # 记住这一页的容器是不是本次才新建的：只有新建的面板才需要下发
            # 卡片填充色。缓存容器里 panels 早已带着正确填充，重复下发会白跑
            # 一遍循环，并且 apply_card_fill 末尾的整窗 self.update() 在方案 A
            # 下（脏区域=整窗）代价很高——这正是「切页卡顿」的一个来源。
            was_cached = idx in self._page_containers
            self._build_page_container(idx)
            old = self._page_containers.get(old_idx)
            new_c = self._page_containers[idx]

            # Decide whether to animate.  Skip if:
            #   - a slide is already running,
            #   - this is the first page ever shown,
            #   - the target page is the same as current,
            #   - or either container isn't layout-sized yet (geometry 0x0).
            # NOTE: sliding animation is temporarily disabled on handheld
            # builds because the free-floating page strip can end up with a
            # clipped/empty parent geometry on some DPR/scaling combos,
            # making the page invisible and blocking input.  The infrastructure
            # (_page_strip / _finish_slide / _stop_slide) is kept so we can
            # re-enable once a more robust layout-integrated approach is ready.
            can_slide = False
            if can_slide:
                # Remove old from inner_layout but keep its geometry/parent.
                self.inner_layout.removeWidget(old)
                old.setVisible(True)
                new_c.setVisible(True)

                # Build a transient horizontal strip that holds both pages.
                # It must be fully transparent and not intercept mouse events,
                # otherwise it would paint a visible background over the cards
                # and block all button interaction on the HUD.
                strip = QWidget(self.inner)
                strip.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
                strip.setAutoFillBackground(False)
                strip.setStyleSheet("background: transparent; border: none;")
                strip.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
                lay = QHBoxLayout(strip)
                lay.setContentsMargins(0, 0, 0, 0)
                lay.setSpacing(0)
                geo = old.geometry()
                direction = 1 if idx > old_idx else -1
                if direction > 0:
                    # New page enters from the right.
                    lay.addWidget(old)
                    lay.addWidget(new_c)
                    start_x = geo.x() - geo.width()
                    end_x = geo.x()
                else:
                    # New page enters from the left.
                    lay.addWidget(new_c)
                    lay.addWidget(old)
                    start_x = geo.x()
                    end_x = geo.x() + geo.width()
                strip.setGeometry(start_x, geo.y(), geo.width() * 2, geo.height())
                self._page_strip = strip
                self._page_strip_layout = lay
                self._sliding = True

                anim = QPropertyAnimation(strip, b"pos")
                anim.setDuration(100)
                anim.setEasingCurve(QEasingCurve.Type.OutQuad)
                anim.setStartValue(QPoint(start_x, geo.y()))
                anim.setEndValue(QPoint(end_x, geo.y()))
                anim.finished.connect(lambda: self._finish_slide(idx, new_c))
                self._slide_anim = anim
                anim.start()
                # Safety net: if the animation's finished signal is somehow
                # missed (e.g. the object is destroyed early), force cleanup.
                # Guard against a race with a hard-cut mid-slide: if the page
                # has already changed, the strip has been torn down elsewhere.
                def _force_finish(target_idx: int, strip_ref: Any) -> None:
                    if (self._page_strip is strip_ref
                            and self._current_page_idx != target_idx):
                        self._finish_slide(target_idx, new_c)
                QTimer.singleShot(250, lambda: _force_finish(idx, strip))
            else:
                # Hard cut: used for first paint, same-page, or mid-slide.
                if self._page_strip is not None:
                    self._stop_slide()
                if old is not None and old_idx != idx:
                    self.inner_layout.removeWidget(old)
                    old.setVisible(False)
                if new_c.parent() is not self.inner:
                    new_c.setParent(self.inner)
                self.inner_layout.addWidget(new_c)
                new_c.setVisible(True)
                self.panels.clear()
                self.panels.extend(self._page_panels[idx])
                self._current_page_idx = idx
                QTimer.singleShot(0, lambda: self._scroll.verticalScrollBar().setValue(0))
                self._controller.rebind()
                if not was_cached:
                    try:
                        self.apply_card_fill(self._card_fill_color.name(),
                                             self._card_fill_alpha)
                    except Exception:
                        pass
                self._did_rebind = True

    def _apply_panels(self, full: bool) -> None:
        """Build / swap the visible page(s).

        ``full`` forces a cache discard (real rebuild); otherwise a plain page
        switch just swaps the cached containers — no destroy/recreate.
        """
        editor_all = bool(getattr(self, "_editor_all_pages", False))
        app = self._app
        nav = app.nav
        pages = app.render_pages()
        idx = nav.current_page if nav is not None else 0
        idx = max(0, min(idx, max(0, len(pages) - 1))) if pages else 0
        if full or not self._page_containers or self._current_page_idx == -1:
            # Real rebuild: discard the cache first.  Stop any slide so the old
            # containers don't get reparented into a deleted strip.
            if self._sliding:
                self._stop_slide()
            self._teardown_all_pages()
            if editor_all:
                self._show_page(idx, editor_all=True)
            else:
                self._build_page_container(idx)
                self._show_page(idx)
            # 全量重建后面板是全新对象（单卡填充取 BlockPanel 默认值，而非当前
            # 已改的值）。但 _show_page 里的 apply_card_fill 重播被 `if not
            # was_cached` 跳过了——因为本分支先 _build_page_container 把容器塞进
            # _page_containers，_show_page 再算 was_cached 时它已是 True，于是
            # 新面板永远拿不到「主题与蒙版」里刚改的卡片颜色/透明度。结果：改完
            # 保存返回主页面，卡片仍显示旧/默认填充，必须重启 nbj 才正常（重启
            # 走首次 _show_page，was_cached=False 会下发）。这里在重建后强制重播，
            # 保证新建面板立即携带当前填充。窗口级属性（托盘/页面底色，由窗口
            # 绘制且已 live 更新）不受影响，无需在此处理。
            try:
                self.apply_card_fill(self._card_fill_color.name(),
                                     self._card_fill_alpha)
            except Exception:
                pass
        else:
            # Lightweight page switch: cache swap only.
            self._show_page(idx, editor_all=editor_all)

    def _build_panels(self) -> None:
        """Backward-compatible alias: full (re)build of the visible page(s).

        Replaced by the per-page container cache (see _apply_panels /
        _show_page); kept so the startup call site stays unchanged.
        """
        self._apply_panels(full=True)

    def _make_panel_for_token(self, token: str) -> Any:
        """Build (but do NOT register/layout) one panel for an expanded-order
        token and return it.

        ``tile#N`` → the Nth extra tile block panel; any other token → the
        base block panel.  Mirrors the PS1 where each ``New-TileBlockPanel``
        creates a separate ``RoundedPanel`` in the scroll container.

        The panel's ``controller`` is wired to the live controller so its
        ``_setup``/``sync`` populate correctly, but — unlike the panels built by
        :meth:`_build_page_container` — it is NOT registered with the controller
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
        if base == "ls":
            # 供 LSPanel 头部的「⚙」/「空路径启动」直接跳进 LS 设置编辑项。
            panel.open_ls_settings = lambda: self._open_editor_ls()
        return panel

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
        _prof = self._prof
        _prof.reset()
        _prof.mark("start")
        if reload_tiles:
            # Refresh tile block's internal state from config (new extra blocks
            # may have been added since boot).
            tile_instance = self._app.blocks.get("tile")
            if tile_instance and hasattr(tile_instance, "load_config"):
                try:
                    tile_instance.load_config()
                except Exception:
                    pass
        _prof.mark("after_reload")
        # Drop the old page's panels.  The actual widget/layout teardown (and
        # the trailing stretch spacer) is handled inside _apply_panels() via
        # _teardown_all_pages() (full rebuild) or skipped entirely on a plain
        # page switch (cached container swap) — so we only reset bookkeeping here.
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
                # full=True forces a cache discard (real rebuild); False makes a
                # plain page switch a cheap cached container swap — no destroy.
                # _did_rebind is set True by _show_page() once it has rebound the
                # controller to the freshly-shown panels; the finally block below
                # only rebinds as a backstop if _show_page never reached its
                # rebind (e.g. a card _setup threw mid-build) — so we never
                # double-rebind (which would rebuild the nav-item list twice).
                self._did_rebind = False
                self._apply_panels(full=(restyle or reload_tiles or heavy))
            finally:
                # Rebind backstop: only if _show_page() did NOT already rebind.
                # _show_page always rebinds to the freshly-shown panels, so on the
                # normal path this is a no-op; it exists only to keep the
                # controller consistent (pointing at live panels, not deleteLater'd
                # ones) if the build threw before the in-_show_page rebind.
                if not getattr(self, "_did_rebind", False):
                    self._controller.rebind()
                self.setUpdatesEnabled(True)
        finally:
            if _ctrl is not None:
                try:
                    if getattr(self, "_transition_active", False):
                        # 页面转场进行中（_arm_page_transition 已置位）：
                        # 守卫保持置位 + 手柄轮询保持暂停，由
                        # _release_transition_guard() 在动画结束后统一复位。
                        # 否则 30ms 轮询会在转场期间去导航一张用户看不见的
                        # 页面（用户看到的是旧页快照，按 RB 却作用在新页上）。
                        self._rebuild_deferred_ctrl = _ctrl
                        self._rebuild_deferred_gp = _was_gp_active
                    else:
                        _ctrl.set_rebuilding(False)
                        if _was_gp_active:
                            _ctrl.start_gamepad()
                except Exception:
                    pass
        _prof.mark("after_build")
        # Apply the new theme background (in case it changed).
        self._bg = self._theme_bg()
        if restyle:
            # Re-apply the active button style (L2) + any bound L1 presets to
            # the freshly created buttons — mirrors PS1 Set-GlobalButtonStyle
            # after a layout rebuild.  Excludes the edit overlay subtree so
            # editor controls are never restyled.
            self._apply_button_styles()
        _prof.mark("before_update")
        if restyle or heavy or reload_tiles:
            # 全量重建：按钮样式 / 呼吸色 / 磁贴配置可能全窗变化，需要整窗重绘。
            self.update()
        else:
            # 普通切页：只有滚动区内容变了。只重绘滚动内容部件，跳过静态的
            # bg/glow/tray 整窗 blit（这些不会因切页改变），等价于把重绘范围
            # 从「整窗」收敛到「当前页卡片」，切页即时感明显更好。
            scroll = getattr(self, "_scroll", None)
            if scroll is not None:
                wgt = getattr(scroll, "widget", lambda: None)()
                if wgt is not None:
                    wgt.update()
                else:
                    scroll.update()
            else:
                self.update()
        _prof.mark("after_update")
        self._refresh_page_bar()
        _prof.mark("after_pagebar")
        self._commit_profile(restyle, heavy, reload_tiles)
        if heavy:
            QTimer.singleShot(0, self._do_refresh)
        else:
            # Cheap repaint from current block state — no subprocess spawn, no
            # full restyle.  This is what a page switch uses.
            QTimer.singleShot(0, self._bridge.sync_all)

    def _commit_profile(self, restyle: bool, heavy: bool,
                        reload_tiles: bool) -> None:
        """Flush the current switch's timings to the CSV.

        No-op unless ``NBJ_PROFILE=1`` was set at startup, so production runs
        pay zero cost.  ``n_panels`` is the live panel count of the *new* page.
        """
        prof = self._prof
        if not getattr(prof, "enabled", False):
            return
        self._prof_seq += 1
        n = len(getattr(self, "panels", []) or [])
        prof.commit(self._prof_seq, n, heavy, restyle, reload_tiles)

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
        self._page_bar_buttons = []

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
            self._page_bar_buttons.append(b)
        lay.addStretch(1)

        # Phase 0：页码滑动指示条。绝对定位在 bar 内、**不进布局**，
        # 因此不会触发 relayout，也不占页码条高度（贴按钮底边内侧 3px）。
        # 旧指示条是旧 bar 的子控件，会随 bar 的 deleteLater 一起销毁。
        self._page_indicator = None
        if total > 1:
            try:
                self._page_indicator = PageIndicator(bar)
                self._page_indicator.sync(self._page_bar_buttons, cur)
            except Exception:
                self._page_indicator = None

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

    def _ensure_page_bar(self) -> None:
        """Build the page-bar widget once; only rebuild when the PAGE COUNT changes.

        A plain page switch keeps the existing bar and just moves the highlight
        (see _highlight_current_page) — no widget destruction/creation, no
        QHBoxLayout churn, no second repaint.  This removes the per-switch
        page-bar rebuild that was the dominant residual cost after the panel
        container-cache refactor.
        """
        app = self._app
        pages = app.render_pages()
        total = max(1, len(pages))
        if (getattr(self, "_page_bar", None) is not None
                and len(getattr(self, "_page_bar_buttons", []) or []) == total):
            return
        self._build_page_bar()

    def _highlight_current_page(self) -> None:
        """Restyle only the current page button (gold); cheap, no layout churn."""
        nav = self._app.nav
        pages = self._app.render_pages()
        total = max(1, len(pages))
        cur = nav.current_page if nav is not None else 0
        cur = max(0, min(cur, total - 1))
        cur_style, normal_style = self._page_bar_styles()
        for i, b in enumerate(getattr(self, "_page_bar_buttons", []) or []):
            try:
                b.setStyleSheet(cur_style if i == cur else normal_style)
            except Exception:
                pass
        # Phase 0：让指示条滑到新页按钮下方。页码条刚重建时 sync() 已经把它
        # 摆在正确位置，此处 slide_to 会因位置相同而直接 no-op。
        ind = getattr(self, "_page_indicator", None)
        if ind is not None:
            try:
                ind.slide_to(cur)
            except Exception:
                pass

    def _refresh_page_bar(self) -> None:
        """Called on every (re)build: ensure the bar exists (rebuild only if the
        page count changed) and move the current-page highlight.

        Replaces the old unconditional ``_build_page_bar()`` call inside
        ``rebuild()`` so a plain page switch no longer destroys + recreates the
        whole page-bar widget tree.
        """
        self._ensure_page_bar()
        self._highlight_current_page()
        self._apply_page_bar_visibility()

    # -- 页面转场（Phase 0 指示条 + Phase 1 快照位图）----------------------
    # 设计见 ui/page_transition.py 模块文档。铁律：转场不让真实 widget 动，
    # 只在覆盖层上画两张快照；任何异常一律降级为现有硬切，绝不卡死。

    def _transition_mode(self) -> str:
        """当前转场模式："slide" / "fade" / "none"。读配置失败回落 slide。"""
        try:
            cfg = self._app.config_manager.config
            m = str(getattr(cfg, "page_transition", "slide") or "slide").strip().lower()
        except Exception:
            return "slide"
        return m if m in TRANSITION_MODES else "slide"

    def _transition_ms(self) -> int:
        """转场时长（ms），clamp 到 [0, 600]。0 等同于关闭动画。"""
        try:
            cfg = self._app.config_manager.config
            ms = int(getattr(cfg, "transition_ms", DEFAULT_TRANSITION_MS))
        except Exception:
            return DEFAULT_TRANSITION_MS
        return max(0, min(600, ms))

    def _page_viewport_rect(self) -> Any:
        """滚动视口在主窗坐标系中的矩形；取不到返回 None（降级为硬切）。"""
        scroll = getattr(self, "_scroll", None)
        if scroll is None:
            return None
        try:
            vp = scroll.viewport()
        except Exception:
            return None
        if vp is None or vp.width() <= 0 or vp.height() <= 0:
            return None
        try:
            tl = vp.mapTo(self, QPoint(0, 0))
        except Exception:
            return None
        return QRect(tl.x(), tl.y(), vp.width(), vp.height())

    def _grab_page_snapshot(self) -> Any:
        """抓取「用户此刻看到的页面区域」位图。

        用**主窗口**的 ``grab(rect)`` 而不是 ``viewport.grab()``：页面底色、
        卡片区背景、托盘描边都是主窗口 paintEvent 画的，视口自身并不画背景，
        直接抓视口会得到一张透明底的图，转场时会透出下面的真实页面（重影）。
        """
        rect = self._page_viewport_rect()
        if rect is None:
            return None
        try:
            pm = self.grab(rect)
        except Exception:
            return None
        return pm if (pm is not None and not pm.isNull()) else None

    def _arm_page_transition(self) -> None:
        """切页入口：抓旧页快照并置位转场意图（在旧页仍完整可见时调用）。

        必须在 ``_on_page_switched`` 的最前面调用——此时旧页还没被 rebuild
        换掉，布局稳定，抓到的快照是用户真正看到的内容。
        """
        self._pending_old_snapshot = None
        self._pending_transition_dir = 1
        # 按钮编辑器全页模式不转场（HUD 不重建，抓快照没有意义）。
        if getattr(self, "_editor_all_pages", False):
            return
        if self._transition_mode() == "none" or self._transition_ms() <= 0:
            return
        try:
            nav = getattr(self._app, "nav", None)
            new_idx = int(nav.current_page) if nav is not None else 0
        except Exception:
            return
        old_idx = int(getattr(self, "_current_page_idx", -1))
        if old_idx < 0 or new_idx == old_idx:
            return  # 首次显示 / 同页：不做转场
        try:
            total = max(1, len(self._app.render_pages()))
        except Exception:
            total = 1
        if total <= 1:
            return
        # 环绕（末页 → 首页）时走「近路」，滑动方向与用户按键方向一致。
        delta = new_idx - old_idx
        if abs(delta) > total // 2:
            delta = -delta
        self._pending_transition_dir = 1 if delta > 0 else -1
        pm = self._grab_page_snapshot()
        if pm is None:
            return
        self._pending_old_snapshot = pm
        # --- 立刻盖上「旧页盖罩」------------------------------------------
        # 必须在 rebuild 之前铺上旧页快照：rebuild 与「抓新页快照 + 起动画」
        # 分处两个事件循环 tick（中间夹着 singleShot(0)），那一帧窗口会把新页
        # 真身合成出来 —— 就是「先跳到第二页、再瞬回第一页、然后才播动画」。
        # 盖罩画面 = 旧页，与当前所见逐像素一致，因此盖上时用户无感。
        self._show_transition_cover(pm)
        # 置位「转场意图」。rebuild() 的 finally 看到它会把守卫/手柄的复位
        # 延期到动画结束；下面这道保底定时器保证即使动画回调丢失也不会
        # 让守卫永久置位（否则手柄彻底失灵）。
        self._transition_active = True
        QTimer.singleShot(self._transition_ms() + SAFETY_MARGIN_MS,
                          self._page_transition_safety)

    def _show_transition_cover(self, old_pm: Any) -> None:
        """用旧页快照盖住页面可视区（转场第一阶段）。

        只画旧页、进度恒为 0，画面与用户当前所见完全一致，所以盖上时无感；
        它的作用是让随后的 rebuild（真实换页）发生在盖罩之下，不被合成出来。
        """
        try:
            mode = self._transition_mode()
            rect = self._page_viewport_rect()
            if mode == "none" or rect is None:
                return
            self._teardown_veil()
            veil = TransitionVeil(self, mode, old_pm, None,
                                  getattr(self, "_pending_transition_dir", 1))
            veil.setGeometry(rect)
            veil.raise_()
            veil.show()
            # 强制立即绘制一次：show() 只是投递 UpdateRequest，而 singleShot(0)
            # 的重建回调与它同属普通优先级事件，谁先被处理没有保证。若盖罩
            # 晚于 rebuild 那一帧合成，新页真身仍会闪一帧出来。
            veil.repaint()
            self._page_veil = veil
        except Exception:
            pass

    def _start_page_transition(self) -> None:
        """抓新页快照 + 起动画。任何异常都静默降级为硬切（清场 + 复位守卫）。

        时序关键点：盖罩（只含旧页快照的 veil）在 ``_arm_page_transition`` 里
        就已经盖上。这里做的是 ① 临时 **隐藏** 盖罩 → ② 抓新页快照 → ③ 把新页
        快照塞回同一个 veil 并重新显示 → ④ 起动画。①②③ 在同一个事件循环回调
        内同步完成，中间不经过事件循环，因此不会被合成出「新页真身」的一帧。
        """
        old_pm = getattr(self, "_pending_old_snapshot", None)
        self._pending_old_snapshot = None
        started = False
        try:
            if old_pm is None or old_pm.isNull():
                raise RuntimeError("no old snapshot")
            mode = self._transition_mode()
            rect = self._page_viewport_rect()
            if mode == "none" or rect is None or not self.isVisible():
                raise RuntimeError("transition disabled")
            # 盖罩是 _arm_page_transition 建的。只隐藏、不销毁：抓完快照还要
            # 复用同一个 veil，销毁会让新页真身露出至少一帧。
            veil = getattr(self, "_page_veil", None)
            if veil is not None:
                veil.hide()
            # 强制布局先落定：_show_page 只是 addWidget，布局要等事件循环才
            # 重算；不激活就抓可能得到尚未布局（0 高度）的页面。
            try:
                lay = getattr(self, "inner_layout", None)
                if lay is not None:
                    lay.activate()
            except Exception:
                pass
            new_pm = self._grab_page_snapshot()
            if new_pm is None:
                raise RuntimeError("no new snapshot")
            if veil is None:
                # 兜底：盖罩缺失（例如被全量重建 cancel 掉）时现建一个。
                veil = TransitionVeil(self, mode, old_pm, new_pm,
                                      getattr(self, "_pending_transition_dir", 1))
                veil.setGeometry(rect)
                self._page_veil = veil
            else:
                veil.set_new_pixmap(new_pm)
            veil.raise_()
            veil.show()
            if self._page_transition is None:
                self._page_transition = PageTransition(self)
                self._page_transition.finished.connect(
                    self._on_page_transition_finished)
            self._page_transition.start(veil, self._transition_ms())
            started = True
        except Exception:
            started = False
        if not started:
            # 走到这里说明转场没能起来（或中途抛异常）：必须拆掉盖罩 + 复位
            # 守卫。少了这一步，页面会被旧页快照**永久**盖住。
            self._finish_page_transition()

    def _teardown_veil(self) -> None:
        """销毁覆盖层并立刻释放两张快照（每张约 3MB）。"""
        veil = getattr(self, "_page_veil", None)
        self._page_veil = None
        if veil is None:
            return
        try:
            veil.release_pixmaps()
            veil.hide()
            veil.setParent(None)
            veil.deleteLater()
        except Exception:
            pass

    def _cancel_page_transition(self) -> None:
        """取消未完成的转场（全量重建前调用，快照随面板销毁而失效）。

        只清状态、**不动守卫本身**：守卫的复位留给 rebuild() 的 finally 走正常
        路径，避免在 rebuild 进行中提前重启手柄轮询。
        """
        self._pending_old_snapshot = None
        self._transition_active = False
        self._rebuild_deferred_ctrl = None
        self._rebuild_deferred_gp = False
        tr = getattr(self, "_page_transition", None)
        if tr is not None:
            try:
                tr.stop()
            except Exception:
                pass
        self._teardown_veil()

    def _finish_page_transition(self) -> None:
        """转场收尾：拆覆盖层 + 释放位图 + 复位被延期的守卫 / 手柄轮询。"""
        self._transition_active = False
        tr = getattr(self, "_page_transition", None)
        if tr is not None:
            try:
                tr.stop()
            except Exception:
                pass
        self._teardown_veil()
        self._release_transition_guard()

    def _on_page_transition_finished(self) -> None:
        self._finish_page_transition()

    def _page_transition_safety(self) -> None:
        """保底：动画回调万一丢失，强制收尾（守卫绝不能永久置位）。"""
        if getattr(self, "_transition_active", False):
            self._finish_page_transition()

    def _release_transition_guard(self) -> None:
        """复位 rebuild() 里被延期的守卫 / 手柄轮询（见 rebuild 的 finally）。"""
        _ctrl = getattr(self, "_rebuild_deferred_ctrl", None)
        _gp = getattr(self, "_rebuild_deferred_gp", False)
        self._rebuild_deferred_ctrl = None
        self._rebuild_deferred_gp = False
        if _ctrl is None:
            return
        try:
            _ctrl.set_rebuilding(False)
            if _gp:
                _ctrl.start_gamepad()
        except Exception:
            pass

    # -- 进入「页面布局编辑」的转场（HUD ↔ 编辑器全窗切换）----------------
    # 与上方卡片分页转场**完全独立**：单独的状态 + 单独的盖罩/驱动器，复用
    # 同一套 TransitionVeil / PageTransition 与两阶段盖罩时序。任何异常一律
    # 静默降级为现有硬切（_open_editor / _close_editor 本来就切好了页面），
    # 绝不卡死、绝不把页面永久盖住。设计见 ui/page_transition.py 模块文档。

    def _editor_transition_mode(self) -> str:
        """进入编辑器转场模式："slide" / "fade" / "none"。读配置失败回落 fade。"""
        try:
            cfg = self._app.config_manager.config
            m = str(getattr(cfg, "editor_transition", "fade") or "fade").strip().lower()
        except Exception:
            return "fade"
        return m if m in TRANSITION_MODES else "fade"

    def _editor_transition_ms(self) -> int:
        """进入编辑器转场时长（ms），clamp 到 [0, 600]。0 = 关闭动画。"""
        try:
            cfg = self._app.config_manager.config
            ms = int(getattr(cfg, "editor_transition_ms", DEFAULT_TRANSITION_MS))
        except Exception:
            return DEFAULT_TRANSITION_MS
        return max(0, min(600, ms))

    def _arm_editor_transition(self, direction: int) -> None:
        """进入/退出编辑器前：抓「旧屏」快照并铺盖罩（旧屏仍完整可见时调用）。

        direction = +1：从 HUD 进入编辑器（编辑器从右滑入）；
        direction = -1：从编辑器退回 HUD（编辑器向右滑出、HUD 从左滑入）。
        抓到快照后立刻铺盖罩，使随后的 QStackedWidget 硬切发生在盖罩之下，
        避免「先闪一下新屏、再播动画」的时序伪影（与 _arm_page_transition 同源）。
        """
        self._editor_old_snapshot = None
        self._editor_transition_dir = 1 if direction >= 0 else -1
        mode = self._editor_transition_mode()
        if mode == "none" or self._editor_transition_ms() <= 0:
            return
        if getattr(self, "_editor_transition_active", False):
            # 上一次编辑器转场还在进行：先拆干净再开新的，避免两个盖罩叠加。
            self._finish_editor_transition()
        try:
            old_pm = self._grab_page_snapshot()
        except Exception:
            old_pm = None
        if old_pm is None:
            return
        rect = self._page_viewport_rect()
        if rect is None:
            return
        self._editor_old_snapshot = old_pm
        self._teardown_editor_veil()
        try:
            veil = TransitionVeil(self, mode, old_pm, None, self._editor_transition_dir)
            veil.setGeometry(rect)
            veil.raise_()
            veil.show()
            veil.repaint()
            self._editor_veil = veil
        except Exception:
            self._editor_old_snapshot = None
            return
        # 置位「转场意图」+ 保底定时器：即使动画回调丢失也不会让盖罩永久置位。
        self._editor_transition_active = True
        QTimer.singleShot(self._editor_transition_ms() + SAFETY_MARGIN_MS,
                          self._editor_transition_safety)

    def _run_editor_transition(self, direction: int) -> None:
        """抓「新屏」快照 + 起动画。任何异常静默降级为硬切（清场）。

        direction 与 _arm_editor_transition 一致：+1 进入 / -1 退出。
        时序：① 隐藏盖罩 → ② 抓新屏快照 → ③ 补回同一盖罩并重新显示 → ④ 起动画。
        ①②③ 同步完成，中间不经过事件循环，不会被合成出「新屏真身」的一帧。
        """
        old_pm = getattr(self, "_editor_old_snapshot", None)
        self._editor_old_snapshot = None
        started = False
        try:
            if old_pm is None or old_pm.isNull():
                raise RuntimeError("no editor old snapshot")
            mode = self._editor_transition_mode()
            rect = self._page_viewport_rect()
            if mode == "none" or rect is None or not self.isVisible():
                raise RuntimeError("editor transition disabled")
            veil = getattr(self, "_editor_veil", None)
            if veil is not None:
                try:
                    veil._direction = 1 if direction >= 0 else -1
                except Exception:
                    pass
                veil.hide()
            # 强制布局先落定：_open_editor / _close_editor 只切了 stacked widget，
            # 布局要等事件循环才重算；不激活就抓可能得到尚未布局的页面。
            try:
                lay = getattr(self, "inner_layout", None)
                if lay is not None:
                    lay.activate()
            except Exception:
                pass
            new_pm = self._grab_page_snapshot()
            if new_pm is None:
                raise RuntimeError("no editor new snapshot")
            if veil is None:
                # 兜底：盖罩缺失（被并发 finish 掉）时现建一个。
                veil = TransitionVeil(self, mode, old_pm, new_pm,
                                      1 if direction >= 0 else -1)
                veil.setGeometry(rect)
                self._editor_veil = veil
            else:
                veil.set_new_pixmap(new_pm)
            veil.raise_()
            veil.show()
            if self._editor_transition is None:
                self._editor_transition = PageTransition(self)
                self._editor_transition.finished.connect(
                    self._on_editor_transition_finished)
            self._editor_transition.start(veil, self._editor_transition_ms())
            started = True
        except Exception:
            started = False
        if not started:
            # 走到这里说明转场没能起来（或中途抛异常）：必须拆掉盖罩。
            # 少了这一步，页面会被旧屏快照**永久**盖住。
            self._finish_editor_transition()

    def _teardown_editor_veil(self) -> None:
        """销毁编辑器转场覆盖层并立刻释放快照（每张约 3MB）。"""
        veil = getattr(self, "_editor_veil", None)
        self._editor_veil = None
        if veil is None:
            return
        try:
            veil.release_pixmaps()
            veil.hide()
            veil.setParent(None)
            veil.deleteLater()
        except Exception:
            pass

    def _finish_editor_transition(self) -> None:
        """编辑器转场收尾：拆覆盖层 + 释放位图 + 复位守卫。"""
        self._editor_transition_active = False
        tr = getattr(self, "_editor_transition", None)
        if tr is not None:
            try:
                tr.stop()
            except Exception:
                pass
        self._teardown_editor_veil()

    def _on_editor_transition_finished(self) -> None:
        self._finish_editor_transition()

    def _editor_transition_safety(self) -> None:
        """保底：动画回调万一丢失，强制收尾（盖罩绝不能永久置位）。"""
        if getattr(self, "_editor_transition_active", False):
            self._finish_editor_transition()

    def _on_gp_connection_changed(self, connected: bool, name: str, battery: int = 0) -> None:
        """Update the header gamepad-status icon + tray tooltip when a
        controller connects / disconnects (driven by the SDL backend's hot-plug
        detection).  The header shows a compact glyph whose colour encodes
        battery (green -> red); it is hidden when detached."""
        tip = (f"凝冰结魄 — 手柄: 已连接 ({name})" if (connected and name)
               else "凝冰结魄 — 手柄: 已连接" if connected
               else "凝冰结魄 — 手柄: 未检测到（可用键盘）")
        if getattr(self, "_gp_status", None) is not None:
            try:
                self._gp_status.set_state(connected, name, battery)
            except Exception:
                pass
        tray = getattr(self, "_tray", None)
        if tray is not None:
            try:
                tray.setToolTip(tip)
            except Exception:
                pass

    def _on_gp_battery(self, level: int) -> None:
        """Recolor the header glyph when the SDL power tier changes (green ->
        red).  Visibility is owned by ``_on_gp_connection_changed``."""
        if getattr(self, "_gp_status", None) is not None:
            try:
                self._gp_status.set_battery(level)
            except Exception:
                pass

    def _on_card_focus_changed(self, old_panel: int, new_panel: int) -> None:
        """焦点切换到另一个功能卡片时，关闭仍打开的疑惑解答弹窗。

        避免弹窗停在旧卡片的解答内容上；切到新卡片后应让它重新按新卡片触发。

        例外：若弹窗此时正好显示「新卡片」的解答（例如点该卡片标题直接开
        FAQ 并同步把焦点切过去），则不关——否则会立刻把刚打开的弹窗关掉。
        """
        # 手柄/键盘焦点切换：冻住呼吸相位一小段，避免操作结束后亮度突跳
        # （配合 BreathingController.note_activity）。放在 FAQ 早返回之前，
        # 保证每次焦点切换都触发，与弹窗是否打开无关。
        breath = getattr(self, "_breath", None)
        if breath is not None:
            try:
                breath.note_activity(0.18)
            except Exception:
                pass
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
        # --- 页面转场 Phase 1：在旧页仍完整可见时抓快照 --------------------
        # 必须放在任何 rebuild / repaint 之前：此刻旧页布局稳定，抓到的就是
        # 用户看到的内容。抓成功会把 _transition_active 置位，rebuild() 的
        # finally 据此把守卫复位延期到动画结束（见 _arm_page_transition）。
        self._arm_page_transition()
        # 按钮编辑器全页模式（_editor_all_pages）：HUD 不重建（所有页面已实例化），
        # 但切页瞬间仍必须立即隐藏焦点框——否则黄框停在旧编辑器项/旧位置，
        # 要等 ~1s 的 sync_all 才被重新评估（延迟消失/越界残留）。
        # 这里只藏框 + 关闭按钮列表浮窗，不 return；继续走下方统一的
        # 「置重建守卫 → singleShot(0) → _do_page_rebuild」路径，
        # 使守卫置/清与正常切页完全一致，并把焦点框重摆收口到 sync_all。
        if getattr(self, "_editor_all_pages", False):
            _ctrl = getattr(self, "_controller", None)
            if _ctrl is not None:
                try:
                    _ctrl._hide_focus_rect()
                except Exception:
                    pass
        else:
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
        # 关掉「手柄 RB 按下 → rebuild 真正执行」之间的 30ms 轮询空窗：重建守卫
        # 在本次切页触发的同一调用栈内立即置位，使后续的 30ms 轮询在空窗期直接
        # no-op（见 InteractionController.set_rebuilding），避免轮询在面板被
        # deleteLater/重建的中途去重绘半建窗口——这是「切页后确定性闪退」的核心
        # 成因（仅 rebuild() 内部才置守卫会漏掉这一段空窗）。rebuild() 的 finally
        # 会复位它。rebuild() 内部仍会再置一次并复位，二者一致。
        _ctrl = getattr(self, "_controller", None)
        if _ctrl is not None:
            try:
                _ctrl.set_rebuilding(True)
                # 切页瞬间立即隐藏焦点框（归零几何，根治残留长条），不等 singleShot(0) 重建。
                # 此处只是把「从按下翻页到重建执行之间」那一个事件循环窗口的黄框可见性提前掐断。
                _ctrl._hide_focus_rect()
                # 主 HUD 切页由随后的 rebind() 做这套复位；但按钮编辑器全页模式走
                # sync_all 不调 rebind()，故这里把同样的复位补一遍，保证两条路径一致：
                # 切页强制回卡片模式 + 清空陈旧活跃卡片索引，避免 refresh() 用旧页
                # _active_card 把焦点框复活到新页错误卡片（与最初 HUD 越界 bug 同源）。
                _ctrl._card_mode = True
                _ctrl._active_card = -1
                _ctrl._card_focus = -1
                # 同步清空扁平焦点索引 _focus：否则「按下翻页 → singleShot(0)
                # 真正重建」之间的事件循环窗口里，1s 同步计时器 / sync_all
                # 可能抢先 refresh()，用旧索引把黄框复活到旧页位置，造成
                # 「页面已切、焦点框还在」的残留。编辑模式（布局编辑器内）保留
                # _focus，由 refresh() 的 (card_token, col) 重解析继续跟随卡片。
                if not _ctrl._edit_mode:
                    _ctrl._focus = -1
                _ctrl._apply_card_highlight()
            except Exception:
                pass
        # 立即重绘窗口，擦掉半透明无边框窗口上「黄框上一帧」的合成残影：
        # hide() 只调度下一次绘制，DWM 已合成的当前帧仍含旧黄框，不强制
        # repaint 一次用户就会看到「页面已切、黄框还在」的一帧延迟。
        try:
            self.repaint()
        except Exception:
            pass
        self._page_rebuild_pending = True
        QTimer.singleShot(0, self._do_page_rebuild)

    def _do_page_rebuild(self) -> None:
        self._page_rebuild_pending = False
        # 无论走哪条分支都先复位重建守卫：本方法可能因 _editor_all_pages 提前
        # return 而不进入 rebuild()，若此时守卫仍置位会让手柄轮询永久 no-op。
        # 复位后若进入 rebuild()，它内部会立刻重新置位并在自身 finally 里再次复位，
        # 二者窗口极短且单线程内不会插入任何定时器，故无竞态。
        _ctrl = getattr(self, "_controller", None)
        if _ctrl is not None:
            try:
                _ctrl.set_rebuilding(False)
            except Exception:
                pass
        if getattr(self, "_editor_all_pages", False):
            # 全页模式不重建 HUD（所有页面已实例化，重建会隐藏其他页按钮）。
            # 但切页后必须让 controller.refresh() 按当前（新页）编辑器项把焦点框
            # 正确重摆/隐藏，消除「黄框停在旧位置」的延迟消失/越界。
            QTimer.singleShot(0, self._bridge.sync_all)
            return
        self.rebuild(restyle=False, reload_tiles=False, heavy=False)
        # --- 页面转场 Phase 1：起动画 ---------------------------------------
        # 再延一个 tick：让布局真正落定、新页完成一次绘制后再抓快照，
        # 否则可能抓到尚未布局（0 高度）或半建状态的页面。
        if getattr(self, "_pending_old_snapshot", None) is not None:
            QTimer.singleShot(0, self._start_page_transition)

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
        self._hud_signature_open = self._compute_hud_signature()
        # --- 进入编辑器转场（方案1：右侧滑入）---------------------------
        # 在旧屏（HUD）仍完整可见时抓快照并铺盖罩，随后 QStackedWidget 硬切
        # 发生在盖罩之下；切到编辑器后再抓新屏补进同一盖罩并起动画。复用
        # ui/page_transition.py 的两阶段盖罩时序，任何异常静默降级为硬切。
        self._arm_editor_transition(1)
        self._edit_overlay.open_layout()
        self._body.setCurrentWidget(self._edit_overlay)
        self._controller.set_edit_mode(True)
        self._controller.set_host(self._edit_overlay)
        self._controller.start_gamepad()
        # Hide the HUD page indicator bar so its [1][2]… buttons don't show
        # through the editor overlay.
        self._apply_page_bar_visibility()
        # 延迟到下一 tick：让编辑器布局落定、完成一次绘制后再抓新屏快照起动画。
        if getattr(self, "_editor_old_snapshot", None) is not None:
            QTimer.singleShot(0, lambda: self._run_editor_transition(1))

    def _close_editor(self) -> None:
        """Return to the HUD, reflecting any layout / theme changes."""
        # Drop the all-pages mode (set while the button-style editor was open)
        # so the HUD returns to showing only the current page's cards.
        self._editor_all_pages = False
        # 是否真的从编辑器退回（避免非编辑器态误触发转场）。
        was_edit = bool(getattr(self._controller, "_edit_mode", False))
        # --- 退出编辑器转场（方案1：编辑器向右滑出、HUD 从左滑入）--------
        # 先抓旧屏（编辑器）快照铺盖罩；随后切回 HUD（发生在盖罩之下）；
        # 切回后再抓 HUD 补进同一盖罩并起反向滑动。
        if was_edit:
            self._arm_editor_transition(-1)
        # 结构/主题未变 → 复用缓存面板直接返回（不拆不建），消除退出卡顿；
        # 变了才走真正的 rebuild()。比对维度见 _compute_hud_signature。
        if self._compute_hud_signature() == self._hud_signature_open:
            self._body.setCurrentWidget(self._scroll)
            self._collapse_to_current_page()
            self._apply_button_styles()
            self._refresh_all_accents()
        else:
            self._body.setCurrentWidget(self._scroll)
            self.rebuild()
        self._controller.set_edit_mode(False)
        self._controller.set_host(self.inner)
        self._apply_page_bar_visibility()
        # 延迟到下一 tick 抓新屏（HUD）快照并起动画。
        if was_edit and getattr(self, "_editor_old_snapshot", None) is not None:
            QTimer.singleShot(0, lambda: self._run_editor_transition(-1))

    def _compute_hud_signature(self) -> str:
        """结构签名：页面结构 + 主题色 + 呼吸色。

        进入 / 退出编辑器时分别采样，若一致说明编辑未改结构或主题，
        退出即可跳过重建（直接复用缓存面板）。
        """
        try:
            pages = self._app.render_pages()
            struct = json.dumps(pages, sort_keys=True, ensure_ascii=False)
        except Exception:
            struct = ""
        # 主题色/呼吸色可能是 PyQt 的 QColor（有 .name()）或本项目自定义的
        # Color（只有 .to_hex()）。两者都能产出 #RRGGBB 风格的十六进制串，
        # 这里统一自适应，避免某一侧缺方法时崩溃。
        def _color_hex(c) -> str:
            if c is None:
                return ""
            return c.to_hex() if hasattr(c, "to_hex") else c.name()

        ac = _color_hex(getattr(self, "_accent", None))
        bc = _color_hex(getattr(self, "_breath_color", None))
        try:
            return hashlib.md5(
                "|".join([struct, ac, bc]).encode("utf-8")).hexdigest()
        except Exception:
            return ""

    def _collapse_to_current_page(self) -> None:
        """退出编辑器且结构未变时，复用缓存面板回到单页 HUD（不重建）。"""
        self._editor_all_pages = False
        self._detach_containers_from_layout()
        app = self._app
        nav = app.nav
        pages = app.render_pages()
        idx = self._current_page_idx
        if not isinstance(idx, int) or idx < 0:
            idx = nav.current_page if nav is not None else 0
        idx = max(0, min(idx, max(0, len(pages) - 1))) if pages else 0
        self._build_page_container(idx)
        c = self._page_containers[idx]
        if c.parent() is not self.inner:
            c.setParent(self.inner)
        self.inner_layout.addWidget(c)
        c.setVisible(True)
        self.panels.clear()
        self.panels.extend(self._page_panels[idx])
        self._current_page_idx = idx
        self._controller.rebind()
        self._did_rebind = True
        QTimer.singleShot(0, lambda: self._scroll.verticalScrollBar().setValue(0))

    def _startup_ls_detect(self) -> None:
        """开机时若 LS 路径为空或失效，静默扫描正在运行的 LS 进程并写入。

        失败（无 LS 进程 / 无权限 / 异常）一律静默忽略——不在开机阶段弹窗或打断。
        """
        try:
            ls_block = self._app.blocks.get("ls")
            if ls_block is not None and hasattr(ls_block, "startup_detect"):
                ls_block.startup_detect()
        except Exception:  # noqa: BLE001 - 启动探测绝不应影响开机
            pass

    def _open_editor_ls(self) -> None:
        """从 LS 卡片「⚙」或「空路径启动」直接打开 LS 设置编辑项。"""
        if not getattr(self._controller, "_edit_mode", False):
            faq = getattr(self, "_faq", None)
            if faq is not None:
                faq.close()
            self._hud_signature_open = self._compute_hud_signature()
            self._body.setCurrentWidget(self._edit_overlay)
            self._controller.set_edit_mode(True)
            self._controller.set_host(self._edit_overlay)
            self._controller.start_gamepad()
            self._apply_page_bar_visibility()
        self._edit_overlay.open_ls("ls")

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
        """手柄 Start（HUD 已在屏上时）：把 HUD 收进托盘；若记到了有效且**未冻结**的游戏窗口，则先把焦点还给它。

        游戏被冻结（suspended）时**绝不**抢焦点、也**绝不**解冻——只收起 HUD，
        让游戏安静停在后台暂停态（用户要的是『只最小化 NBJ，保持冻结』；且把
        焦点交给挂起窗口正是历史『卡死』死局）。解冻请走冻结卡片的数字键。

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
                # 游戏若被冻结（suspended）：绝不恢复窗口 / 抢焦点 / 解冻——
                # 把焦点交给一个无法 pump 消息的挂起窗口正是历史『卡死』死局，
                # 且解冻会偷改用户明确的冻结状态（用户要的是『只最小化 NBJ，
                # 保持冻结』）。故冻结时只收起 HUD，游戏安静停在后台暂停态。
                if not self._is_game_frozen(game_hwnd):
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

        游戏被冻结时直接返回：保持冻结、不抢焦点、不解冻（与 ``_return_to_game``
        一致）。想恢复暂停的游戏，请先在冻结卡片里手动解冻。

        仅 best-effort：记不到有效游戏窗口（``_prev_game_hwnd`` 缺失、或句柄已
        失效/被复用/属于桌面外壳）就什么都不做——既不 fallback 抢当前前台窗口
        （重启掌机后会误拉起『关机前的程序』），也绝不召唤 HUD，因为召唤是全局
        热键（Win+Ctrl+F8）的职责，Start 严格只做『回到游戏』。
        """
        try:
            game_hwnd = self._resolve_game_hwnd()
            if not game_hwnd:
                return
            # 游戏若被冻结：保持冻结，不抢焦点、不解冻（同 _return_to_game）。
            if self._is_game_frozen(game_hwnd):
                return
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

    def _is_game_frozen(self, hwnd: int) -> bool:
        """记忆的游戏窗口所属进程是否仍被冻结（suspended）——只读探测，绝不
        改动冻结状态。

        Start 据此决定要不要把焦点还给游戏窗口：冻结时一律不抢焦点、不解冻
        （用户要的是『只最小化 NBJ，保持冻结』；且把焦点交给挂起窗口正是历史
        『卡死』死局）。best-effort，任何异常都按『不可信』返回 False（宁可
        误判为未冻结去抢焦点，也不因探测失败让 Start 整体失效）。"""
        try:
            if not hwnd:
                return False
            user32 = ctypes.windll.user32
            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return False
            fb = self._app.blocks.get("freeze") if getattr(self, "_app", None) else None
            pm = getattr(fb, "pm", None)
            if pm is None or not getattr(pm, "available", False):
                return False
            return bool(pm.is_suspended(pid.value))
        except Exception:  # noqa: BLE001
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
        # 全局热键唤出 HUD：事件驱动刷新电源计划（本函数可能从 nativeEvent 栈内
        # 触发，故延后到事件循环执行，避免卡死消息泵）。
        try:
            QTimer.singleShot(0, self._refresh_powerplan)
        except Exception as exc:  # noqa: BLE001
            self._hotkey_err("schedule_pp_refresh", exc)

    def _steal_foreground_best_effort(self, _attempt: int = 0) -> None:
        """尽力把 HUD 变成前台窗口并拿到键盘焦点。绝不向消息泵抛异常。

        为什么需要它（而不只是 Qt 的 ``raise_()`` + ``activateWindow()``）：
        Windows 的**前台锁**会让「非前台进程」的 ``SetForegroundWindow`` 静默
        失败（只闪任务栏）。Qt 的 ``activateWindow()`` 底层就是它，所以从托盘
        / 热键唤出 HUD 时会出现「窗口显示了、键盘和手柄却仍归游戏或桌面」。
        这里先把本线程 ``AttachThreadInput`` 挂到前台线程上，让前台锁把本线程
        视作前台进程的一部分，再依次 ``SetForegroundWindow`` /
        ``BringWindowToTop`` / ``SetActiveWindow`` / ``SetFocus``；用完立即解绑
        （长期不解绑会让两个线程共享输入队列，鼠标键盘行为错乱）。

        ``_attempt`` 用于**有上限的单次补刀**：托盘右键菜单刚被点下时菜单仍处于
        active 状态，``SetForegroundWindow`` 同样会被拒；等菜单关掉（约一次
        ``_STEAL_FG_RETRY_MS`` 之后）再试一次即可成功——但只补一次，绝不无限
        重试（用户可能已经 Alt-Tab 走了，抢焦点就变成骚扰）。
        """
        try:
            from PyQt6.sip import isdeleted
            if isdeleted(self):
                return
        except Exception:  # noqa: BLE001
            pass
        try:
            # 单独包一层：窗口的 C++ 对象若已被销毁，这里会抛 RuntimeError；
            # 不能用 `except: pass` 放行（下面还会继续摸 self），必须直接返回。
            if not self.isVisible():
                return
        except Exception:  # noqa: BLE001
            return
        if not _USER32_READY:
            try:
                self.activateWindow()
            except Exception:  # noqa: BLE001
                pass
            return
        ok = False
        try:
            user32 = _user32
            hwnd = int(self.winId())
            if not hwnd:
                return
            fg = user32.GetForegroundWindow()
            fg_thread = user32.GetWindowThreadProcessId(fg, None)
            # kernel32，不是 user32 —— 见模块头部的说明（旧代码写错 DLL，
            # 导致整段抢焦点静默失效）。
            cur_thread = _kernel32.GetCurrentThreadId()
            attached = False
            if fg_thread and fg_thread != cur_thread:
                attached = bool(user32.AttachThreadInput(fg_thread, cur_thread, True))
            try:
                # 最小化态必须先还原：对图标态窗口 SetForegroundWindow 一律失败。
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE = 9
                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
                user32.SetActiveWindow(hwnd)
                # 只有键盘焦点还落在别的窗口时才硬拉；焦点已在本窗口子树内时
                # 不动它，免得把 Qt 内部 focusWidget 状态搞乱（按钮焦点丢失）。
                if not user32.IsChild(hwnd, user32.GetFocus()):
                    user32.SetFocus(hwnd)
            finally:
                # 无论成败都必须解绑，否则输入队列被永久串在一起。
                if attached:
                    user32.AttachThreadInput(fg_thread, cur_thread, False)
            ok = int(user32.GetForegroundWindow() or 0) == hwnd
        except Exception:  # noqa: BLE001
            # Best-effort only: fall back to the Qt path, never crash.
            try:
                self.activateWindow()
            except Exception:  # noqa: BLE001
                pass
            return
        if not ok and _attempt < 1:
            # ⚠️ PyQt6 **没有** ``singleShot(msec, receiver, method)`` 三参重载
            # （实测抛 TypeError），只能用两参形式绑一个无参 bound method。
            try:
                QTimer.singleShot(_STEAL_FG_RETRY_MS,
                                  self._steal_foreground_retry)
            except Exception:  # noqa: BLE001
                pass

    def _steal_foreground_retry(self) -> None:
        """``_steal_foreground_best_effort`` 的补刀入口（无参，供 QTimer 直接绑）。

        窗口若在定时器到期前销毁，函数开头的 ``isdeleted`` + ``isVisible`` 双重
        守卫会直接返回，不会把 RuntimeError 抛进事件循环。"""
        self._steal_foreground_best_effort(_attempt=1)

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
        # Esc — two-stage on the main HUD (keyboard), mirroring the gamepad
        # B / Select split so the keyboard path behaves consistently:
        #   - inside a single card (item mode, controller._card_mode == False):
        #     hand Esc to the interaction controller so it EXITS the card back
        #     to the card-selection layer — it must NOT jump into the editor.
        #   - on the card-selection layer (controller._card_mode == True):
        #     open the page-layout editor (mirrors F2).
        #   - in the editor (layout page) -> close back to the HUD,
        #   - in an editor sub-page   -> return to the layout page.
        # Previously Esc ALWAYS opened the editor from anywhere on the main
        # page, which broke "Enter card -> Esc exits card -> Esc opens editor".
        # Now the editor only opens from the card-selection layer.
        if ev.key() == Qt.Key.Key_Escape:
            if self._controller._edit_mode:
                self._gp_cancel()
                ev.accept()
                return
            fw = self.focusWidget()
            if isinstance(fw, (QLineEdit, QTextEdit, QPlainTextEdit)):
                super().keyPressEvent(ev)  # let a focused text field consume Esc
                return
            if not self._controller._card_mode:
                # Inside a card: delegate Esc to the controller so it exits the
                # card back to card selection.  Never open the editor here.
                try:
                    self._controller.handle_key(ev)
                except Exception:
                    # navigation logic is defensively guarded internally; the
                    # final backstop just keeps a single bad card from killing
                    # the HUD on a keypress.
                    pass
                ev.accept()
                return
            self._open_editor()  # card-selection layer: open page-layout editor
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

    # -- 电源计划事件驱动刷新（唤醒前台 / AC-DC 插拔电） --------------
    def _refresh_powerplan(self) -> None:
        """事件驱动刷新电源计划卡片（统一收口到 Application）。

        离屏（headless/测试）环境直接跳过，避免跑 powercfg 子进程。所有调用方
        都通过 QTimer.singleShot(0, ...) 延后到 Qt 事件循环执行——绝不在
        nativeEvent 消息泵栈内直接跑子进程，否则可能卡死消息泵。"""
        try:
            if getattr(self, "_offscreen", False):
                return
            app = getattr(self, "_app", None)
            if app is None or not hasattr(app, "refresh_powerplan_if_visible"):
                return
            app.refresh_powerplan_if_visible()
        except Exception:  # noqa: BLE001
            pass

    def _register_power_source_notifier(self) -> None:
        """注册 GUID_ACDC_POWER_SOURCE 通知（win32 且仅真实窗口）。

        注册后 OS 在插拔电时会经 WM_POWERBROADCAST / PBT_POWERSETTINGCHANGE 把
        事件推给本窗口（事件驱动、零轮询）。失败一律静默——不影响其它功能。"""
        if not _WTS_AVAILABLE or getattr(self, "_offscreen", False):
            return
        try:
            user32 = ctypes.windll.user32
            hwnd = int(self.winId())
            if not hwnd:
                return

            class _GUID(ctypes.Structure):
                _fields_ = [("Data1", ctypes.c_ulong),
                            ("Data2", ctypes.c_ushort),
                            ("Data3", ctypes.c_ushort),
                            ("Data4", ctypes.c_ubyte * 8)]

            g = _GUID(0x5D3E9A59, 0xE9D5, 0x4B00,
                      (0xA6, 0xBD, 0xFF, 0x34, 0xFF, 0x51, 0x65, 0x48))
            try:
                from ctypes import wintypes
                user32.RegisterPowerSettingNotification.argtypes = [
                    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
                user32.RegisterPowerSettingNotification.restype = ctypes.c_void_p
            except Exception:
                pass
            h = user32.RegisterPowerSettingNotification(
                hwnd, ctypes.byref(g), 0)
            if h:
                self._acdc_handle = h
        except Exception:  # noqa: BLE001
            self._acdc_handle = None

    def _unregister_power_source_notifier(self) -> None:
        h = getattr(self, "_acdc_handle", None)
        if h is None:
            return
        try:
            user32 = ctypes.windll.user32
            user32.UnregisterPowerSettingNotification.argtypes = [ctypes.c_void_p]
            user32.UnregisterPowerSettingNotification(h)
        except Exception:  # noqa: BLE001
            pass
        self._acdc_handle = None

    def _on_power_setting_change(self, msg: Any) -> None:
        """PBT_POWERSETTINGCHANGE：仅当是 AC/DC 电源切换才刷新电源计划卡片。

        lParam 指向 POWERBROADCAST_SETTING（前 16 字节为该设置的 GUID）。读取并
        比对 GUID_ACDC_POWER_SOURCE；其它电源设置变更（如显示器开关）一律忽略。
        解析或比对失败则静默跳过——不依赖此路径的正确性。"""
        try:
            lp = int(msg.lParam)
            if not lp:
                return
            buf = ctypes.cast(lp, ctypes.POINTER(ctypes.c_ubyte * 16))
            guid = bytes(buf.contents)
            if guid != _ACDC_GUID_BYTES:
                return
            # 延后到事件循环，避免在 nativeEvent（消息泵）栈内跑 powercfg。
            QTimer.singleShot(0, self._refresh_powerplan)
        except Exception:  # noqa: BLE001
            pass

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
        # 1b) 持久化固定档（降压 -30mV 状态按钮）：休眠后 SMU 已重置，重钉一次。
        #     与自动降压互斥——pinned_mv 非零时走这里（不为 0 且非 dry_run）。
        try:
            ctrl = getattr(self, "_uv_controller", None)
            pinned = getattr(ctrl.config, "pinned_mv", 0) \
                if ctrl is not None else 0
            if isinstance(pinned, int) and pinned != 0 \
                    and ctrl is not None \
                    and not getattr(ctrl.config, "dry_run", False):
                ctrl.backend.apply_coall(pinned)
                ctrl.last_applied_mv = pinned
                self._uv_last["state"] = "pinned"
                self._uv_last["mv"] = pinned
                if "降压" not in reapplied:
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
                # 呼吸边框(_glow_border)现为 topmost 独立窗口：主窗激活/置顶时会把它
                # 压回 z 序之下 → 边框消失。这里在主窗激活后把它重新顶到主窗之上
                # （透明中心 + WA_TransparentForMouseEvents，不挡点击）。此分支覆盖
                # 聚焦/热键唤出/托盘恢复/淡入等所有激活路径。
                b = getattr(self, "_glow_border", None)
                if b is not None:
                    b.raise_()
                # 丙方案：重新激活 -> 呼吸边框柔和淡回（若此前因失焦熄灭）。
                # 但若正处于托盘淡入(_fading)，光效 opacity 已由锁步动画独占，
                # 这里跳过，避免与 _fade_in_from_tray 的锁步淡入抢同一 windowOpacity。
                if not self._fading:
                    self._fade_breath_in()
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
                    self._start_dim(1.0)
                    if getattr(self, "_breath", None) is not None:
                        self._breath.resume()
                    self._ensure_fast()
                    self._ensure_gamepad()
                    # 自己的弹窗不算真失焦：若此前已因失焦熄灭，这里淡回。
                    self._fade_breath_in()
                elif self.isVisible() and not (self.windowState() &
                                               Qt.WindowState.WindowMinimized):
                    self._start_dim(self._DIM_UNFOCUSED)
                    # Unfocused-but-visible: idle the heavier refresh timers but
                    # keep the 1 s display refresh alive so the perfmon / read-
                    # only cards stay live while a game is in front.
                    self._ensure_fast()
                    self._pause_heavy_aux()
                    self._ensure_gamepad()  # background gamepad still listens
                    # 丙方案(2026-08-29)：失焦 -> 呼吸边框柔和渐隐到 0。
                    # ⚠️ 设计意图变更：原注释要求「游戏在前台时呼吸灯也保持可见」，
                    # 现按用户要求改为失焦即熄灭（游戏前台看不到呼吸灯）。
                    # 但若正处于托盘淡出(_fading)，光效 opacity 已由锁步动画独占，
                    # 这里跳过，避免与 _fade_out_then_hide 的锁步淡出抢同一 windowOpacity。
                    if not self._fading:
                        self._fade_breath_out()
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
        # 呼吸边框(_glow_border)现为 topmost 独立窗口，显示时一并顶到主窗之上
        # （透明中心 + WA_TransparentForMouseEvents，不挡点击）。changeEvent 激活
        # 分支也会顶，这里覆盖首启尚未激活的边界情况。
        b = getattr(self, "_glow_border", None)
        if b is not None:
            b.raise_()

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
            elif wp == _PBT_POWERSETTINGCHANGE:
                self._on_power_setting_change(msg)
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
        self._unregister_power_source_notifier()
        self._unregister_global_hotkey()
        if getattr(self, "_breath", None) is not None:
            self._breath.stop()
        # Stop any running fade animation so a queued finished/hide callback does
        # not fire after the window is destroyed.
        _fade = getattr(self, "_fade_anim", None)
        if _fade is not None:
            try:
                _fade.stop()
            except Exception:
                pass
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
        # 终止共享指标采样器（MetricSampler → 级联 SharedMonitor 的 PowerShell
        # 采样子进程）。closeEvent 此前只停了直连的 _GpuUtilMonitor，漏了
        # MetricSampler——它背后仍挂着 SharedMonitor 子进程，退出后变孤儿，
        # 直到 atexit 才回收（硬崩溃/快速退出路径下 atexit 不一定跑）。仅当
        # 单例已存在时才 stop，避免退出路径误创建采样器单例。
        try:
            if MetricSampler._instance is not None:
                MetricSampler.instance().stop()
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
    # 主线程、QApplication 已存在：初始化跨线程 UI 马歇尔桥（core.gui）。
    # 否则 rtss/power 后台线程的 show_toast() 会直接碰 QWidget → 原生崩溃。
    from ..core.gui import ensure_gui_bridge
    ensure_gui_bridge()
    build_overlay(app).show()
    return qapp.exec()