#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hint_hold_demo.py — 长按提示点「逐个消失」视觉原型 (SPIKE, 不碰主程序)

目的
----
验证用户想要的手柄长按反馈「视觉半」：按住 ABXY 提示键时，胶囊外侧的虚点
随按住时长逐颗淡出，到达 LONG_PRESS_MS 阈值时闪一下作「触发确认」。

为什么是独立 spike
-----------------
- 目前主程序 HintButton.set_hint_long_press 只有布尔量（点全亮 / 全灭），
  没有 progress 参数。本脚本自实现带 progress 的虚点弧，让你先「摸到手感」，
  不改动 src/ 任何一行 shipping 代码，确认节奏对不对再说接线。
- 胶囊本体绘制**直接复用** gamepad_hint.py 的真实函数（long_press=False 只画
  胶囊+字母），保证和 HUD 里看到的长得一模一样；只有「点淡出」是新行为，
  单独内联实现。

驱动方式（任选，多个可同时生效）
------------------------------
- 鼠标左键按住窗口里的按钮区域
- 按住空格键
- 接了真手柄时，按住 A 或 Y 键（复用生产同款 SDLGamepadBackend，最高保真）
- 勾选「自动演示」：脚本自己循环 按住→松开，纯看效果

判定阈值：progress = 按住时长 / LONG_PRESS_MS(500ms)；0→1 对应「点由全亮到全灭」。

