# -*- coding: utf-8 -*-
"""Offscreen smoke test for the page-transition module (2026-08-31).

Covers Phase 0 (PageIndicator) + Phase 1 (TransitionVeil / PageTransition):

1. PageIndicator.sync() 落位正确；slide_to() 动画能跑到终点。
2. PageIndicator 在不可见（编辑器模式下页码条被隐藏）时只跳位、不动画、不 show。
3. TransitionVeil 的两种模式（slide / fade）在 t=0 / 0.5 / 1 都能画出来不崩。
4. PageTransition 动画能跑完并发出 finished；stop() 不误发 finished。
5. release_pixmaps() 真的把两张快照放掉（掌机内存：每张约 3MB）。
6. AppConfig 的 PageTransition / TransitionMs 往返正确，垃圾值回落 slide。
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "src"))

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QColor, QPainter, QPixmap  # noqa: E402
from PyQt6.QtWidgets import QApplication, QHBoxLayout, QPushButton, QWidget  # noqa: E402

from ningbingjue.ui.page_transition import (  # noqa: E402
    INDICATOR_HEIGHT,
    PageIndicator,
    PageTransition,
    TransitionVeil,
)
from ningbingjue.core.config import AppConfig  # noqa: E402

app = QApplication.instance() or QApplication([])

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAILURES.append(name)


def spin_until(predicate, timeout_s=3.0):
    """Drive the event loop until predicate() is true or we time out."""
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.004)
    return predicate()


# ---------------------------------------------------------------------------
# 1-2. Phase 0：PageIndicator
# ---------------------------------------------------------------------------
print("Phase 0 - PageIndicator")

bar = QWidget()
lay = QHBoxLayout(bar)
lay.setContentsMargins(0, 0, 0, 0)
lay.setSpacing(4)
buttons = []
for i in range(3):
    b = QPushButton(str(i + 1))
    b.setFixedSize(26, 22)
    lay.addWidget(b)
    buttons.append(b)
lay.addStretch(1)
bar.show()
app.processEvents()

ind = PageIndicator(bar)
ind.sync(buttons, 0)
check("sync 落位到第 0 个按钮下方",
      ind.pos().x() == buttons[0].x()
      and ind.pos().y() == buttons[0].y() + buttons[0].height() - INDICATOR_HEIGHT,
      "got %r want x=%d y=%d" % ((ind.pos().x(), ind.pos().y()),
                                 buttons[0].x(),
                                 buttons[0].y() + buttons[0].height() - INDICATOR_HEIGHT))
check("sync 后可见", ind.isVisible())
check("指示条不撑高页码条（贴底边内侧）",
      ind.pos().y() + ind.height() <= bar.height(),
      "ind bottom=%d bar h=%d" % (ind.pos().y() + ind.height(), bar.height()))

# 滑到第 2 页
ind.slide_to(2)
target_x = buttons[2].x()
ok = spin_until(lambda: ind.pos().x() == target_x)
check("slide_to 动画跑到终点", ok and ind.pos().x() == target_x,
      "x=%d want %d" % (ind.pos().x(), target_x))

# 同位置重复调用应 no-op（不报错）
ind.slide_to(2)
check("重复 slide_to 同位置不报错", ind.pos().x() == target_x)

# 不可见时（页码条被编辑器隐藏）只跳位、不动画、不 show
bar.hide()
ind.slide_to(0)
check("隐藏时跳位到目标", ind.pos().x() == buttons[0].x(),
      "x=%d want %d" % (ind.pos().x(), buttons[0].x()))
check("隐藏时 isVisible() 为 False（不单独 show）", not ind.isVisible())
bar.show()

# 无按钮 / 空列表不崩
ind2 = PageIndicator(bar)
ind2.sync([], 0)
check("空按钮列表不崩且隐藏", not ind2.isVisible())
ind2.slide_to(0)
print("  ok   空按钮列表 slide_to 不崩")

# ---------------------------------------------------------------------------
# 3-5. Phase 1：TransitionVeil / PageTransition
# ---------------------------------------------------------------------------
print("Phase 1 - TransitionVeil / PageTransition")


def make_pm(color):
    pm = QPixmap(80, 60)
    pm.fill(QColor(color))
    return pm


host = QWidget()
host.resize(200, 200)
host.show()
app.processEvents()

for mode in ("slide", "fade"):
    for direction in (1, -1):
        pm_old = make_pm("#204080")
        pm_new = make_pm("#802040")
        veil = TransitionVeil(host, mode, pm_old, pm_new, direction)
        veil.setGeometry(10, 10, 80, 60)
        veil.show()
        for t in (0.0, 0.5, 1.0):
            veil.set_progress(t)
            app.processEvents()
            veil.grab()  # 强制走一次 paintEvent
        veil.release_pixmaps()
        check("%s dir=%d 三个进度点绘制不崩 + 位图已释放" % (mode, direction),
              veil._old_pm is None and veil._new_pm is None)
        veil.hide()
        veil.setParent(None)
        veil.deleteLater()

# 非法模式回落 slide、不崩
veil = TransitionVeil(host, "bogus", make_pm("#111111"), make_pm("#222222"), 1)
check("非法模式回落 slide", veil._mode == "slide")
veil.setParent(None)

# 动画能跑完 + 发出 finished
tr = PageTransition()
seen = []
tr.finished.connect(lambda: seen.append(True))
pm_old = make_pm("#123456")
pm_new = make_pm("#654321")
veil = TransitionVeil(host, "slide", pm_old, pm_new, 1)
veil.setGeometry(10, 10, 80, 60)
veil.show()
tr.start(veil, 120)
check("start() 后 running=True", tr.running)
ok = spin_until(lambda: not tr.running, timeout_s=3.0)
check("动画在 3s 内跑完", ok)
check("finished 信号发出", len(seen) == 1, "seen=%r" % (seen,))
check("结束时进度为 1.0", abs(tr.progress - 1.0) < 1e-6,
      "progress=%r" % tr.progress)

# stop() 不应误发 finished
seen2 = []
tr.finished.connect(lambda: seen2.append(True))
veil2 = TransitionVeil(host, "fade", make_pm("#111111"), make_pm("#222222"), -1)
veil2.setGeometry(10, 10, 80, 60)
veil2.show()
tr.start(veil2, 5000)
app.processEvents()
tr.stop()
app.processEvents()
spin_until(lambda: False, timeout_s=0.05)
check("stop() 中止且不误发 finished",
      (not tr.running) and len(seen2) == 0, "seen2=%r" % (seen2,))
veil2.setParent(None)

# 快速连按：重复 start() 只保留最后一个动画
seen3 = []
tr.finished.connect(lambda: seen3.append(True))
veils = []
for i in range(5):
    v = TransitionVeil(host, "slide", make_pm("#000001"), make_pm("#000002"), 1)
    v.setGeometry(10, 10, 80, 60)
    v.show()
    veils.append(v)
    tr.start(v, 60)
app.processEvents()
ok = spin_until(lambda: not tr.running, timeout_s=3.0)
check("连续 5 次 start() 只发出一次 finished", ok and len(seen3) == 1,
      "seen3=%r" % (seen3,))

# ---------------------------------------------------------------------------
# Phase 1b：DPR 缩放正确性
# ---------------------------------------------------------------------------
print("Phase 1b - DPR scaling correctness")


def make_high_dpr_snapshot():
    """造一张逻辑 60x40、devicePixelRatio=2 的快照，四个象限颜色不同。

    ⚠️ 必须在 **dpr=1 的状态下** 按 device 像素把内容画完，最后再
    ``setDevicePixelRatio(2.0)``。反过来（先设 dpr 再作画）QPainter 会按
    逻辑坐标作画，``fillRect(0,0,60,40)`` 会铺满整张图，测试就会自证失败。
    """
    pm = QPixmap(120, 80)
    pm.fill(Qt.GlobalColor.black)
    p = QPainter(pm)
    p.fillRect(0, 0, 60, 40, QColor("red"))          # 左上 device
    p.fillRect(60, 0, 60, 40, QColor("green"))       # 右上 device
    p.fillRect(0, 40, 60, 40, QColor("blue"))        # 左下 device
    p.fillRect(60, 40, 60, 40, QColor("yellow"))     # 右下 device
    p.end()
    pm.setDevicePixelRatio(2.0)
    return pm


pm_colored = make_high_dpr_snapshot()
veil_dpr = TransitionVeil(host, "fade", None, pm_colored, 1)
veil_dpr.setGeometry(0, 0, 60, 40)
veil_dpr.show()
veil_dpr.set_progress(0.0)
app.processEvents()
out = veil_dpr.grab()
out_img = out.toImage()
# 用比例坐标取样，兼容不同 host DPR 下 out 的 device 尺寸
pw, ph = out_img.width(), out_img.height()
qx1, qx2 = int(pw * 0.1), int(pw * 0.9)
qy1, qy2 = int(ph * 0.1), int(ph * 0.9)


_EXPECT_NAME = {
    "red": "#ff0000",
    "green": "#008000",
    "blue": "#0000ff",
    "yellow": "#ffff00",
}


def _pixel_is(x, y, color_name):
    return QColor(out_img.pixel(x, y)).name() == _EXPECT_NAME[color_name]


# 如果 sourceRect 被错误地除以 DPR，四个象限都会被拉伸成左上角红色，测试失败。
check("DPR 缩放正确：左上角呈现红色",
      _pixel_is(qx1, qy1, "red"), out_img.pixel(qx1, qy1))
check("DPR 缩放正确：右上角呈现绿色",
      _pixel_is(qx2, qy1, "green"), out_img.pixel(qx2, qy1))
check("DPR 缩放正确：左下角呈现蓝色",
      _pixel_is(qx1, qy2, "blue"), out_img.pixel(qx1, qy2))
check("DPR 缩放正确：右下角呈现黄色",
      _pixel_is(qx2, qy2, "yellow"), out_img.pixel(qx2, qy2))
veil_dpr.hide()
veil_dpr.setParent(None)
veil_dpr.deleteLater()

# ---------------------------------------------------------------------------
# 6. 配置往返
# ---------------------------------------------------------------------------
print("Config - PageTransition / TransitionMs")

cfg = AppConfig()
check("默认模式 slide", cfg.page_transition == "slide")
check("默认时长 180", cfg.transition_ms == 180)

d = cfg.to_dict()
check("to_dict 写入 PageTransition", d.get("PageTransition") == "slide")
check("to_dict 写入 TransitionMs", d.get("TransitionMs") == 180)

for raw, want in (("fade", "fade"), ("NONE", "none"),
                  ("bogus", "slide"), ("", "slide"), (None, "slide")):
    got = AppConfig.from_dict({"PageTransition": raw}).page_transition
    check("from_dict 模式 %r -> %s" % (raw, want), got == want, "got %r" % got)

check("from_dict 时长 clamp 上限",
      AppConfig.from_dict({"TransitionMs": 99999}).transition_ms == 600)
check("from_dict 时长 clamp 下限",
      AppConfig.from_dict({"TransitionMs": -5}).transition_ms == 0)
check("from_dict 垃圾时长回落 180",
      AppConfig.from_dict({"TransitionMs": "abc"}).transition_ms == 180)
check("旧配置缺字段回落 slide/180",
      AppConfig.from_dict({}).page_transition == "slide"
      and AppConfig.from_dict({}).transition_ms == 180)

# 往返稳定性
c2 = AppConfig.from_dict({"PageTransition": "fade", "TransitionMs": 240})
check("往返稳定", c2.to_dict().get("PageTransition") == "fade"
      and c2.to_dict().get("TransitionMs") == 240)

# ---------------------------------------------------------------------------
print()
if FAILURES:
    print("FAILED (%d): %s" % (len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("ALL PASS")
