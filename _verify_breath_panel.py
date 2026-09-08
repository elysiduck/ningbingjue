"""验证呼吸灯编辑面板 2026-08-30 整合改造（offscreen 直构组件）。

检查项：
  1. 顶部预览已换成 _BreathHeadPreview（旧的静态渐变条 _BreathPreview 已删）
  2. 8 项参数控件齐全、取值范围与源码一致
  3. nav_items() 不抛异常，且顺序符合「视觉顺序 = 导航顺序」
  4. 预览 paintEvent（模拟窗口 + 两张波形）真的画出了东西
  5. 改周期/亮度/谷值能真正推到预览（旧代码 _on_period 漏推的 bug 已修）
"""
import os
import sys
from types import SimpleNamespace as NS

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from PyQt6.QtCore import QPoint                          # noqa: E402
from PyQt6.QtGui import QColor, QPixmap, QRegion         # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget      # noqa: E402
from ningbingjue.ui.edit_overlay import (           # noqa: E402
    BreathingEditPanel, _BreathHeadPreview, _BreathHeadCanvas)

app = QApplication(sys.argv)

ok = True


def check(label, cond, extra=""):
    global ok
    flag = "PASS" if cond else "FAIL"
    if not cond:
        ok = False
    print(f"  [{flag}] {label}{(' -> ' + str(extra)) if extra else ''}")


cfg = NS(breath_period_s=2.4, breath_max_brightness=1.0, breath_color="7fd1ff",
         breath_enabled=True, breath_floor=0.1, breath_flash_ms=180,
         breath_hold_ms=120, breath_scrim_ms=200,
         tray_color="0e1117", tray_opacity=86, tray_border=True, tray_blur=18,
         editor_dim_alpha=118, card_bg_enabled=False, card_bg_mode="solid",
         card_bg_color="0e1117", card_bg_image="",
         card_fill_color="141821", card_fill_alpha=165)
app_stub = NS(config_manager=NS(config=cfg), _window=None)
overlay_stub = NS(faq=lambda: None, open_layout=lambda: None,
                  _apply_window_border=lambda: None)

print("== 1. 构造面板 ==")
panel = BreathingEditPanel(app_stub, overlay_stub)
check("BreathingEditPanel 构造成功", panel is not None)
check("顶部预览是 _BreathHeadPreview",
      isinstance(panel._preview, _BreathHeadPreview),
      type(panel._preview).__name__)
check("预览自带 _BreathHeadCanvas",
      isinstance(panel._preview._canvas, _BreathHeadCanvas))
check("旧的 _sim 已不存在", not hasattr(panel, "_sim"))
check("旧的 _preview_breath 已不存在", not hasattr(panel, "_preview_breath"))

print("== 2. 8 项参数控件与取值范围 ==")
for name, lo, hi in (("_period_slider", 5, 80), ("_max_slider", 25, 100),
                     ("_floor_slider", 0, 100), ("_flash_slider", 60, 600),
                     ("_hold_slider", 0, 600), ("_scrim_slider", 0, 800)):
    sl = getattr(panel, name, None)
    good = sl is not None and (sl.minimum(), sl.maximum()) == (lo, hi)
    check(f"{name} 范围 {lo}-{hi}", good,
          None if sl is None else (sl.minimum(), sl.maximum()))
for name in ("_enabled_btn", "_custom_btn", "_follow_btn"):
    check(f"{name} 存在", getattr(panel, name, None) is not None)
check("色板数量 12", len(panel._swatches) == 12, len(panel._swatches))

print("== 3. refresh() ==")
try:
    panel.refresh()
    check("refresh() 无异常", True)
except Exception as e:                               # noqa: BLE001
    check("refresh() 无异常", False, repr(e))

print("== 4. 参数推送到预览（旧 _on_period 漏推 bug）==")
panel._period_slider.setValue(80)                    # 8.0 s
check("改周期后预览同步", abs(panel._preview._period - 8.0) < 1e-6,
      panel._preview._period)
panel._max_slider.setValue(50)
check("改亮度后预览同步", abs(panel._preview._peak - 0.5) < 1e-6,
      panel._preview._peak)
panel._scrim_slider.setValue(700)
check("改黑罩后预览同步", panel._preview._scrim_ms == 700,
      panel._preview._scrim_ms)
panel._period_slider.setValue(24)
check("周期改回后预览同步", abs(panel._preview._period - 2.4) < 1e-6,
      panel._preview._period)

