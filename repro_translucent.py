import sys, os
sys.path.insert(0, "src")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PyQt6.QtWidgets import QApplication, QWidget
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPainter, QPainterPath, QColor
app = QApplication([])
w = QWidget()
w.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
w.resize(300, 200)
def paint(ev):
    p = QPainter(w)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath(); path.addRoundedRect(0,0,w.width(),w.height(),16,16)
    p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(20,24,33,205)); p.drawPath(path)
    p.end()
w.paintEvent = paint
w.show()
print("before PE")
app.processEvents()
print("after PE OK")
