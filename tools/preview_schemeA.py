"""离线渲染「方案 A」呼吸灯实际效果预览（复刻 app_window 的绘制序列）。

输出：
  breathing_schemeA_peak.png  呼吸峰值 (a=1.0)
  breathing_schemeA_dim.png   呼吸谷值 (a=0.2)

绘制顺序与 app_window.paintEvent 完全一致（2026-08-29 方案 A 定稿）：
  1. 窗口底色 _bg
  2. 托盘：_bg 不透明底 → 托盘色 #0e1117 @ 219
  3. 全窗径向辉光 + 霓虹边框（画在托盘**之上** → 跨托盘边界连续、无接缝）
  4. 托盘白边（最后画，保持锐利）
  5. 示意卡片（card_fill #141821 @ 165）—— 子控件在 paintEvent 之后绘制

注意：不创建 QApplication（offscreen 平台在离屏绘制时会 C 级硬崩，退出码 127），
直接用 QPainter 画到 QImage。
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP_WINDOW = os.path.join(ROOT, "src", "ningbingjue", "ui", "app_window.py")

from PyQt6.QtCore import QRect, Qt  # noqa: E402
from PyQt6.QtGui import (QColor, QImage, QLinearGradient, QPainter,  # noqa: E402
                         QPainterPath, QPen, QRadialGradient)

W, H = 300, 620
RADIUS = 16
TRAY = QRect(10, 78, W - 20, H - 88)
CARD_H = 76
CARD_GAP = 8


def _load_consts():
    """从 app_window.py 里读出方案 A 的常量，保证预览与实现同源。"""
    with open(APP_WINDOW, "r", encoding="utf-8") as fh:
        text = fh.read()
    out = {}
    for name in ("_RADIAL_CORE_A", "_RADIAL_MID_A", "_RADIAL_EDGE_A",
                 "_RADIAL_RADIUS_K"):
        m = re.search(rf"^{name}\s*=\s*([0-9.]+)", text, re.M)
        if not m:
            raise SystemExit(f"constant {name} not found in app_window.py")
        out[name] = float(m.group(1))
    return out


C = _load_consts()


def _radial(p, a, gr, gg, gb, edge_alpha, clip=None):
    """复刻 _paint_radial_glow。"""
    cx, cy = W / 2.0, H / 2.0
    radius = max(W, H) * C["_RADIAL_RADIUS_K"]
    k = edge_alpha / C["_RADIAL_EDGE_A"] if C["_RADIAL_EDGE_A"] else 1.0
    g = QRadialGradient(cx, cy, radius)
    g.setColorAt(0.0, QColor(gr, gg, gb, int(C["_RADIAL_CORE_A"] * a * k)))
    g.setColorAt(0.55, QColor(gr, gg, gb, int(C["_RADIAL_MID_A"] * a * k)))
    g.setColorAt(1.0, QColor(gr, gg, gb, int(edge_alpha * a)))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(g)
    if clip is not None:
        p.save()
        p.setClipPath(clip)
    p.drawRoundedRect(0, 0, W, H, RADIUS, RADIUS)
    if clip is not None:
        p.restore()


def paint_frame(a: float, glow: QColor, path_out: str,
                editor: bool = False, top_fade: bool = False,
                border_alpha: int = 110, exclude_tray: bool = False) -> None:
    img = QImage(W, H, QImage.Format.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    win = QPainterPath()
    win.addRoundedRect(0, 0, W, H, RADIUS, RADIUS)

    # 1. 窗口底色
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(16, 16, 16, 235))
    p.drawPath(win)

    # 2. 托盘（底 + 托盘色）
    gr, gg, gb = glow.red(), glow.green(), glow.blue()
    tray_path = QPainterPath()
    tray_path.addRoundedRect(TRAY.x(), TRAY.y(), TRAY.width(), TRAY.height(),
                             14, 14)
    p.save()
    p.setClipPath(tray_path)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(16, 16, 16, 235))
    p.drawRoundedRect(TRAY.x(), TRAY.y(), TRAY.width(), TRAY.height(), 14, 14)
    p.setBrush(QColor(14, 17, 23, 219))
    p.drawRoundedRect(TRAY.x(), TRAY.y(), TRAY.width(), TRAY.height(), 14, 14)
    p.restore()

    # 3. 全窗径向辉光（画在托盘之上 → 连续无接缝）
    _radial(p, a, gr, gg, gb, C["_RADIAL_EDGE_A"], None)

    # 3a. 顶部衰减（可选）：用户反馈径向辉光在托盘上沿形成一条明显亮带。
    # 用 DestinationIn 模式从窗口顶边往下做一个二次 fade-in，把顶部辉光压暗，
    # 霓虹边框与托盘白边在后续绘制，因此保持锐利。
    if top_fade:
        fade_end = int(min(H * 0.35, TRAY.y() * 2.5))
        if fade_end > TRAY.y():
            p.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_DestinationIn)
            grad = QLinearGradient(0, 0, 0, fade_end)
            grad.setColorAt(0.0, QColor(0, 0, 0, 0))
            grad.setColorAt(0.5, QColor(0, 0, 0, int(255 * 0.25)))
            grad.setColorAt(1.0, QColor(0, 0, 0, 255))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(grad)
            p.drawRect(0, 0, W, fade_end)
            p.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_SourceOver)

    # 3b. 霓虹边框
    p.setBrush(Qt.BrushStyle.NoBrush)
    for i in range(3, 0, -1):
        pen = QPen(QColor(gr, gg, gb, int(34 * i * a)))
        pen.setWidth(i)
        p.setPen(pen)
        p.drawPath(win)
    p.setPen(QPen(glow, 1.2))
    p.drawPath(win)

    # 3c. 方案 B：辉光不覆盖托盘 → 在窗口-level 辉光之上重新铺一层不透明托盘底，
    #     把托盘内部（=卡片区，靠近窗口中心）的呼吸辉光抹掉，只有窗口边框外的窄环在呼吸。
    if exclude_tray:
        p.save()
        p.setClipPath(tray_path)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(16, 16, 16, 235))
        p.drawRoundedRect(TRAY.x(), TRAY.y(), TRAY.width(), TRAY.height(), 14, 14)
        p.setBrush(QColor(14, 17, 23, 219))
        p.drawRoundedRect(TRAY.x(), TRAY.y(), TRAY.width(), TRAY.height(), 14, 14)
        p.restore()

    # 4. 托盘白边
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, border_alpha), 2))
    p.drawRoundedRect(TRAY.x(), TRAY.y(), TRAY.width() - 1,
                      TRAY.height() - 1, 14, 14)

    # 4b. 编辑态深色蒙层（画在辉光**之上**，保证编辑内容对比度）
    if editor:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 118))
        p.drawRoundedRect(TRAY.x(), TRAY.y(), TRAY.width(), TRAY.height(),
                          14, 14)

    # 5. 示意卡片（子控件：在 paintEvent 之后绘制，故在最上层）
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(20, 24, 33, 165))
    y = TRAY.y() + 10
    while y + CARD_H <= TRAY.bottom() - 10:
        p.drawRoundedRect(TRAY.x() + 8, y, TRAY.width() - 16, CARD_H, 12, 12)
        y += CARD_H + CARD_GAP

    p.end()
    if not img.save(path_out):
        raise SystemExit(f"failed to save {path_out}")


def main() -> None:
    glow = QColor("#7fd1ff")
    peak = os.path.join(ROOT, "breathing_schemeA_peak.png")
    dim = os.path.join(ROOT, "breathing_schemeA_dim.png")
    paint_frame(1.0, glow, peak)
    paint_frame(0.2, glow, dim)
    edit = os.path.join(ROOT, "breathing_schemeA_editor.png")
    paint_frame(1.0, glow, edit, editor=True)
    peak_fade = os.path.join(ROOT, "breathing_schemeA_peak_topfade.png")
    paint_frame(1.0, glow, peak_fade, top_fade=True)
    dim_fade = os.path.join(ROOT, "breathing_schemeA_dim_topfade.png")
    paint_frame(0.2, glow, dim_fade, top_fade=True)
    peak_thin = os.path.join(ROOT, "breathing_schemeA_peak_thinborder.png")
    paint_frame( 1.0, glow, peak_thin, border_alpha=55)
    dim_thin = os.path.join(ROOT, "breathing_schemeA_dim_thinborder.png")
    paint_frame(0.2, glow, dim_thin, border_alpha=55)
    # 方案 B：辉光不覆盖托盘（卡片区不呼吸，仅窗口外环呼吸）
    out_peak = os.path.join(ROOT, "breathing_schemeA_glowoutside_peak.png")
    paint_frame(1.0, glow, out_peak, exclude_tray=True)
    out_dim = os.path.join(ROOT, "breathing_schemeA_glowoutside_dim.png")
    paint_frame(0.2, glow, out_dim, exclude_tray=True)
    print("peak:", peak)
    print("dim :", dim)
    print("editor:", edit)
    print("peak_topfade:", peak_fade)
    print("dim_topfade :", dim_fade)
    print("peak_thinborder:", peak_thin)
    print("dim_thinborder :", dim_thin)
    print("glowoutside_peak:", out_peak)
    print("glowoutside_dim :", out_dim)
    print("consts:", C)


if __name__ == "__main__":
    main()
