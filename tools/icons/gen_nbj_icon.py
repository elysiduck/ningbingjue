#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate tools/icons/nbj_icon.ico — the frozen .exe resource icon.

凝冰结魄的托盘图标是代码绘制的全息冰块立方体（见
src/ningbingjue/ui/app_window.py:_draw_ice_cube / _make_tray_icon）。本脚本
把**同一份绘制逻辑**渲染成多分辨率 PNG，再拼进一个 ICO 容器，供
build_exe.py 的 PyInstaller --icon 使用，使冻结产物的 .exe 文件图标 / 任务栏
fallback 也变成冰块（运行时窗口图标由 setWindowIcon 覆盖，本 .ico 是静态回退）。

注意：绘制逻辑刻意与 app_window._draw_ice_cube 同源。若冰块的几何/配色
被修改，请同步本文件（构建期工具，无法 import app_window 的全部依赖）。

依赖：仅 PyQt6（项目 bundled runtime 自带）+ 标准库 struct。
产物：tools/icons/nbj_icon.ico（含 16/24/32/40/48/64/72/96/128/256 共 10 档）。
"""
from __future__ import annotations

import os
import struct

from PyQt6.QtCore import Qt, QPointF
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)


# --------------------------------------------------------------------------
# 与 app_window.py:_palette_from_accent / _draw_ice_cube 同源
# --------------------------------------------------------------------------
def _palette_from_accent(accent: QColor) -> dict:
    h, s, l, _ = accent.getHsl()

    def _make(deg=0, light_mul=1.0) -> QColor:
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


def _draw_ice_cube(p: QPainter, size: int, accent: QColor) -> None:
    """在 size×size 透明画布上绘制全息冰块立方体（详见 app_window._draw_ice_cube）。"""
    k = size / 64.0
    p.save()
    p.scale(k, k)

    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(20, 24, 33))
    p.drawRoundedRect(5, 5, 54, 54, 14, 14)
    p.setPen(QPen(accent, 2.4))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(5, 5, 54, 54, 14, 14)

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

    g_left = QLinearGradient(cx - w, 0, cx, 0)
    g_left.setColorAt(0.0, pal["accent_dark"])
    g_left.setColorAt(1.0, pal["accent"])
    g_right = QLinearGradient(cx, 0, cx + w, 0)
    g_right.setColorAt(0.0, pal["comp_lit"])
    g_right.setColorAt(1.0, pal["comp_dark"])
    g_top = QLinearGradient(cx - w, 0, cx + w, 0)
    g_top.setColorAt(0.0, pal["accent_lit"])
    g_top.setColorAt(0.5, pal["comp"])
    g_top.setColorAt(1.0, pal["comp_lit"])

    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(g_right)); p.drawPolygon(right_face)
    p.setBrush(QBrush(g_left));  p.drawPolygon(left_face)
    p.setBrush(QBrush(g_top));   p.drawPolygon(top_face)

    edge = QPen(QColor(255, 255, 255, 240), 1.4)
    edge.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(edge)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPolygon(top_face)
    p.drawPolygon(left_face)
    p.drawPolygon(right_face)

    p.restore()


def _render_png_bytes(size: int, accent: QColor) -> bytes:
    """渲染单档位图并返回 PNG 字节（用于塞进 ICO）。

    注意：本 bundled PyQt6 下 ``QImage.save(QBuffer, "PNG")`` 会硬崩（进程
    直接退出码 127，连 try/except 都拦不住）；改为先存临时文件再读回字节，
    与已验证可用的 pm.save(path, "PNG") 路径一致。
    """
    import os
    import tempfile
    pm = QPixmap(size, size)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    _draw_ice_cube(p, size, accent)
    p.end()
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        pm.save(path, "PNG")
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _build_ico(sizes, accent: QColor, out_path: str) -> None:
    pngs = []
    for sz in sizes:
        pngs.append((sz, _render_png_bytes(sz, accent)))

    # ICONDIR
    count = len(pngs)
    header = struct.pack("<HHH", 0, 1, count)
    entries = b""
    offset = 6 + count * 16
    imagedata = b""
    for sz, data in pngs:
        w = sz & 0xFF
        h = sz & 0xFF
        # ICONDIRENTRY
        entries += struct.pack(
            "<BBBBHHII",
            w,            # bWidth
            h,            # bHeight
            0,            # bColorCount (0 = >256 colors / PNG)
            0,            # bReserved
            1,            # wPlanes
            32,           # wBitCount
            len(data),    # dwBytesInRes
            offset,       # dwImageOffset
        )
        imagedata += data
        offset += len(data)

    with open(out_path, "wb") as f:
        f.write(header)
        f.write(entries)
        f.write(imagedata)


def main() -> int:
    # offscreen 下 QPainter 需要存在一个 QApplication 实例，否则进程硬崩（退出码 127）。
    from PyQt6.QtWidgets import QApplication
    QApplication([])

    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "nbj_icon.ico")
    accent = QColor(127, 209, 255)  # neon cyan 默认；运行时会随呼吸灯变，但 .ico 是静态回退用默认色
    sizes = [16, 24, 32, 40, 48, 64, 72, 96, 128, 256]
    _build_ico(sizes, accent, out)
    print(f"[gen_nbj_icon] wrote {out} ({len(sizes)} sizes: {sizes})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
