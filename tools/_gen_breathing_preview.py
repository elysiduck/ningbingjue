# -*- coding: utf-8 -*-
"""呼吸灯辉光背景「候选效果图」生成器（只读，不修改任何 ningbingjue 源码）。

模拟 280x600 的 HUD 窗口在呼吸灯峰值 / 谷值下的背景辉光外观，输出 4 个方案的
静态 PNG + 一个 HTML 对比页。

渲染模型严格对齐 src/ningbingjue/ui/app_window.py：
  * 窗底色   self._bg = QColor(16,16,16,255)            （_theme_bg 默认）
  * 辉光色   默认 accent = (127, 209, 255)              （_breath_glow_color 兜底）
  * 圆角     addRoundedRect(0,0,w,h,16,16)
  * 托盘     矩形 (10,70,260,520)，先整块填充窗底色，再叠 26px 静态扫描线
            QLinearGradient(0,ty,0,ty+26) alpha 0 -> 80 -> 0
  * 呼吸相位 alpha a ∈ [0.2, 1.0]（core.timers.scanline_alpha）
  * 合成顺序 paintEvent: 窗底 -> 辉光层(setOpacity(a)) -> 托盘
只用标准库：手写 PNG 编码器（zlib + struct），无需 Pillow。
"""

from __future__ import annotations

import math
import os
import struct
import zlib

W, H = 280, 600
RADIUS = 16

BG = (16, 16, 16)              # self._bg
GLOW = (127, 209, 255)         # 默认呼吸灯色（冰蓝）
TRAY = (10, 70, 260, 520)      # 卡片托盘（窗口坐标）
TRAY_TOP, TRAY_LEFT = TRAY[1], TRAY[0]
TRAY_RIGHT = TRAY[0] + TRAY[2]
TRAY_BOTTOM = TRAY[1] + TRAY[3]

# 帧区（窗口 - 托盘）尺寸，与 _top_glow_band / _side_glow_bands 一致
TOP_BAND = 60                  # min(72, 70-10)
SIDE_LEFT = 10                 # min(12, tray.left())
SIDE_RIGHT = 9                 # min(12, W - tray.right() - 1)
BOTTOM_BAND = 9                # min(12, H - tray.bottom() - 1)

# 输出目录：ningbingjue/（本脚本位于 ningbingjue/tools/，取上一级）
OUT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else (hi if v > hi else v)


def mix(c1, c2, t):
    """c1 over c2? 不 —— t 是 c2 的占比：结果 = c1*(1-t) + c2*t。"""
    return (c1[0] + (c2[0] - c1[0]) * t,
            c1[1] + (c2[1] - c1[1]) * t,
            c1[2] + (c2[2] - c1[2]) * t)


def rounded_sd(px, py, x0, y0, x1, y1, r):
    """圆角矩形的**带符号距离**（内部为负，路径上为 0，外部为正）。

    标准 rounded-box SDF：q = |p-c| - (half - r)
    """
    cx, cy = (x0 + x1) * 0.5, (y0 + y1) * 0.5
    hx, hy = (x1 - x0) * 0.5, (y1 - y0) * 0.5
    qx = abs(px - cx) - (hx - r)
    qy = abs(py - cy) - (hy - r)
    mx, my = max(qx, 0.0), max(qy, 0.0)
    return math.hypot(mx, my) + min(max(qx, qy), 0.0) - r


def stroke_cov(sd, width):
    """Qt 描边的抗锯齿覆盖率：笔宽 width、居中于路径。"""
    return clamp(width * 0.5 - abs(sd) + 0.5, 0.0, 1.0)


def over(dst, src, a):
    """source-over，a 已含覆盖率。"""
    return mix(dst, src, a)


# --------------------------------------------------------------------------
# 托盘（所有方案共用，静态、不随呼吸变化 —— 与 _paint_tray 一致）
# --------------------------------------------------------------------------
def paint_tray(col, px, py, glow, tray_fill_alpha):
    """托盘底色（tray_fill_alpha=1.0 表示当前那种完全不透明、辉光进不去）
    + 26px 静态扫描线。"""
    if TRAY_LEFT <= px < TRAY_RIGHT and TRAY_TOP <= py < TRAY_BOTTOM:
        col = over(col, BG, tray_fill_alpha)
        band = py - TRAY_TOP
        if band < 26:
            t = band / 26.0
            a = (80 / 255.0) * (t / 0.5 if t < 0.5 else (1.0 - t) / 0.5)
            col = over(col, glow, clamp(a))
    return col


