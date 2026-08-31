"""Gamepad ABXY hint badge — shared rendering + ``HintButton``.

Mirrors the original ``RoundedButton.GamepadHint``: a small coloured disc
(A绿 / B红 / X蓝 / Y黄) carrying the letter, drawn in a control's corner.  It is
used by **both** the HUD ``RoundedButton`` and the in-window editor's plain
buttons so *every* shortcut is shown in gamepad style — never as keyboard text.

A single palette lives here so the badge looks identical everywhere (HUD,
editor overlay, focus rectangle).
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QSize, Qt
from PyQt6.QtGui import (QColor, QFont, QFontMetrics, QLinearGradient,
                         QPainter, QPainterPath, QPen)
from PyQt6.QtWidgets import QPushButton, QSizePolicy, QWidget

# Gamepad ABXY badge colours (A绿 / B红 / X蓝 / Y黄) — matches the original's
# RoundedButton::SetHintColor convention.
HINT_COLORS = {
    "A": QColor(130, 255, 30),
    "B": QColor(230, 75, 90),
    "X": QColor(95, 175, 235),
    "Y": QColor(245, 215, 75),
}

# Horizontal space reserved for an inline left-side badge.  Radius 9 + margin 5
# + 1px backing + a little breathing room → 24.  Controls that draw their own
# centred text should inset the text rect by this amount so the badge and label
# never overlap.
BADGE_WIDTH = 24

# 按按钮宽度分流：宽度达到此阈值即视为「长按钮」。长按钮的提示胶囊画在
# 左侧；短按钮画在顶部。
HINT_WIDE_THRESHOLD = 96

# HintButton 专用：低于此宽度的按钮视为「短按钮」，提示胶囊贴在按钮上沿
# （内部顶部，文字下移），而非左侧。比 HINT_WIDE_THRESHOLD 略大，确保
# 锁帧:关 / 自动锁帧:关 这类按钮稳定走顶部胶囊。
HINT_TOP_W = 112

# 左侧胶囊（长按钮）预留给文字的左内边距，避免胶囊压住文字。
LEFT_CAP_W = 26

# 右侧胶囊（长按钮）预留给文字的右内边距（Y 提示放在进程按钮右边时使用）。
RIGHT_CAP_W = 26

# 顶部胶囊尺寸（仅用于短按钮、且完全在按钮内部，不超出边界）
# 短按钮最小高度已提到 28px，胶囊高 12 既贴合字母（11px）又不压住下方文字。
CAPSULE_TOP_MARGIN = 1     # 胶囊贴按钮顶边（仅留 1px 缝隙）
# 顶部胶囊（仅用于短按钮、且完全在按钮内部，不超出边界）
# 高度贴合字母（12px 字母需 ≥12px 高胶囊才不溢出），故比旧值 9 放大到 12；
# 仍受按钮最小高度约束（见 HintButton.setMinimumHeight），绝不超出按钮高度。
CAPSULE_INNER_HEIGHT = 12   # 顶部胶囊实际高度（贴合字母、不超按钮高度）

# 自绘按钮内部文字留白（避免文字贴边，看起来像被压扁）
BUTTON_PAD_X = 8
BUTTON_PAD_Y = 3

# HintButton 的基线字号（px）——所有由 HintButton 自己生成的样式表（默认外观、
# 预设、预设激活/未激活变体）都必须显式写回这个值。
#
# 为什么必须显式写：run.py:_freeze_application_font() 把 QApplication 的全局
# 字体锁成了 16px（为绕开 Windows「辅助功能 → 文本大小」缩放）。而 QSS 有一条
# 反直觉的规则——**样式表里不含 font-size 时，控件就回落到 QApplication 全局
# 字体**，不是「继承上一次的字号」。于是任何漏写 font-size 的样式表都会让按钮
# 文字从 13px 悄悄变成 16px（+23%），表现为「套了按钮预设后字比默认按钮大」。
# 见 core/color/styles.py:_font_size_qss（预设样式表同样靠这个常量兜底）。
HINT_BUTTON_FONT_PX = 13

# 手柄提示胶囊（ABXY 徽章）统一尺寸：圆点略放大、内部字母缩小，让字母在
# 胶囊内有呼吸余量、不再显得过大（原左/右胶囊 letter=16px 塞进 16px 圆点 →
# 视觉上「内容过大」溢出）。HINT_PILL_W 是两用的圆点宽 / 高；HINT_PILL_TOP_W /
# HINT_PILL_TOP_H 是短按钮顶部胶囊的宽 / 高。HINT_LETTER_PX 是胶囊内字母字号。
HINT_PILL_W = 20
HINT_PILL_TOP_W = 24
HINT_PILL_TOP_H = 13
HINT_LETTER_PX = 12

# -- 长按标记（拖尾式）----------------------------------------------------
# 短按 = 纯胶囊；长按 = 胶囊右侧再拖出一条**向右渐隐**的横条，读作「持续按住」。
# 选它是因为：① 只有一条实心横块，13px 高的顶部胶囊上也糊不掉；
# ② 不改动胶囊本体尺寸/位置，短按徽章的既有几何完全不受影响。
# 代价：徽章整体占宽 20 → 20 + 1 + 14 = 35px（左侧/顶部摆放时需注意避让文字）。
HINT_TAIL_W = 14          # 拖尾长度
HINT_TAIL_RATIO = 0.5     # 拖尾高度 = 胶囊高 × 该比例
HINT_TAIL_MIN_H = 4       # 拖尾高度下限（顶部胶囊 13px 高时 = 6px）

# -- 长按标记（虚点弧，取代拖尾式）------------------------------------------
# 长按钮：在胶囊右缘画一段「90° 虚点弧」，圆点沿弧包裹胶囊右缘，读作「按住不放」。
# 比拖尾式横向更省、更精致，且天然「竖排包裹」手柄提示。弧以胶囊中心为圆心、
# 半径 = 胶囊外接半径 + HINT_ARC_GAP，扫过 ±45°（最右点向上下各铺 45°，共 90°）。
HINT_ARC_DOTS = 5        # 弧上圆点数量
HINT_ARC_GAP = 3         # 弧半径相对胶囊外接半径的外扩（px）——越小圆点越贴近胶囊
HINT_ARC_DOT_R = 1.7     # 单颗圆点半径（px）
HINT_ARC_W = 6           # 长按弧相对胶囊右缘额外占用的水平宽度（用于预留文字）
# ④-C 短按钮胶囊下方虚点（横排，不占用按钮横向空间）。
HINT_BOTTOM_DOTS = 3     # 胶囊下方虚点数量
HINT_BOTTOM_GAP = 5      # 相邻虚点间距（px）
HINT_BOTTOM_DY = 4       # 虚点相对胶囊下缘的纵向偏移（px）


def _draw_long_tail(painter: QPainter, color: QColor, x: int, y: int,
                    w: int, h: int, to_right: bool = True) -> None:
    """在胶囊右侧（``to_right``）或左侧拖一条渐隐横条，表达「长按」。

    画在胶囊**外面**，与胶囊本体留 1px 缝；先铺一层比拖尾大 1px 的深色底衬，
    保证在浅色/亮色按钮底色（如保存按钮的 #82ff1e）上也抠得出来。
    """
    tw = HINT_TAIL_W
    th = max(HINT_TAIL_MIN_H, int(round(h * HINT_TAIL_RATIO)))
    ty = y + (h - th) // 2
    tx = (x + w + 1) if to_right else (x - 1 - tw)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    # 深色底衬（比拖尾各大 1px）
    painter.setBrush(QColor(0, 0, 0, 160))
    painter.drawRoundedRect(tx - 1, ty - 1, tw + 2, th + 2,
                            (th + 2) // 2, (th + 2) // 2)
    # 渐隐拖尾：靠近胶囊一端不透明，末端全透明
    g = QLinearGradient(QPointF(tx, 0.0), QPointF(tx + tw, 0.0))
    fade = QColor(color)
    fade.setAlpha(0)
    if to_right:
        g.setColorAt(0.0, QColor(color))
        g.setColorAt(1.0, fade)
    else:
        g.setColorAt(0.0, fade)
        g.setColorAt(1.0, QColor(color))
    painter.setBrush(g)
    painter.drawRoundedRect(tx, ty, tw, th, th // 2, th // 2)
    painter.restore()


def _draw_inner_dot(painter: QPainter, color: QColor, cx: float, cy: float,
                    r: float) -> None:
    """④-A：常驻说明条（GamepadHintBar）用「胶囊右侧留白圆点」表示长按。

    圆点画在**加宽后胶囊的右侧留白区**（见 ``GamepadHintBar`` 的
    ``_pill_extra``），中心绝不与居中的 Y/X 字母重叠——旧实现把圆点画在
    胶囊正中心，会压住字母（用户反馈 Y/X 被遮挡）。与 ④-B/④-C 的外置
    虚点弧/下方虚点区分：此圆点是「平时就亮着」的常驻标记。
    """
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(0, 0, 0, 170))
    painter.drawEllipse(int(cx - r - 1), int(cy - r - 1),
                        int(2 * (r + 1)), int(2 * (r + 1)))
    painter.setBrush(QColor(color))
    painter.drawEllipse(int(cx - r), int(cy - r), int(2 * r), int(2 * r))
    painter.restore()


def _draw_long_arc(painter: QPainter, color: QColor, x: int, y: int,
                   w: int, h: int, side: str = "right",
                   hold_progress: float = 0.0) -> None:
    """长按标记（虚点），随 ``hold_progress``(0→1) 逐颗淡出。

    - ``hold_progress = 0`` → 全部亮（刚按下 / 静止等待）；``= 1`` → 全部灭
      （长按已触发）。每颗点 ``i``（0-based）的 alpha = ``clamp(1 - (progress*n
      - i), 0, 1)``，故 progress 增大时点从首颗起**逐个消失**，像进度条倒计。
    - ``side="right"`` / ``side="left"``：以胶囊中心为圆心、半径 = 胶囊外接半径
      + ``HINT_ARC_GAP``，扫过 ±45°（共 90°）的**虚点弧**，竖排包裹胶囊右/左缘
      （④-B，长按钮用）。
    - ``side="bottom"``：在胶囊**正下方**横排 ``HINT_BOTTOM_DOTS`` 颗虚点
      （④-C，短按钮用），不占按钮横向空间。
    """
    prog = max(0.0, min(1.0, float(hold_progress)))
    n_base = HINT_BOTTOM_DOTS if side == "bottom" else HINT_ARC_DOTS

    def _alpha(i: int, n: int) -> float:
        a = 1.0 - (prog * n - i)
        return 0.0 if a <= 0.0 else (1.0 if a >= 1.0 else a)

    if side == "bottom":
        # ④-C：胶囊下方横排虚点。
        use_n = HINT_BOTTOM_DOTS
        total = (use_n - 1) * HINT_BOTTOM_GAP
        sx = x + w / 2.0 - total / 2.0
        py = y + h + HINT_BOTTOM_DY
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(use_n):
            a = _alpha(i, use_n)
            if a <= 0.0:
                continue
            px = sx + i * HINT_BOTTOM_GAP
            painter.setBrush(QColor(0, 0, 0, int(160 * a)))
            painter.drawEllipse(int(px - HINT_ARC_DOT_R - 1),
                                int(py - HINT_ARC_DOT_R - 1),
                                int(2 * (HINT_ARC_DOT_R + 1)),
                                int(2 * (HINT_ARC_DOT_R + 1)))
            c = QColor(color)
            c.setAlpha(int(255 * a))
            painter.setBrush(c)
            painter.drawEllipse(int(px - HINT_ARC_DOT_R),
                                int(py - HINT_ARC_DOT_R),
                                int(2 * HINT_ARC_DOT_R),
                                int(2 * HINT_ARC_DOT_R))
        painter.restore()
        return

    cx = x + w / 2.0
    cy = y + h / 2.0
    base_r = max(w, h) / 2.0
    R = base_r + HINT_ARC_GAP
    n = HINT_ARC_DOTS
    start = 0.0 if side == "right" else 180.0   # 3 点钟（右）/ 9 点钟（左）
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    for i in range(n):
        a = _alpha(i, n)
        if a <= 0.0:
            continue
        deg = start + (-45.0 + 90.0 * i / (n - 1))   # -45°..+45° 均布
        rad = math.radians(deg)
        px = cx + R * math.cos(rad)
        py = cy + R * math.sin(rad)
        # 深色底衬（比圆点各大 1px）
        painter.setBrush(QColor(0, 0, 0, int(160 * a)))
        painter.drawEllipse(int(px - HINT_ARC_DOT_R - 1),
                            int(py - HINT_ARC_DOT_R - 1),
                            int(2 * (HINT_ARC_DOT_R + 1)),
                            int(2 * (HINT_ARC_DOT_R + 1)))
        # 彩色圆点
        c = QColor(color)
        c.setAlpha(int(255 * a))
        painter.setBrush(c)
        painter.drawEllipse(int(px - HINT_ARC_DOT_R),
                            int(py - HINT_ARC_DOT_R),
                            int(2 * HINT_ARC_DOT_R),
                            int(2 * HINT_ARC_DOT_R))
    painter.restore()


def _norm_entry(e) -> tuple:
    """把手柄提示条的条目规整成 ``(letter, text, long_press)`` 三元组。

    2 元组 ``(letter, text)`` → 短按（``long_press=False``）；3 元组
    ``(letter, text, long_press)`` → 原样透传。未知长度的条目一律按短按处理。
    """
    if len(e) >= 3:
        return (e[0], e[1], bool(e[2]))
    return (e[0], e[1], False)


def draw_hint_badge(painter: QPainter, hint: str, w: int, h: int,
                    radius: int = 9, margin: int = 5,
                    placement: str = "left") -> None:
    """Back-compat shim: render the legacy left/top **disc** badge.

    New code should call :func:`draw_hint_auto` (capsule form) instead.  Kept so
    any stray caller does not break.
    """
    if hint not in HINT_COLORS:
        return
    r = radius
    if placement == "top":
        cx, cy = w // 2, r + margin
    else:  # left, vertical centre
        cx, cy = r + margin, h // 2
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(0, 0, 0, 150))
    painter.drawEllipse(cx - r - 1, cy - r - 1, 2 * (r + 1), 2 * (r + 1))
    painter.setBrush(HINT_COLORS[hint])
    painter.drawEllipse(cx - r, cy - r, 2 * r, 2 * r)
    f = painter.font()
    f.setBold(True)
    f.setPixelSize(12)  # 圆点内字母缩小，避免「内容过大」溢出圆点
    f.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    painter.setFont(f)
    painter.setPen(QColor(12, 14, 20))
    painter.drawText(cx - r, cy - r, 2 * r, 2 * r,
                     Qt.AlignmentFlag.AlignCenter, hint)
    painter.restore()


def draw_top_capsule_badge(painter: QPainter, hint: str, w: int,
                           capsule_h: int = HINT_PILL_TOP_H,
                           top_margin: int = CAPSULE_TOP_MARGIN,
                           pill_w: int = HINT_PILL_TOP_W,
                           long_press: bool = False,
                           hold_progress: float = 0.0) -> None:
    """Paint a small ABXY pill at the **top-left corner, fully inside** a control.

    B 策略：覆盖式绘制、不占布局。短按钮（宽 < HINT_TOP_W）走此路径，胶囊贴在
    左上角、不预留内边距，故按钮高度恒定不跳变；居中文字与胶囊最左上角可能有极
    轻微重叠，属已签过的 B 取舍。用于紧凑/窄按钮，避免左侧胶囊盖住首字。
    """
    if hint not in HINT_COLORS:
        return
    x = 3  # 顶部胶囊贴左上角（覆盖式：不占布局，避免压住居中文字）
    y = top_margin
    radius = capsule_h // 2
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    # dark backing for contrast (slightly larger than the pill)
    painter.setBrush(QColor(0, 0, 0, 160))
    painter.drawRoundedRect(x - 1, y - 1, pill_w + 2, capsule_h + 2, radius + 1, radius + 1)
    # coloured pill
    painter.setBrush(HINT_COLORS[hint])
    painter.drawRoundedRect(x, y, pill_w, capsule_h, radius, radius)
    # letter — kept small because the pill is only CAPSULE_INNER_HEIGHT tall,
    # and drawn inside a slightly inset rect so it never touches the pill edge.
    f = painter.font()
    f.setBold(True)
    f.setPixelSize(11)  # 顶部胶囊字母：贴合胶囊高，比例协调不溢出
    f.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    painter.setFont(f)
    painter.setPen(QColor(12, 14, 20))
    inset_x = 2
    inset_y = 1
    painter.drawText(x + inset_x, y + inset_y,
                     pill_w - 2 * inset_x, capsule_h - 2 * inset_y,
                     Qt.AlignmentFlag.AlignCenter, hint)
    if long_press:
        # ④-C：短按钮（顶部胶囊）长按标记画在胶囊**下方**横排虚点。
        _draw_long_arc(painter, HINT_COLORS[hint], x, y, pill_w, capsule_h,
                       side="bottom", hold_progress=hold_progress)
    painter.restore()


def draw_left_capsule_badge(painter: QPainter, hint: str, h: int,
                             capsule_w: int = HINT_PILL_W, margin: int = 3,
                             long_press: bool = False,
                             hold_progress: float = 0.0) -> None:
    """Paint a small ABXY pill pinned to the **left edge, vertically centred**,
    fully inside the control (never clipped).  B 策略：覆盖式绘制，不占布局，
    文字靠样式表恒定的 ``padding-left`` 让到胶囊右侧，互不重叠。

    Used for long / wide buttons where a left-side placement keeps the label
    clear when it is left-inset (e.g. the HUD 锁帧 / 自动锁帧 / GPU 浮动 /
    自动降压 / RTSS 等按钮)。
    """
    if hint not in HINT_COLORS:
        return
    cap_h = min(capsule_w, h - 4)
    radius = cap_h // 2
    x = margin
    y = (h - cap_h) // 2  # 左侧胶囊竖直居中、贴左缘（覆盖式，不占布局）
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    # dark backing for contrast (slightly larger than the pill)
    painter.setBrush(QColor(0, 0, 0, 160))
    painter.drawRoundedRect(x - 1, y - 1, capsule_w + 2, cap_h + 2, radius + 1, radius + 1)
    # coloured pill
    painter.setBrush(HINT_COLORS[hint])
    painter.drawRoundedRect(x, y, capsule_w, cap_h, radius, radius)
    # letter — 字号缩小（HINT_LETTER_PX），在放大后的圆点内有呼吸余量、不溢出。
    f = painter.font()
    f.setBold(True)
    f.setPixelSize(HINT_LETTER_PX)
    f.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    painter.setFont(f)
    painter.setPen(QColor(12, 14, 20))
    painter.drawText(x, y, capsule_w, cap_h, Qt.AlignmentFlag.AlignCenter, hint)
    if long_press:
        _draw_long_arc(painter, HINT_COLORS[hint], x, y, capsule_w, cap_h,
                       side="right", hold_progress=hold_progress)
    painter.restore()


def draw_right_capsule_badge(painter: QPainter, hint: str, w: int, h: int,
                             capsule_w: int = HINT_PILL_W, margin: int = 3,
                             long_press: bool = False,
                             hold_progress: float = 0.0) -> None:
    """Paint a small ABXY pill pinned to the **right edge, vertically centred**,
    fully inside the control (mirror of :func:`draw_left_capsule_badge`).  B
    策略：覆盖式绘制、不占布局；用于把提示放右侧的按钮（如冻结卡片 Y = 标为
    游戏），左对齐文字不会被压住。"""
    if hint not in HINT_COLORS:
        return
    cap_h = min(capsule_w, h - 4)
    radius = cap_h // 2
    x = w - margin - capsule_w
    y = (h - cap_h) // 2  # 右侧胶囊竖直居中、贴右缘（覆盖式，不占布局）
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    # dark backing for contrast (slightly larger than the pill)
    painter.setBrush(QColor(0, 0, 0, 160))
    painter.drawRoundedRect(x - 1, y - 1, capsule_w + 2, cap_h + 2, radius + 1, radius + 1)
    # coloured pill
    painter.setBrush(HINT_COLORS[hint])
    painter.drawRoundedRect(x, y, capsule_w, cap_h, radius, radius)
    # letter — 字号缩小（HINT_LETTER_PX），在放大后的圆点内有呼吸余量、不溢出。
    f = painter.font()
    f.setBold(True)
    f.setPixelSize(HINT_LETTER_PX)
    f.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    painter.setFont(f)
    painter.setPen(QColor(12, 14, 20))
    painter.drawText(x, y, capsule_w, cap_h, Qt.AlignmentFlag.AlignCenter, hint)
    if long_press:
        # 右侧胶囊贴着按钮右缘：长按弧改包**左**缘，避免被按钮右边界裁掉。
        _draw_long_arc(painter, HINT_COLORS[hint], x, y, capsule_w, cap_h,
                       side="left", hold_progress=hold_progress)
    painter.restore()


def choose_hint_side(w: int, text: str, fm,
                     pill_w: int = HINT_PILL_W,
                     wide_threshold: int = HINT_WIDE_THRESHOLD,
                     margin: int = 3, gap: int = 4) -> str:
    """Decide where the ABXY capsule should sit on a (non-locked) button.

    Returns ``"left"`` or ``"top"``:

    * ``"left"`` — wide button whose **centred** text leaves enough room on the
      left for the left-edge capsule (no overlap).
    * ``"top"``  — narrow button, **or** a wide button whose centred text would
      be covered by the left-edge capsule.  In that case the capsule falls back
      to the top-left corner so it never sits on top of the label.

    The decision is purely geometric (width + measured text width) and is
    identical whether or not the hint is currently shown, so flipping the hint
    on/off never changes the button's size or the label position.
    """
    if w < wide_threshold:
        return "top"
    if text:
        tw = fm.horizontalAdvance(text)
        text_left = (w - tw) / 2.0
        # Capsule occupies [margin, margin + pill_w]; keep a small gap so the
        # first glyph never grazes the pill edge.
        if text_left < (margin + pill_w + gap):
            return "top"
    return "left"


def draw_hint_auto(painter: QPainter, hint: str, w: int, h: int,
                   text: str = None,
                   wide_threshold: int = HINT_WIDE_THRESHOLD,
                   long_press: bool = False,
                   hold_progress: float = 0.0) -> None:
    """Pick the capsule layout by button width — both are pills, never discs.

    * **Wide button** (``w >= wide_threshold``) → capsule pinned to the left
      edge, vertically centred; **unless** the centred text would be covered by
      the capsule, in which case it falls back to the top-left corner.
    * **Narrow button** → capsule pinned to the top edge, fully inside the
      control (never clipped, never covers the centred label).

    ``text`` is the button's label (used to detect overlap on wide buttons);
    when omitted the legacy width-only rule applies.
    """
    if hint not in HINT_COLORS:
        return
    if w >= wide_threshold and text:
        side = choose_hint_side(w, text, painter.fontMetrics())
    else:
        side = "left" if w >= wide_threshold else "top"
    if side == "left":
        draw_left_capsule_badge(painter, hint, h, capsule_w=HINT_PILL_W,
                                margin=3, long_press=long_press,
                                hold_progress=hold_progress)
    else:
        draw_top_capsule_badge(painter, hint, w, long_press=long_press,
                               hold_progress=hold_progress)


class HintButton(QPushButton):
    """Button that overlays an ABXY gamepad-hint capsule (A/B/X/Y) on its
    **left** edge, in pill form.

    The button does **not** self-paint its background.  It lets ``QPushButton``
    render normally via the stylesheet, so:

    * every caller's ``setStyleSheet`` colours are honoured (the layout
      editor's 重启程序 / 保存 / 呼吸灯自定义 … keep their distinct tints — #4),
    * the font comes from the stylesheet / app default instead of a hardcoded
      bold 10px that made some labels look bloated (#3),
    * the label is never squeezed by a top capsule (#2) — the badge is always
      a small pill pinned to the left, inside reserved left padding, so it can
      never overlap the centred text.

    The badge only appears when ``show_gamepad_hint`` is True (gated by the
    ``InteractionController`` once the user navigates by keyboard / gamepad).
    """

    # Left-padding (px) reserved by :meth:`set_style` so the capsule never
    # covers the label.  Callers that supply their own stylesheet must include
    # at least this much left padding (the layout editor's buttons already do).
    RESERVED_LEFT = 24

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.gamepad_hint = ""
        self.show_gamepad_hint = False
        # 胶囊位置：短按钮 = "top"（贴在按钮上沿，内部顶部）；长按钮 = "left"
        # （左侧胶囊）。仅由宽度决定，与提示是否显示无关，故高度恒定不跳变。
        self._hint_placement = "left"
        # 锁定摆放位置（"left"/"top"/"right"）；非 None 时 _recompute_placement
        # 不再按宽度自适应，用于进程冻结按钮把 Y 提示固定放在右侧。
        self._hint_placement_lock = None
        self._accent = "#7da2c8"
        self._active = False
        # 基线字号（px）：预设样式表从这个值取 font-size，保证「套预设前后」
        # 字号完全一致（详见 HINT_BUTTON_FONT_PX 的注释）。
        self._base_font_px = HINT_BUTTON_FONT_PX
        # 切角（chamfer）自绘所需的状态。仅当按钮风格预设写入了 cut_size 且
        # corner_radius==0 时，paintEvent 才走自绘切角路径；其余情况仍是原生
        # 样式表绘制（行为不变）。默认 None 表示「未套用预设、用原生绘制」。
        self._cut_size = 0
        self._corner_radius = 0
        self._bs_back = None
        self._bs_hover = None
        self._bs_border = None
        self._bs_fore = None
        self._bs_bt = 0
        # 激活态/未激活态颜色：仅当套用的预设定义了 ActiveBackColor/
        # InactiveBackColor 时由 styles._apply_style_to_ctrl 注入（非空）。
        # set_style 在按钮激活(active)时优先用 _bs_active_* 绘制，使「锁帧/
        # 自动锁帧/GPU浮动」这类切换按钮在绑定了带激活态的预设（如 ZZZ2）
        # 后，点击真的显示「开」的颜色；稀疏预设不写，回落到「基础色+文字」。
        self._bs_active_back = None
        self._bs_active_border = None
        self._bs_inactive_back = None
        self._bs_inactive_border = None
        self._hovered = False
        # 文本左对齐开关：默认 False（沿用原生 QPushButton 的居中）。冻结卡片
        # 的候选/已冻结按钮开启，让序号贴按钮左缘、便于「按数字键」一目了然。
        self._left_align = False
        # 常驻显示：为 True 时即便手柄导航未激活（调用方下发空提示），仍保留
        # 已设提示字母并显示胶囊——用于布局编辑页「保存(Y)/取消(B)」这类希望
        # 永远亮着手柄键提示的按钮。
        self._hint_always = False
        # 长按标记：为 True 时在提示胶囊外侧再拖一条渐隐横条（拖尾式），
        # 用来区分「长按」与「短按」。仅影响绘制，不占布局、不改变按钮尺寸。
        self._hint_long = False
        # 长按进度（0→1）：随按住时长增长，驱动长按虚点逐个淡出。0=静止全亮。
        self._hint_hold_progress = 0.0
        # 完全屏蔽：为 True 时永不显示手柄提示胶囊（调用方下发任何字母都忽略），
        # 用于「快捷键」编辑按钮等明确要求取消提示的场景。
        self._hint_suppressed = False
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Don't let the button grab keyboard focus: arrow keys must always reach
        # the window so the unified nav model can move the cursor between
        # buttons (keyboard + gamepad share one path).  Click / Enter still work
        # through the clicked signal.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(29)

    def _hint_height(self) -> int:
        # 高度恒定：B 策略下 _sync_hint_margins 永远置 0 边距，按钮高度只由
        # 文字 + 样式表内边距决定，与提示显示/隐藏无关 → 短按钮/编辑器按钮
        # 不会被顶部胶囊撑高。与 setMinimumHeight(29) 保持一致。
        return 29

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt camelCase
        # B 策略（覆盖式）：胶囊是用 QPainter 画在按钮表面的覆盖层，完全不占
        # 布局。尺寸只由「文字 + 样式表内边距」决定（_sync_hint_margins 永远
        # 置 0 边距），与「提示显示/隐藏」无关 → 短按钮不再被顶部胶囊撑高、
        # 长按钮不再被左右胶囊撑宽，页面宽度恒定、不溢出窗口。
        sh = super().sizeHint()
        return QSize(max(sh.width(), 56), max(sh.height(), self._hint_height()))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt camelCase
        mh = super().minimumSizeHint()
        return QSize(max(mh.width(), 56), max(mh.height(), self._hint_height()))

    def reserve_width_for(self, texts) -> None:
        """一次性固定按钮宽度 = 能容纳 ``texts`` 中最宽文案的宽度。

        目的：让文字会随状态切换（开/关 / 监测中 / 当前频率 / 不可用…）的
        按钮，在切换时**宽度恒定不跳**，避免把同一行的兄弟按钮左右挤动、
        破坏手柄/键盘导航的视觉顺序。

        取最宽文案按按钮当前字体/样式量出自然宽度（``sizeHint`` 已含样式表
        内边距与边框）；若该宽度达到「长按钮」阈值（``HINT_TOP_W``），说明它
        走左侧 ABXY 胶囊、手柄提示出现时会再加 ``RESERVED_LEFT`` 左边距——
        把这部分也**预先计入**固定宽度，使「提示出现/消失」同样不改变宽度。
        短按钮走顶部胶囊（提示完全画在按钮内部、不加边距），无需额外预留。
        """
        if not texts:
            return
        widest = max(texts, key=lambda t: self.fontMetrics().horizontalAdvance(t))
        cur = self.text()
        try:
            self.setText(widest)
            base = self.sizeHint().width()
        finally:
            self.setText(cur)
        # B 策略：胶囊为覆盖层、不再占布局，故无需预留 RESERVED_LEFT；这里只
        # 锁定「最宽文案」宽度，使 开/关 / 监测中… 等状态切换时宽度恒定不跳。
        self.setFixedWidth(max(56, base))

    def _sync_hint_margins(self) -> None:
        """B 策略（覆盖式）：胶囊是覆盖层、不占布局，故内边距永远为 0。

        无论提示显示/隐藏、摆放在 top/left/right，都不预留任何 contentsMargin，
        按钮尺寸只由「文字 + 样式表内边距」决定、恒定不变 → 彻底消除「提示亮起
        时短按钮被撑高、长按钮被撑宽导致页面超出窗口」两个 bug。胶囊的避让改
        由绘制时贴角 + 样式表恒定的 padding-left 负责，不随提示亮灭跳变。
        """
        cm = self.contentsMargins()
        if cm.left() != 0 or cm.top() != 0 or cm.right() != 0 or cm.bottom() != 0:
            self.setContentsMargins(0, 0, 0, 0)
            self.updateGeometry()

    def set_style(self, accent: str, active: bool) -> None:
        """Set the base look (accent colour + active/pressed state).

        Implemented as a stylesheet so ``QPushButton`` paints it natively —
        preserving the focus ring, hover and (crucially) any later
        ``setStyleSheet`` override a caller applies for a special-coloured
        button.          The gamepad capsule is an overlay painted on the button surface (B 策略),
        不占布局：左侧胶囊长按钮改用样式表恒定的 ``text-align:left`` +
        ``padding-left`` 把文字推离胶囊；顶部/右侧胶囊贴角，文字居中/左对齐
        互不冲突。提示显示/隐藏都不改变按钮尺寸（_sync_hint_margins 永远置 0）。
        """
        accent = accent or "#7da2c8"
        self._accent = accent
        self._active = bool(active)
        # 预设保护：若该按钮已套用用户预设（样式表已缓存到 _preset_ss），直接
        # 复用预设样式表后返回，绝不用自带默认外观覆盖。否则状态切换 / 周期
        # 刷新反复调用 set_style 会把「圆角 + 悬浮色」预设冲掉，交互后看起来
        # 就「预设消失、变回方角 + 绑定主题色」。预设按钮的开/关状态由按钮
        # 文字（如「锁帧:开/关」）表达，颜色永远遵循用户在按钮风格编辑器里的
        # 预设——这正是用户期望的「我设的圆角 + 悬浮色保持不动」。
        preset_ss = getattr(self, "_preset_ss", None)
        if preset_ss:
            # 预设保护：已套用户预设时，默认仍复用基础预设样式表（圆角/悬浮色
            # 不动，向后兼容「稀疏预设只靠文字表达开关」的 7-30 行为）。
            # 但若该预设定义了激活态颜色（_bs_active_back），则按钮激活时改用
            # 激活态颜色绘制——这是「套了带激活态的预设（如 ZZZ2）后，锁帧/
            # 自动锁帧/GPU浮动 点击仍显示『开』」的修复点。
            # 切角预设走自绘路径，激活色由 _paint_chamfer 在绘制时按 self._active
            # 实时取，这里直接复用 transparent 样式表即可（几何不变）。
            if self._cut_size and not self._corner_radius and self._bs_back is not None:
                self.setStyleSheet(preset_ss)
                self._sync_hint_margins()
                self.update()
                return
            if active and getattr(self, "_bs_active_back", None):
                self._apply_preset_variant(
                    self._bs_active_back, getattr(self, "_bs_active_border", None),
                    self._bs_fore)
                return
            if (not active) and getattr(self, "_bs_inactive_back", None):
                self._apply_preset_variant(
                    self._bs_inactive_back, getattr(self, "_bs_inactive_border", None),
                    self._bs_fore)
                return
            # 无激活态变体（稀疏预设）→ 保持基础外观，开关只靠文字表达。
            self.setStyleSheet(preset_ss)
            self._sync_hint_margins()
            self.update()
            return
        # 保留已套用预设的几何：当按钮风格预设写了圆角/切角后，HUD 在选中 /
        # 聚焦 / 重建时会再次调用 set_style（re-apply 默认外观）。若此处把
        # _corner_radius / _cut_size / _bs_* 清零，会把预设的圆角（>7px）或
        # 切角冲掉 —— 圆角预设看起来就「变回矩形」。仅当本按钮尚未套用任何
        # 预设（_bs_back 为空且无圆角）时，才回到原生默认外观。
        preset_applied = bool(getattr(self, "_bs_back", None)) or bool(self._corner_radius)
        if not preset_applied:
            self._bs_back = None
            self._cut_size = 0
            self._corner_radius = 0
        # 圆角：有预设就用预设值（保持按钮风格预设的圆角不丢失），否则默认 7px。
        radius = int(self._corner_radius) if self._corner_radius else 7
        if active:
            bg = accent
            fg = "#0c0f16"
            border = accent
        else:
            bg = "rgba(255,255,255,0.10)"
            fg = "#dfe6f0"
            border = "rgba(255,255,255,0.18)"
        # 顶部胶囊（短按钮）需要更小竖向留白，给上沿胶囊让出空间。
        vpad = 1 if self._hint_placement == "top" else 5
        # 文本对齐：仅冻结列表等「明确左对齐（_left_align）」的按钮走左对齐；
        # 普通 left 摆放的长按钮（锁帧/GPU浮动/RTSS 等）保持原生居中——胶囊是
        # 覆盖层画在左缘，宽按钮居中文字左侧本就有空白，胶囊落在空白处不压字，
        # 故不强行左对齐、也不加 padding-left，避免「提示未亮时左侧留空占位」。
        hleft = self._left_align
        align = " text-align: left;" if hleft else ""
        # 仅当「左对齐 且 左侧胶囊」同时成立时才预留左内缩避让（如冻结列表项），
        # 普通 left 长按钮不加 → 文字居中、无空占位。
        lpad = " padding-left: 22px;" if (self._left_align and self._hint_placement == "left") else ""
        ss = (
            "QPushButton {"
            f" background: {bg}; color: {fg};"
            f" border: 1px solid {border}; border-radius: {radius}px;"
            f" padding: {vpad}px 12px;{lpad}"
            f" font-size: {self._base_font_px}px;" + align +
            "}"
            "QPushButton:hover { background: rgba(255,255,255,0.16); }"
            "QPushButton:focus { outline: none;"
            " border: 1px solid rgba(255,255,255,0.40); }"
        )
        self.setStyleSheet(ss)
        self._sync_hint_margins()
        self.update()

    def _apply_preset_variant(self, back, border, fore) -> None:
        """套用预设的激活/未激活态颜色，同时保留预设的几何（圆角/边框厚度）。

        仅用于非切角预设：圆角预设的外观完全由样式表控制，这里用激活态颜色
        重拼一张样式表（边框/圆角/内边距与原预设一致），既显示「开」的颜色，
        又不回落到默认主题色外观（避免 7-30 修复前的『交互后预设消失』问题）。
        """
        border = border or self._bs_border
        fore = fore or self._bs_fore
        cr = int(self._corner_radius) if self._corner_radius else 7
        bt = int(self._bs_bt) if self._bs_bt else 1
        # B 策略：仅「左对齐 且 左侧胶囊」的特例（冻结列表项）预留左内缩；
        # 普通 left 长按钮保持居中、不加 padding-left，避免空占位。
        hleft = self._left_align
        align = " text-align: left;" if hleft else ""
        lpad = " padding-left: 22px;" if (self._left_align and self._hint_placement == "left") else ""
        ss = (
            f"QPushButton {{ background: rgb({back[0]},{back[1]},{back[2]});"
            f" color: rgb({fore[0]},{fore[1]},{fore[2]});"
            f" border: {bt}px solid rgb({border[0]},{border[1]},{border[2]});"
            f" border-radius: {cr}px; padding: 4px 10px;{lpad}{align}"
            f" font-size: {self._base_font_px}px; }}"
            "QPushButton:hover { background: rgba(255,255,255,0.16); }"
            "QPushButton:focus { outline: none;"
            " border: 1px solid rgba(255,255,255,0.40); }"
        )
        self.setStyleSheet(ss)
        self._sync_hint_margins()
        self.update()

    def _recompute_placement(self) -> None:
        """短按钮（宽度 < HINT_TOP_W）把提示胶囊贴在按钮上沿（内部顶部），
        其余按钮保持左侧胶囊。位置只由宽度决定（与提示是否显示无关），
        因此按钮高度恒定，不会在显示/隐藏提示时跳变。

        若 ``_hint_placement_lock`` 已设置（如冻结按钮锁定 "right"），
        则尊重锁定值、不再按宽度自适应覆盖。"""
        if self._hint_placement_lock:
            self._hint_placement = self._hint_placement_lock
            return
        self._hint_placement = (
            "top" if (self.width() > 0 and self.width() < HINT_TOP_W) else "left"
        )

    def _effective_placement(self) -> str:
        """绘制时实际使用的胶囊摆放位置（考虑文字重叠回退）。

        * 锁定为 "right"（如冻结卡片 Y 提示）→ 维持右侧。
        * 短按钮（``_hint_placement == "top"``）→ 维持顶部左上角。
        * 长按钮（"left"）：默认左缘竖直居中；但若**居中文字**会盖到左缘胶囊
          （文字太宽、左侧空白不足），自动回退为**左上角**胶囊，避免压字。
          该判定只用几何（宽度 + 文字宽度），与提示亮灭无关 → 不跳变。
        """
        if self._hint_placement == "right":
            return "right"
        if self._hint_placement == "top":
            return "top"
        # 长按钮：按文字是否与左缘胶囊重叠，决定 left / top。
        return choose_hint_side(self.width(), self.text(), self.fontMetrics())

    def set_hint_side(self, side: str) -> None:
        """锁定提示胶囊的摆放位置（"left" / "top" / "right"）。

        锁定后 :meth:`_recompute_placement` 不再按宽度自适应覆盖，确保像
        「进程冻结按钮把 Y 提示放右侧」这类特例能稳定生效（否则每次
        ``set_gamepad_hint`` 都会把位置重算回 left/top）。"""
        self._hint_placement_lock = side
        self._recompute_placement()
        self._sync_hint_margins()
        self.update()

    def set_hint_always_visible(self, v: bool) -> None:
        """让手柄提示「常驻显示」：即便手柄导航未激活，也保留并绘制提示胶囊。

        调用方通常仍需先 ``set_gamepad_hint("Y")`` 设定字母；本方法仅保证该
        字母在导航未激活时不被清空、不消失。"""
        self._hint_always = bool(v)
        if self._hint_always and self.gamepad_hint:
            self.show_gamepad_hint = True
            self._sync_hint_margins()
            self.update()

    def set_hint_long_press(self, v: bool) -> None:
        """标记该按钮的手柄提示为「长按」，徽章改用拖尾式（胶囊 + 渐隐横条）。

        与 :meth:`set_gamepad_hint` 互不干扰：可先设字母再设长按，或反过来；
        两者都只触发重绘，不动样式表、不改按钮几何（B 策略：胶囊是覆盖层）。
        """
        v = bool(v)
        if v == self._hint_long:
            return
        self._hint_long = v
        self.update()

    def set_hint_hold_progress(self, p: float) -> None:
        """推长按进度（0→1）：驱动长按虚点随按住时长逐个淡出。

        0 = 静止全亮；1 = 全部灭（长按已触发）。仅触发重绘，不碰样式表/几何。
        与 :meth:`set_hint_long_press` 配合：先标记长按胶囊，再每帧推进度。
        """
        p = max(0.0, min(1.0, float(p)))
        if abs(p - self._hint_hold_progress) < 0.01:
            return
        self._hint_hold_progress = p
        self.update()

    def set_hint_suppressed(self, v: bool) -> None:
        """完全屏蔽手柄提示：为 True 时任何 ``set_gamepad_hint`` 调用都被忽略，
        提示胶囊永远不绘制（用于明确要求取消提示的按钮）。"""
        self._hint_suppressed = bool(v)
        if self._hint_suppressed and (self.gamepad_hint or self.show_gamepad_hint):
            self.gamepad_hint = ""
            self.show_gamepad_hint = False
            self._recompute_placement()
            self._sync_hint_margins()
            self.update()

    def set_gamepad_hint(self, hint: str) -> None:
        hint = hint or ""
        # 完全屏蔽：永不显示提示，调用方下发的任何字母都忽略。
        if self._hint_suppressed:
            if self.gamepad_hint or self.show_gamepad_hint:
                self.gamepad_hint = ""
                self.show_gamepad_hint = False
                self._recompute_placement()
                # 只刷新内边距（清空胶囊预留空间）并重绘，绝不重写整个样式表。
                self._sync_hint_margins()
                self.update()
            return
        # 常驻显示：导航未激活时调用方下发空串，仍保留已设字母不消失。
        if self._hint_always and hint == "" and self.gamepad_hint:
            hint = self.gamepad_hint
        if hint != self.gamepad_hint:
            self.gamepad_hint = hint
            self.show_gamepad_hint = bool(hint) or self._hint_always
            self._recompute_placement()
            # 只刷新内边距（为胶囊预留空间）并重绘，绝不重写整个样式表——
            # 否则会把按钮风格预设套上的圆角/颜色冲掉（选中显示提示后变回默认）。
            self._sync_hint_margins()
            self.update()
        elif self._hint_always and not self.show_gamepad_hint and self.gamepad_hint:
            # 字母未变但需补保证常驻可见（首次进入编辑器时可能尚未被下发过）。
            self.show_gamepad_hint = True
            self._sync_hint_margins()
            self.update()

    def enterEvent(self, ev: object) -> None:  # noqa: N802 - Qt camelCase
        self._hovered = True
        self.update()
        super().enterEvent(ev)  # type: ignore[call-arg]

    def leaveEvent(self, ev: object) -> None:  # noqa: N802 - Qt camelCase
        self._hovered = False
        self.update()
        super().leaveEvent(ev)  # type: ignore[call-arg]

    def resizeEvent(self, ev: object) -> None:  # noqa: N802 - Qt camelCase
        self._recompute_placement()
        # 只刷新内边距（胶囊位置可能随宽度翻转），绝不重写样式表——
        # 否则会冲掉按钮风格预设的颜色/圆角，以及编辑器按钮的自定义配色。
        self._sync_hint_margins()
        super().resizeEvent(ev)  # type: ignore[call-arg]

    def paintEvent(self, ev: object) -> None:  # noqa: N802 - Qt camelCase
        # 切角（chamfer）预设：corner_radius==0 且 cut_size>0 时，原生
        # QPushButton 无法做切角（border-radius 只能圆角），改为自绘切角路径。
        # 这样「切角」设置才会真正生效。其余情况（圆角预设 / 默认样式）仍走
        # 原生绘制，行为完全不变。
        if (self._cut_size and not self._corner_radius
                and self._bs_back is not None):
            self._paint_chamfer(ev)
            return
        # 三种摆放（短按钮顶部 / 长按钮左侧 / 右侧）统一走原生 QPushButton 绘制
        # （背景、文字、字体、聚焦环全部由样式表接管），仅在末尾叠加 ABXY 胶囊。
        # 这样短按钮显示提示时也能保留其真实背景/预设色，而不会像此前顶部胶囊
        # 自绘那样把按钮背景硬写成近乎透明（表现为「一片空白」）。顶部胶囊完全
        # 落在按钮内部（不超出边界），且 _sync_hint_margins 对 top 摆放已置 0
        # 上边距，故按钮高度恒定、不纵向拉伸（bug #1 修复不变式保持）。
        super().paintEvent(ev)
        if self.show_gamepad_hint and self.gamepad_hint in HINT_COLORS:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            side = self._effective_placement()
            if side == "right":
                # 右侧胶囊，位于预留的右边距内。
                draw_right_capsule_badge(
                    p, self.gamepad_hint, self.width(), self.height(),
                    capsule_w=HINT_PILL_W, margin=3,
                    long_press=self._hint_long,
                    hold_progress=self._hint_hold_progress)
            elif side == "top":
                # 顶部左上角胶囊：短按钮，或长按钮文字会盖到左缘时的回退。
                draw_top_capsule_badge(p, self.gamepad_hint, self.width(),
                                       long_press=self._hint_long,
                                       hold_progress=self._hint_hold_progress)
            else:
                # 长按钮：左侧胶囊，位于预留的左边距内。
                draw_left_capsule_badge(
                    p, self.gamepad_hint, self.height(),
                    capsule_w=HINT_PILL_W, margin=3,
                    long_press=self._hint_long,
                    hold_progress=self._hint_hold_progress)
            p.end()

    def _paint_chamfer(self, ev: object) -> None:  # noqa: N802 - Qt camelCase
        """自绘切角背景 + 边框 + 文字 + 手柄提示（仅切角预设启用）。"""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        cs = int(self._cut_size)
        cs = max(0, min(cs, min(w, h) // 2))
        path = QPainterPath()
        path.moveTo(cs, 0)
        path.lineTo(w, 0)
        path.lineTo(w, h - cs)
        path.lineTo(w - cs, h)
        path.lineTo(0, h)
        path.lineTo(0, cs)
        path.closeSubpath()
        # 激活态（active）且预设定义了激活色时，用激活色自绘；否则走基础色
        # （悬浮时用悬浮色）。切角预设的「开/关」颜色由此处实时取，几何不变。
        if self._active and self._bs_active_back is not None:
            back = self._bs_active_back
            border = self._bs_active_border or self._bs_border
            fore = self._bs_fore
        else:
            back = self._bs_hover if (self._hovered and self._bs_hover) else self._bs_back
            border = self._bs_border
            fore = self._bs_fore
        p.fillPath(path, QColor(*back))
        if self._bs_bt and border:
            p.setPen(QPen(QColor(*border), int(self._bs_bt)))
            p.drawPath(path)
        if fore:
            p.setPen(QColor(*fore))
            # 用按钮自身字体（与原生 QPushButton 一致），不要硬编码加粗大字——
            # 否则切角预设应用到主页面后文字会突然变粗变大（与原预览/其它按钮
            # 不一致）。
            p.setFont(self.font())
            tr = self.rect()
            # B 策略：胶囊永远贴角（top/left 贴左上、right 贴右上），文字内缩量
            # 恒定、与提示亮灭无关 → 不出现「提示亮时文字右移」的跳变。摆放取
            # 实际位置（含文字重叠回退），与 paintEvent 一致。
            side = self._effective_placement()
            if side == "top":
                tr = tr.adjusted(0, CAPSULE_TOP_MARGIN + HINT_PILL_TOP_H + 1, 0, 0)
            elif side == "right":
                tr = tr.adjusted(0, 0, -BADGE_WIDTH, 0)
            else:  # left
                tr = tr.adjusted(BADGE_WIDTH, 0, 0, 0)
            _ta = (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter) \
                if self._left_align else Qt.AlignmentFlag.AlignCenter
            p.drawText(tr, _ta, self.text())
            if self.show_gamepad_hint and self.gamepad_hint in HINT_COLORS:
                if side == "right":
                    draw_right_capsule_badge(
                        p, self.gamepad_hint, w, h, capsule_w=HINT_PILL_W,
                        margin=3, long_press=self._hint_long,
                        hold_progress=self._hint_hold_progress)
                else:
                    draw_hint_auto(p, self.gamepad_hint, w, h, text=self.text(),
                                   long_press=self._hint_long,
                                   hold_progress=self._hint_hold_progress)
        p.end()


class GamepadHintBar(QWidget):
    """Persistent (always-on) legend of ABXY gamepad shortcuts, drawn in the
    exact same capsule style as :class:`HintButton` (``HINT_COLORS`` + dark
    backing) so it reads as one family with the in-control hints.

    Unlike :class:`HintButton`, this is *always* visible — it is a static
    reference, not driven by navigation state.  Used by panels that want a
    bottom strip listing what each gamepad key does (e.g. the freeze card:
    X=关闭进程, Y=标为游戏).  ``entries`` is a list of ``(letter, text)`` —
    or ``(letter, text, long_press)`` when an entry must be marked as a
    **long press** (the pill then gets the same fading tail as
    :class:`HintButton`, see :func:`_draw_long_tail`).  Entries without a
    known colour are drawn as bare text.
    """

    def __init__(self, entries: list = None, parent: QWidget = None) -> None:
        super().__init__(parent)
        # Explicit palette so the bar is readable in both themes without
        # depending on CSS vars.  Dark background theme → light text below.
        self._pill_w = 16
        self._pill_h = 13
        self._pill_extra = 9  # 长按条目额外留白：给右侧圆点腾位置（不压字母、不贴边缘）
        self._gap = 7
        self._text_gap = 3
        self._letter_px = 11
        self._label_px = 10
        self.setContentsMargins(2, 3, 2, 3)
        # 水平策略用 Preferred：布局会优先给足 sizeHint（完整文案），空间不足时
        # 按 minimumSizeHint 压缩到「所有胶囊 + 间隔」的宽度，掌机窄屏下只省略
        # 文字、绝不把整行压缩到 0 导致手柄键提示全部消失（Ignored 策略在紧空间
        # 下会把 bar 压成 0px）。
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._entries = []
        self._layout = []
        self._total_w = 0
        self._min_w = 0
        self.set_entries(entries or [])

    def set_entries(self, entries: list) -> None:
        """Replace the displayed entries.

        Each entry is ``(letter, text)`` or ``(letter, text, long_press)``;
        the 2-tuple form is kept for backwards compatibility (short press).
        """
        self._entries = [_norm_entry(e) for e in (entries or [])]
        self._refresh()
        self.updateGeometry()
        self.update()

    def _refresh(self) -> None:
        fm = self.fontMetrics()
        x = 0
        self._layout = []
        for letter, text, lp in self._entries:
            tw = fm.horizontalAdvance(text) if text else 0
            # ④-A：长按条目用「胶囊右侧留白圆点」表示（常驻说明条）。长按条目
            # 的胶囊加宽 _pill_extra，右侧留白放圆点，绝不压住居中的 Y/X 字母。
            is_long = bool(lp) and letter in HINT_COLORS
            pw = self._pill_w + (self._pill_extra if is_long else 0)
            entry_w = pw + self._text_gap + tw
            self._layout.append((x, letter, text, entry_w, lp, pw))
            x += entry_w + self._gap
        self._total_w = max(0, x - self._gap)
        if self._layout:
            pill_only = sum(item[5] for item in self._layout)
            self._min_w = pill_only + (len(self._layout) - 1) * self._gap
        else:
            self._min_w = 0
        self.setFixedHeight(self._pill_h + 8)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt camelCase
        return QSize(self._total_w, self._pill_h + 8)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt camelCase
        # 下限 = 所有胶囊 + 间隔（不含文字）。避免窄屏被压到 0 导致整行提示消失；
        # 空间再不够时绘制代码会省略文字，至少保证键帽可见。
        return QSize(max(8, self._min_w), self._pill_h + 6)

    def paintEvent(self, ev: object) -> None:  # noqa: N802 - Qt camelCase
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        tf = QFont(self.font())
        tf.setPixelSize(self._label_px)
        fm = QFontMetrics(tf)
        y = (self.height() - self._pill_h) // 2
        # 按实际（可能被窄屏压缩后的）宽度绘制：超出可用宽度的条目不再绘制，
        # 最后一个可见条目的文案用省略号截断，保证任何屏宽下都「显示完全、不溢出」。
        available = self.width()
        for (x, letter, text, entry_w, lp, pw) in self._layout:
            if x >= available:
                break
            lead = pw + self._text_gap
            is_long = bool(lp) and letter in HINT_COLORS
            if letter in HINT_COLORS:
                # dark backing (slightly larger than the pill) for contrast
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(0, 0, 0, 150))
                p.drawRoundedRect(x - 1, y - 1, pw + 2,
                                  self._pill_h + 2, (self._pill_h + 2) // 2,
                                  (self._pill_h + 2) // 2)
                # coloured pill (long-press pills are widened to host the dot)
                p.setBrush(HINT_COLORS[letter])
                p.drawRoundedRect(x, y, pw, self._pill_h,
                                  self._pill_h // 2, self._pill_h // 2)
                # letter — bold, dark on the coloured pill.  Always centred in
                # the LEFT _pill_w region so the right reserved slot never clips
                # the glyph (fixes Y/X being obscured by the long-press dot).
                lf = QFont(self.font())
                lf.setBold(True)
                lf.setPixelSize(self._letter_px)
                lf.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
                p.setFont(lf)
                p.setPen(QColor(12, 14, 20))
                p.drawText(x, y, self._pill_w, self._pill_h,
                           Qt.AlignmentFlag.AlignCenter, letter)
                # ④-A：常驻说明条的长按标记 = 胶囊右侧留白圆点（画在加宽后的
                # 右侧留白中央），区别于 ④-B/④-C 的按钮外置虚点弧/下方虚点。
                if is_long:
                    _draw_inner_dot(p, HINT_COLORS[letter],
                                    x + self._pill_w + self._pill_extra / 2.0,
                                    y + self._pill_h / 2.0, 2.0)
            # label text — light, readable in dark theme。窄屏超出可用宽度时
            # 用省略号截断，保证不被裁掉、也保持紧凑。
            p.setFont(tf)
            p.setPen(QColor(223, 230, 240))
            tx = x + lead
            max_text_w = available - tx
            if max_text_w > 4 and text:
                drawn = fm.elidedText(text, Qt.TextElideMode.ElideRight,
                                      max_text_w)
                p.drawText(tx, y, max_text_w, self._pill_h,
                           Qt.AlignmentFlag.AlignLeft
                           | Qt.AlignmentFlag.AlignVCenter, drawn)
        p.end()