运行：python tools/hint_hold_demo.py
（需要 PyQt6 + pygame；在用户的开发机/掌机上跑，本无显示环境不跑 GUI）
"""

from __future__ import annotations

import math
import os
import sys
import time

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (QApplication, QCheckBox, QFrame, QLabel,
                             QMainWindow, QVBoxLayout, QWidget)

# LONG_PRESS_MS 与主程序 core/shortcuts.py 保持一致（500ms）。
try:
    _HERE = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.normpath(os.path.join(_HERE, "..", "src")))
    from ningbingjue.core.shortcuts import LONG_PRESS_MS  # type: ignore
except Exception:
    LONG_PRESS_MS = 500

# --- 复用真实胶囊绘制（失败则退回极简内联版，保证脚本仍可跑）-------------
try:
    from ningbingjue.ui.gamepad_hint import (  # type: ignore
        HINT_COLORS, HINT_LETTER_PX, HINT_PILL_TOP_H, HINT_PILL_TOP_W,
        HINT_PILL_W, draw_left_capsule_badge, draw_top_capsule_badge,
    )
    _HAVE_REAL_HINT = True
except Exception:
    _HAVE_REAL_HINT = False
    HINT_COLORS = {
        "A": QColor(130, 255, 30), "B": QColor(230, 75, 90),
        "X": QColor(95, 175, 235), "Y": QColor(245, 215, 75),
    }
    HINT_PILL_W = 20
    HINT_PILL_TOP_W = 24
    HINT_PILL_TOP_H = 13
    HINT_LETTER_PX = 12


# --- 真手柄（生产同款后端，可选）-----------------------------------------
try:
    from ningbingjue.ui.gamepad import SDLGamepadBackend  # type: ignore
    _HAVE_REAL_PAD = True
except Exception:
    _HAVE_REAL_PAD = False


# 虚点弧几何常量（与 gamepad_hint.py 一致）
HINT_ARC_DOTS = 5        # 长按钮：右弧点数量
HINT_ARC_GAP = 3         # 弧半径外扩
HINT_ARC_DOT_R = 1.7     # 单颗点半径
HINT_BOTTOM_DOTS = 3     # 短按钮：下方点数量
HINT_BOTTOM_GAP = 5
HINT_BOTTOM_DY = 4


def _dot_opacity(i: int, n: int, progress: float) -> float:
    """第 i 颗点（0..n-1）在 progress(0..1) 下的不透明度。

    progress 跨过 [i/n, (i+1)/n] 区间时该点由 1 线性淡到 0；之前全亮、之后全灭。
    → 按住越久，灭掉的点越多（进度式逐个消失）。
    """
    lo = i / n
    hi = (i + 1) / n
    if progress <= lo:
        return 1.0
    if progress >= hi:
        return 0.0
    return 1.0 - (progress - lo) / (hi - lo)


def _draw_dot(p: QPainter, color: QColor, cx: float, cy: float, r: float,
              opacity: float) -> None:
    """画一颗带深色底衬的虚点，按 opacity 整体淡出。"""
    if opacity <= 0.01:
        return
    p.setPen(Qt.PenStyle.NoPen)
    # 底衬（按 opacity 缩放，否则点灭了底衬还留个黑印）
    back = QColor(0, 0, 0, int(160 * opacity))
    p.setBrush(back)
    p.drawEllipse(int(cx - r - 1), int(cy - r - 1),
                  int(2 * (r + 1)), int(2 * (r + 1)))
    # 彩色点
    c = QColor(color)
    c.setAlpha(int(255 * opacity))
    p.setBrush(c)
    p.drawEllipse(int(cx - r), int(cy - r), int(2 * r), int(2 * r))


def _draw_long_arc_progress(p: QPainter, color: QColor, x: int, y: int,
                            w: int, h: int, side: str, progress: float) -> None:
    """带 progress 的虚点弧（取代布尔 long_press）。

    side="right"：以胶囊中心为圆心、半径=外接半径+GAP，扫过 ±45° 的 5 颗点，
                  按 _dot_opacity 逐颗淡出（长按钮用）。
    side="bottom"：胶囊正下方横排 3 颗点，同样逐颗淡出（短按钮用）。
    """
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if side == "bottom":
        n = HINT_BOTTOM_DOTS
        total = (n - 1) * HINT_BOTTOM_GAP
        sx = x + w / 2.0 - total / 2.0
        py = y + h + HINT_BOTTOM_DY
        for i in range(n):
            px = sx + i * HINT_BOTTOM_GAP
            _draw_dot(p, color, px, py, HINT_ARC_DOT_R, _dot_opacity(i, n, progress))
        return

    cx = x + w / 2.0
    cy = y + h / 2.0
    base_r = max(w, h) / 2.0
    R = base_r + HINT_ARC_GAP
    n = HINT_ARC_DOTS
    start = 0.0 if side == "right" else 180.0
    for i in range(n):
        deg = start + (-45.0 + 90.0 * i / (n - 1))
        rad = math.radians(deg)
        px = cx + R * math.cos(rad)
        py = cy + R * math.sin(rad)
        _draw_dot(p, color, px, py, HINT_ARC_DOT_R, _dot_opacity(i, n, progress))


def _draw_capsule(p: QPainter, hint: str, side: str, abs_x: int, abs_y: int,
                  w: int, h: int, progress: float) -> None:
    """画胶囊 + 进度式虚点弧（复用真实胶囊函数，点弧用本地 progress 版）。

    abs_x/abs_y 是按钮区在窗口里的左上角；真实 helper 内部以 (3, 居中) 为原点，
    故用 translate 把它挪到目标位置，并紧接着在**同一本地坐标**里画点弧，
    保证点弧精确包裹胶囊（和 gamepad_hint.py 里 draw_*_capsule_badge 调用
    _draw_long_arc 的几何完全一致）。
    """
    color = HINT_COLORS.get(hint, QColor(180, 180, 180))
    p.save()
    p.translate(abs_x, abs_y)
    if _HAVE_REAL_HINT:
        if side == "left":
            cap_h = min(HINT_PILL_W, h - 4)
            draw_left_capsule_badge(p, hint, h, capsule_w=HINT_PILL_W,
                                    margin=3, long_press=False)
            _draw_long_arc_progress(p, color, 3, (h - cap_h) // 2,
                                    HINT_PILL_W, cap_h, "right", progress)
        else:  # top
            draw_top_capsule_badge(p, hint, w, long_press=False)
            _draw_long_arc_progress(p, color, 3, 1, HINT_PILL_TOP_W,
                                    HINT_PILL_TOP_H, "bottom", progress)
        p.restore()
        return
    # 退回极简内联胶囊（仅当真实模块不可用时）
    p.setPen(Qt.PenStyle.NoPen)
    if side == "left":
        cap_h = min(HINT_PILL_W, h - 4)
        x, y = 3, (h - cap_h) // 2
        p.setBrush(QColor(0, 0, 0, 160))
        p.drawRoundedRect(x - 1, y - 1, HINT_PILL_W + 2, cap_h + 2,
                          cap_h // 2 + 1, cap_h // 2 + 1)
        p.setBrush(color)
        p.drawRoundedRect(x, y, HINT_PILL_W, cap_h, cap_h // 2, cap_h // 2)
        _draw_long_arc_progress(p, color, x, y, HINT_PILL_W, cap_h,
                                "right", progress)
    else:
        x, y = 3, 1
        p.setBrush(QColor(0, 0, 0, 160))
        p.drawRoundedRect(x - 1, y - 1, HINT_PILL_TOP_W + 2,
                          HINT_PILL_TOP_H + 2,
                          HINT_PILL_TOP_H // 2 + 1, HINT_PILL_TOP_H // 2 + 1)
        p.setBrush(color)
        p.drawRoundedRect(x, y, HINT_PILL_TOP_W, HINT_PILL_TOP_H,
                          HINT_PILL_TOP_H // 2, HINT_PILL_TOP_H // 2)
        _draw_long_arc_progress(p, color, x, y, HINT_PILL_TOP_W,
                                HINT_PILL_TOP_H, "bottom", progress)
    f = p.font()
    f.setBold(True)
    f.setPixelSize(HINT_LETTER_PX)
    p.setFont(f)
    p.setPen(QColor(12, 14, 20))
    if side == "left":
        p.drawText(3, (h - HINT_PILL_W) // 2, HINT_PILL_W,
                   min(HINT_PILL_W, h - 4), Qt.AlignmentFlag.AlignCenter, hint)
    else:
        p.drawText(3, 1, HINT_PILL_TOP_W, HINT_PILL_TOP_H,
                   Qt.AlignmentFlag.AlignCenter, hint)
    p.restore()


class _FakeButton(QFrame):
    """一个虚拟按钮区：左侧长按钮 + 顶部短按钮 两种布局，共用同一 progress。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(120)
        self._progress = 0.0
        self._fired_at = 0.0   # 触发确认闪光的时刻（单调秒）
        self._hint_wide = "A"  # 长按钮用 A（左胶囊 + 右弧）
        self._hint_short = "B"  # 短按钮用 B（顶胶囊 + 下方点）
        self._mouse_hold = False

    def set_progress(self, prog: float, fired: bool) -> None:
        self._progress = prog
        if fired:
            self._fired_at = time.monotonic()
        self.update()

    def mousePressEvent(self, ev: QEvent) -> None:  # noqa: N802
        if ev.type() == QEvent.Type.MouseButtonPress and \
                ev.button() == Qt.MouseButton.LeftButton:  # type: ignore[attr-defined]
            self._mouse_hold = True

    def mouseReleaseEvent(self, ev: QEvent) -> None:  # noqa: N802
        self._mouse_hold = False

    def paintEvent(self, ev: QEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        prog = self._progress

        # ① 长按钮（左胶囊 + 右弧 5 颗点）
        wide_x, wide_y, wide_w, wide_h = 24, 28, 150, 36
        _draw_capsule(p, self._hint_wide, "left", wide_x, wide_y,
                      wide_w, wide_h, prog)
        # 长按钮标签
        p.setPen(QColor(223, 230, 240))
        p.setFont(QFont("Microsoft YaHei", 11))
        p.drawText(wide_x + HINT_PILL_W + 6, wide_y, wide_w,
                   wide_h, Qt.AlignmentFlag.AlignLeft
                   | Qt.AlignmentFlag.AlignVCenter, "锁帧 (A)")

        # ② 短按钮（顶胶囊 + 下方 3 颗点）
        short_x, short_y, short_w, short_h = 24, 78, 70, 28
        _draw_capsule(p, self._hint_short, "top", short_x, short_y,
                      short_w, short_h, prog)

        # 触发确认闪光：progress 到顶后 ~200ms 内，胶囊描一圈亮边
        since = time.monotonic() - self._fired_at
        if 0.0 <= since < 0.2:
            a = int(255 * (1.0 - since / 0.2))
            pen = QPen(QColor(255, 255, 255, a))
            pen.setWidth(2)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(wide_x - 2, wide_y - 2, wide_w + 4, wide_h + 4, 8, 8)
            p.drawRoundedRect(short_x - 2, short_y - 2, short_w + 4, short_h + 4, 8, 8)
        p.end()


class _Bar(QWidget):
    """底部进度条：按 pct(0..100) 从左侧填充，纯重绘、不抖布局。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(10)
        self._pct = 0

    def set_value(self, pct: int) -> None:
        self._pct = max(0, min(100, pct))
        self.update()

    def paintEvent(self, ev: QEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(27, 35, 48))
        p.drawRoundedRect(0, 0, w, h, 5, 5)
        if self._pct > 0:
            fw = max(0, int(w * self._pct / 100.0))
            grad = QColor(45, 111, 219) if self._pct < 100 else QColor(130, 255, 30)
            p.setBrush(grad)
            p.drawRoundedRect(0, 0, fw, h, 5, 5)
        p.end()


class _Demo(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("长按提示点消失原型 (spike)")
        self.resize(540, 360)
        self.setStyleSheet("background:#0c0f16; color:#dfe6f0;")

        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(16, 16, 16, 16)

        info = QLabel(
            "按住 = 鼠标左键点窗口按钮区 / 空格键 / 真手柄 A 或 Y 键。\n"
            "虚点随按住时长逐颗淡出；到 500ms 闪白边 = 触发确认。\n"
            "勾选「自动演示」自动循环。底部进度条 = progress。")
        info.setWordWrap(True)
        info.setStyleSheet("font-size:12px; color:#9fb0c4;")
        lay.addWidget(info)

        self._btn = _FakeButton()
        lay.addWidget(self._btn)

        self._auto = QCheckBox("自动演示（循环按住→松开）")
        self._auto.setStyleSheet("font-size:12px;")
        lay.addWidget(self._auto)

        self._bar = _Bar()
        lay.addWidget(self._bar)
        self._bar_label = QLabel("progress: 0%")
        self._bar_label.setStyleSheet("font-size:11px; color:#9fb0c4;")
        lay.addWidget(self._bar_label)

        # 状态
        self._holding_mouse = False
        self._holding_key = False
        self._hold_start = 0.0
        self._auto_hold_until = 0.0
        self._auto_phase = "idle"   # idle / holding / rest
        self._auto_next = time.monotonic()
        self._backend = None
        if _HAVE_REAL_PAD:
            try:
                self._backend = SDLGamepadBackend()
            except Exception:
                self._backend = None

        self._gp_hold = False
        self._last_poll = 0.0

        # 30ms 轮询（与生产手柄轮询同频），复用一趟 tick 推 progress，不新开高频定时器
        from PyQt6.QtCore import QTimer
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ---- 输入 ----
    def keyPressEvent(self, ev: QEvent) -> None:  # noqa: N802
        if ev.key() == Qt.Key.Key_Space:  # type: ignore[attr-defined]
            self._holding_key = True

    def keyReleaseEvent(self, ev: QEvent) -> None:  # noqa: N802
        if ev.key() == Qt.Key.Key_Space:  # type: ignore[attr-defined]
            self._holding_key = False

    def _poll_gamepad(self) -> bool:
        if self._backend is None:
            return False
        now = time.monotonic()
        if now - self._last_poll < 0.03:
            return self._gp_hold
        self._last_poll = now
        try:
            st = self._backend.poll()
            self._gp_hold = bool(st.connected and (st.a or st.y))
        except Exception:
            self._gp_hold = False
        return self._gp_hold

    # ---- 主循环：算 progress ----
    def _tick(self) -> None:
        now = time.monotonic()
        auto = self._auto.isChecked()

        # 自动演示状态机
        if auto:
            if self._auto_phase == "idle" and now >= self._auto_next:
                self._auto_phase = "holding"
                self._hold_start = now
            elif self._auto_phase == "holding" and \
                    (now - self._hold_start) >= (LONG_PRESS_MS / 1000.0) * 1.2:
                self._auto_phase = "rest"
                self._auto_next = now + 0.6  # 松开停顿
            elif self._auto_phase == "rest" and now >= self._auto_next:
                self._auto_phase = "idle"

        gp = self._poll_gamepad()
        mouse = self._btn._mouse_hold
        holding = self._holding_key or mouse or gp or \
            (auto and self._auto_phase == "holding")

        if holding:
            if self._hold_start == 0.0:
                self._hold_start = now
            elapsed = now - self._hold_start
            prog = min(1.0, elapsed / (LONG_PRESS_MS / 1000.0))
            fired = prog >= 1.0
            self._btn.set_progress(prog, fired)
        else:
            # 松开 → 复位（没真动作，点弹回全亮）
            self._hold_start = 0.0
            self._btn.set_progress(0.0, False)

        # 底部进度条
        pct = int(self._btn._progress * 100)
        self._bar_label.setText(f"progress: {pct}%")
        self._bar.set_value(pct)

    def closeEvent(self, ev: QEvent) -> None:  # noqa: N802
        if self._backend is not None:
            try:
                self._backend.close()
            except Exception:
                pass
        super().closeEvent(ev)


def main() -> int:
    app = QApplication(sys.argv)
    w = _Demo()
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
