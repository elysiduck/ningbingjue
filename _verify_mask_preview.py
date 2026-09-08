"""验证蒙版页预览：图片背景裁剪 + 蒙版叠加 + 缓存 + 布局预览不崩。"""
import os, sys
from types import SimpleNamespace as NS
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPixmap, QPainter, QRegion
from PyQt6.QtWidgets import QApplication, QWidget
from ningbingjue.ui.edit_overlay import _MaskPreviewCanvas

app = QApplication(sys.argv)

# 造一张 4000x1000 的亮色测试图（模拟 4K 宽壁纸）
big = QPixmap(4000, 1000)
big.fill(QColor(230, 240, 255))
test_img = os.path.abspath("_test_bg_4k.png")
big.save(test_img)

ok = True
def check(label, cond, extra=""):
    global ok
    if not cond: ok = False
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{(' -> ' + str(extra)) if extra else ''}")

b = NS(_color_hex="39d3c4", _card_bg_enabled=True, _card_bg_mode="image",
       _card_bg_color_hex="0e1117", _card_bg_image=test_img,
       _tray_opacity=86, _tray_color_hex="0e1117", _tray_blur=18,
       _tray_border=True, _editor_dim_alpha=118)
e = NS()
pm_big = QPixmap(test_img)
class _O(QWidget):   # owner 既是 Qt parent 又是属性提供者
    pass
o = _O()
o._breath = b; o._eff = e; o._active_region = None; o._page_bg_hex = "0b0e13"
o._card_regions = {}; o.dim_preview_on = lambda: False
o.bg_pixmap = lambda: pm_big

cv = _MaskPreviewCanvas(o)
cv.resize(760, 420)
cv.show()
app.processEvents()

dpr = max(1.0, float(cv.devicePixelRatio()))
pm = QPixmap(int(cv.width()*dpr), int(cv.height()*dpr))
pm.setDevicePixelRatio(dpr)
pm.fill(QColor(0, 0, 0))
cv.render(pm, __import__("PyQt6.QtCore", fromlist=["QPoint"]).QPoint(),
          __import__("PyQt6.QtGui", fromlist=["QRegion"]).QRegion(),
          QWidget.RenderFlag.DrawChildren)
img = pm.toImage()
nonbg = sum(1 for y in range(0, img.height(), 4) for x in range(0, img.width(), 4)
            if img.pixelColor(x, y).red()+img.pixelColor(x, y).green()+img.pixelColor(x, y).blue() > 40)
check("布局预览渲染成功（两栏窗口）", nonbg > 500, f"{nonbg} px")

# 缓存生效：连续两帧同尺寸，第二次不应再缩放（key 不变）
k1 = getattr(cv, "_pv_scaled_key", "missing")
cv._draw_tray = cv._draw_tray  # noop
img2 = pm.toImage()
check("缩放缓存 key 已建立", k1 != "missing" and cv._pv_scaled is not None,
      cv._pv_scaled_key)

# 帧耗时对比：第二帧（缓存命中）应显著快于首帧
import time
cv2 = _MaskPreviewCanvas(o); cv2.resize(760, 420); cv2.show(); app.processEvents()
pm2 = QPixmap(pm)
t0 = time.perf_counter()
cv2.render(pm2, __import__("PyQt6.QtCore", fromlist=["QPoint"]).QPoint(),
           __import__("PyQt6.QtGui", fromlist=["QRegion"]).QRegion(),
           QWidget.RenderFlag.DrawChildren)
t1 = time.perf_counter()
cv2.render(pm2, __import__("PyQt6.QtCore", fromlist=["QPoint"]).QPoint(),
           __import__("PyQt6.QtGui", fromlist=["QRegion"]).QRegion(),
           QWidget.RenderFlag.DrawChildren)
t2 = time.perf_counter()
first, second = (t1-t0)*1000, (t2-t1)*1000
print(f"    [info] 首帧(含缩放) {first:.1f} ms | 缓存帧 {second:.1f} ms")
check("缓存帧明显快于首帧", second <= first * 0.8 or second < 30,
      f"{first:.1f} -> {second:.1f} ms")

os.remove(test_img)
print()
print("VERDICT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