def tray_border(col, px, py):
    """托盘 2px 白色描边（alpha 110），仅在托盘外侧一圈可见。"""
    sd = rounded_sd(px, py, TRAY_LEFT, TRAY_TOP, TRAY_RIGHT - 1, TRAY_BOTTOM - 1, 14)
    cov = stroke_cov(sd, 2.0)
    if cov > 0 and sd > -2.5:
        col = over(col, (255, 255, 255), cov * (110 / 255.0) * 0.55)
    return col


# --------------------------------------------------------------------------
# 方案 A —— 径向渐变（以窗口中心为圆心，由内向外平滑提升，边缘一圈恒定高亮）
# --------------------------------------------------------------------------
A_CORE, A_EDGE, A_SAT, A_POW = 0.06, 0.52, 1.15, 1.4


def field_A(px, py):
    r = math.hypot((px - W / 2.0) / (W / 2.0), (py - H / 2.0) / (H / 2.0))
    t = clamp(r / A_SAT) ** A_POW
    return A_CORE + (A_EDGE - A_CORE) * t


# --------------------------------------------------------------------------
# 方案 B —— 边缘提亮 + 向内均匀衰减（矩形等距场，中心留底不过暗）
# --------------------------------------------------------------------------
B_CORE, B_EDGE, B_DEPTH, B_POW = 0.14, 0.58, 110.0, 1.15


def field_B(px, py):
    d = min(px, py, W - 1.0 - px, H - 1.0 - py)
    t = clamp(1.0 - d / B_DEPTH) ** B_POW
    return B_CORE + (B_EDGE - B_CORE) * t


# --------------------------------------------------------------------------
# 方案 C —— 顶部/侧边扫描线式辉光（只画在「窗口-托盘」框内，零额外开销）
#            + 整窗静态底光（一次性烘焙，抹平托盘接缝，中间不再纯黑）
# --------------------------------------------------------------------------
C_CORE, C_EDGE, C_DEPTH, C_POW = 0.04, 0.30, 130.0, 1.1
C_TOP_PEAK, C_SIDE_PEAK = 0.46, 0.40


def field_C_static(px, py):
    """整窗静态底光：不随呼吸变化，只烘焙一次。"""
    d = min(px, py, W - 1.0 - px, H - 1.0 - py)
    t = clamp(1.0 - d / C_DEPTH) ** C_POW
    return C_CORE + (C_EDGE - C_CORE) * t


def field_C_breath(px, py):
    """呼吸部分：严格限制在「窗口 - 托盘」框内，进入托盘前衰减到 0。"""
    a = 0.0
    if py < TOP_BAND:
        t = py / float(TOP_BAND)
        a = max(a, C_TOP_PEAK * clamp(1.0 - t) ** 1.6)
    if px < SIDE_LEFT:
        a = max(a, C_SIDE_PEAK * clamp(1.0 - px / float(SIDE_LEFT)) ** 1.4)
    if px >= W - SIDE_RIGHT:
        a = max(a, C_SIDE_PEAK * clamp(1.0 - (W - 1.0 - px) / float(SIDE_RIGHT)) ** 1.4)
    if py >= H - BOTTOM_BAND:
        a = max(a, C_SIDE_PEAK * clamp(1.0 - (H - 1.0 - py) / float(BOTTOM_BAND)) ** 1.4)
    return a


# --------------------------------------------------------------------------
# 方案 D —— 当前实现（对照）：顶部带 + 左右/底部窄边 + 4 层霓虹描边
# --------------------------------------------------------------------------
def field_D(px, py):
    a = 0.0
    # 顶部辉光带（含上/下圆角裁切）：垂直渐变 0 -> 120@22% -> 0
    if rounded_sd(px, py, 0, 0, W, TOP_BAND, RADIUS) <= 0:
        t = py / float(TOP_BAND)
        a = max(a, (120 / 255.0) * (t / 0.22 if t < 0.22 else (1.0 - t) / 0.78))
    # 左右 / 底部窄边
    if px < SIDE_LEFT:
        a = max(a, (95 / 255.0) * clamp(1.0 - px / float(SIDE_LEFT)))
    if px >= W - SIDE_RIGHT:
        a = max(a, (95 / 255.0) * clamp(1.0 - (W - 1.0 - px) / float(SIDE_RIGHT)))
    if py >= H - BOTTOM_BAND:
        a = max(a, (95 / 255.0) * clamp(1.0 - (H - 1.0 - py) / float(BOTTOM_BAND)))
    return a