print("== 5. 预览绘制（模拟窗口 + 两张波形）==")
panel.resize(760, 720)
panel.show()
app.processEvents()
panel._preview._canvas.resize(700, 110)
panel._preview._canvas.repaint()
app.processEvents()
cv0 = panel._preview._canvas
dpr0 = max(1.0, float(cv0.devicePixelRatio()))
pm0 = QPixmap(int(cv0.width() * dpr0), int(cv0.height() * dpr0))
pm0.setDevicePixelRatio(dpr0)
pm0.fill(QColor(0, 0, 0))
cv0.render(pm0, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
img = pm0.toImage()
non_bg = 0
for y in range(0, img.height(), 2):
    for x in range(0, img.width(), 2):
        c = img.pixelColor(x, y)
        if c.red() + c.green() + c.blue() > 40:      # 亮于纯黑背景
            non_bg += 1
check("画布画出了内容（非纯黑像素数）", non_bg > 200, f"{non_bg} px")

print("== 6. 波形随周期变化（绝对时间轴，不是归一化）==")
def wave_pixels(period_tenths):
    """返回「常态呼吸波形区」内亮蓝像素总数。

    用 ``QWidget.render(pm)`` 强制重绘到指定 QPixmap —— 不能用 ``grab()``，
    offscreen 下它会拿到上一帧缓存（实测三种周期指纹完全相同，全是旧图）。
    统计总数而非逐列位置：周期越短，曲线上下振荡越剧烈、路径越长，
    像素总数越大；若轴被归一化，不同周期的总数会完全一样。
    """
    panel._period_slider.setValue(period_tenths)
    app.processEvents()
    cv = panel._preview._canvas
    dpr = max(1.0, float(cv.devicePixelRatio()))
    pm = QPixmap(int(cv.width() * dpr), int(cv.height() * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(QColor(0, 0, 0))
    # 关键：只画 DrawChildren，**不要**默认的 DrawWindowBackground —— 它会把
    # widget 调色板背景（offscreen 下是浅灰 239,239,239）刷满整个 pixmap，
    # 把我填的黑底盖掉，导致每个像素都"亮蓝"、统计恒等于区域总像素数。
    cv.render(pm, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
    im = pm.toImage()
    # 呼吸波形位于画布右侧约 59%..98%（左侧是模拟窗口 + 焦点瞬态）
    x0, x1 = int(im.width() * 0.60), int(im.width() * 0.97)
    cnt = 0
    for x in range(x0, x1):
        for y in range(im.height()):
            if im.pixelColor(x, y).blue() > 150:     # 曲线实色，避开网格/谷值线
                cnt += 1
    return cnt


s05 = wave_pixels(5)      # 0.5 s  -> 窗口内 16 个周期
s24 = wave_pixels(24)     # 2.4 s  -> 约 3.3 个周期
s80 = wave_pixels(80)     # 8.0 s  -> 1 个周期
check("0.5s 与 2.4s 波形不同", s05 != s24, f"{s05} vs {s24}")
check("2.4s 与 8.0s 波形不同", s24 != s80, f"{s24} vs {s80}")
check("周期越短曲线像素越多（真实缩放）", s05 > s24 > s80,
      f"0.5s={s05} 2.4s={s24} 8.0s={s80}")

print("== 7. nav_items 与视觉顺序 ==")
try:
    items = panel.nav_items()
    check("nav_items() 无异常", True, f"{len(items)} 项")
    widgets = [it.widget for it in items]
    idx = {}
    for i, wgt in enumerate(widgets):
        for key, obj in (("enabled", panel._enabled_btn),
                         ("custom", panel._custom_btn),
                         ("period", panel._period_slider),
                         ("max", panel._max_slider),
                         ("floor", panel._floor_slider),
                         ("flash", panel._flash_slider),
                         ("hold", panel._hold_slider),
                         ("scrim", panel._scrim_slider)):
            if wgt is obj and key not in idx:
                idx[key] = i
    order = ["enabled", "custom", "period", "max", "floor", "flash",
             "hold", "scrim"]
    got = [k for k in order if k in idx]
    check("八项均已注册导航", len(got) == 8, got)
    seq = [idx[k] for k in got]
    check("导航顺序 = 视觉顺序（递增）", seq == sorted(seq), seq)
except Exception as e:                               # noqa: BLE001
    check("nav_items() 无异常", False, repr(e))

print("== 8. 演示按钮 ==")
try:
    panel._preview.demo_gain()
    app.processEvents()
    panel._preview.demo_lose()
    app.processEvents()
    check("演示 获得/失去焦点 无异常", True)
except Exception as e:                               # noqa: BLE001
    check("演示 获得/失去焦点 无异常", False, repr(e))

print()
print("VERDICT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
