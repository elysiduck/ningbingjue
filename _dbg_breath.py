import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel
from PyQt6.QtCore import Qt, QSize, QPoint
from PyQt6.QtGui import QColor, QImage, QPainter

# Stubs -----------------------------------------------------------------
class Cfg:
    breath_period_s = 2.36
    breath_speed = 50
    breath_max_brightness = 1.0
    breath_color = ""
    breath_enabled = True
    tray_color = "0e1117"
    tray_opacity = 86
    tray_border = True
    tray_blur = 18
    editor_dim_alpha = 118

class ConfigManager:
    config = Cfg()

class App:
    config_manager = ConfigManager()
    nav = None
    blocks = {}
    def render_pages(self):
        return []

class Overlay:
    def open_layout(self): pass
    def open_breathing(self): pass
    def faq(self): return None
    def _apply_window_border(self): pass

# Import the real panel -------------------------------------------------
import ningbingjue.ui.edit_overlay as m

app = QApplication(sys.argv)
win = QWidget()
win.setFixedSize(420, 640)
ov = Overlay()
a = App()
panel = m.BreathingEditPanel(a, ov)
print("constructed BreathingEditPanel OK")
try:
    panel.refresh()
    print("refreshed OK")
except Exception as e:
    print("REFRESH THREW:", repr(e))

# Put panel in a window + show to force layout
panel.setParent(win)
lay = QVBoxLayout(win)
lay.setContentsMargins(0,0,0,0)
lay.addWidget(panel)
win.show()
app.processEvents()
win.repaint()
app.processEvents()

print("panel geometry:", panel.geometry())
pv = getattr(panel, "_preview", None)
if pv is not None:
    print("_preview geometry:", pv.geometry(), "minH=", pv.minimumHeight())
tp = getattr(panel, "_tray_preview", None)
if tp is not None:
    print("_tray_preview geometry:", tp.geometry())
scroll = None
# find scroll area
for c in panel.children():
    from PyQt6.QtWidgets import QScrollArea
    if isinstance(c, QScrollArea):
        scroll = c
        print("scroll geometry:", c.geometry(), "viewport:", c.viewport().geometry())
        print("scroll content:", c.widget().geometry() if c.widget() else None)

# Render the panel composited over an opaque dark tray bg (simulate main window)
W, H = panel.width(), panel.height()
img = QImage(W, H, QImage.Format.Format_ARGB32)
img.fill(QColor(14, 17, 23))  # tray base #0e1117
p = QPainter(img)
panel.render(p, QPoint(0, 0))
p.end()
out = os.path.join(os.path.dirname(__file__), "_dbg_breath_render.png")
img.save(out)
print("saved render:", out, img.size())
# also count light (text-ish) pixels to confirm controls rendered
light = 0
for y in range(0, H, 3):
    for x in range(0, W, 3):
        c = img.pixelColor(x, y)
        if c.red() > 120 and c.green() > 120 and c.blue() > 120:
            light += 1
print("light-ish px (controls/text):", light)
print("DONE")