def rim_D(sd):
    """当前实现的霓虹边框：3/2/1px 三层描边(alpha 34*i) + 1.2px 不透明细线。"""
    a = 0.0
    for width, alpha in ((3.0, 102 / 255.0), (2.0, 68 / 255.0),
                         (1.0, 34 / 255.0), (1.2, 1.0)):
        cov = stroke_cov(sd, width)
        if cov > 0:
            a = a + (1.0 - a) * alpha * cov
    return a


# --------------------------------------------------------------------------
# 逐方案渲染
# --------------------------------------------------------------------------
SCHEMES = {
    "A": dict(tray_alpha=0.72, rim_w=2.0, rim_a=0.85),
    "B": dict(tray_alpha=0.72, rim_w=2.5, rim_a=0.90),
    "C": dict(tray_alpha=0.55, rim_w=2.5, rim_a=0.90),
    "D": dict(tray_alpha=1.00, rim_w=0.0, rim_a=0.00),
}


def render(scheme: str, breath: float) -> bytes:
    """返回 RGB 行数据拼接后的 bytes（每行 W*3 字节）。"""
    cfg = SCHEMES[scheme]
    tray_alpha = cfg["tray_alpha"]
    rim_w, rim_a = cfg["rim_w"], cfg["rim_a"]
    glow = GLOW

    # 预计算：桌面背景（带一点垂直渐变 + 窗口投影）
    rows = []
    for py in range(H):
        row = bytearray()
        fy = py / (H - 1.0)
        desk = (34 + (22 - 34) * fy, 36 + (24 - 36) * fy, 44 + (30 - 44) * fy)
        for px in range(W):
            cx, cy = px + 0.5, py + 0.5
            sd = rounded_sd(cx, cy, 0, 0, W, H, RADIUS)
            win = clamp(0.5 - sd, 0.0, 1.0)

            # --- 桌面 + 投影 ---
            if sd > 0:
                shade = clamp(1.0 - sd / 10.0)
                base = mix(desk, (6, 7, 10), 0.55 * shade)
            else:
                base = desk

            # --- 窗底 ---
            col = BG

            # --- 辉光层（呼吸调制）---
            if scheme == "A":
                g = field_A(cx, cy)
            elif scheme == "B":
                g = field_B(cx, cy)
            elif scheme == "C":
                g = field_C_static(cx, cy) + field_C_breath(cx, cy) * breath
            else:
                g = field_D(cx, cy)
            if scheme != "C":
                g = g * breath
            col = over(col, glow, clamp(g))

            # --- 边缘恒亮描边 ---
            if rim_w > 0:
                col = over(col, glow, clamp(rim_a * stroke_cov(sd, rim_w) * breath))
            else:
                col = over(col, glow, clamp(rim_D(sd) * breath))

            # --- 托盘（画在辉光之上）---
            col = paint_tray(col, px, py, glow, tray_alpha)
            col = tray_border(col, cx, cy)

            # --- 合成到桌面 ---
            col = mix(base, col, win)
            row += bytes((int(round(col[0])), int(round(col[1])), int(round(col[2]))))
        rows.append(bytes(row))
    return rows


# --------------------------------------------------------------------------
# 极简 PNG 编码器（标准库）
# --------------------------------------------------------------------------
def write_png(path: str, rows) -> None:
    raw = b"".join(b"\x00" + r for r in rows)
    comp = zlib.compress(raw, 9)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", comp)
    png += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


def main() -> None:
    made = []
    for scheme in ("A", "B", "C", "D"):
        for tag, breath in (("", 1.0), ("_dim", 0.2)):
            rows = render(scheme, breath)
            name = f"breathing_effect_{scheme}{tag}.png"
            path = os.path.join(OUT_DIR, name)
            write_png(path, rows)
            made.append(name)
            print("wrote", path)
    print("\n".join(made))


if __name__ == "__main__":
    main()
