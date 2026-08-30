import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication, QWidget, QWidgetItem
from PyQt6.QtCore import QRect
from ningbingjue.ui.gamepad_hint import HintButton

qapp = QApplication.instance() or QApplication([])

# 模拟: 自动锁帧 fixedWidth 149
src = HintButton("自动锁帧：关")
src.setStyleSheet("QPushButton{padding:5px 12px;font-size:11px;}")
src.setFixedWidth(149)
dst = HintButton("自动锁帧：关")
dst.setStyleSheet("QPushButton{padding:5px 12px;font-size:11px;}")
dst.setFixedWidth(149)
print("dst shW=%d minW=%d maxW=%d" % (dst.sizeHint().width(), dst.minimumWidth(), dst.maximumWidth()))

# 包一层容器(QHBoxLayout sizeHint 视角), 再走 QWidgetItem.setGeometry
item = QWidgetItem(dst)
w = QWidget(); w.resize(400, 300); w.show()
item.setGeometry(QRect(0, 0, 109, 30))
qapp.processEvents()
print("QWidgetItem.setGeometry(109): actual width=%d" % dst.width())
