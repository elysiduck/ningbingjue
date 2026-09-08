import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication, QWidget
from PyQt6.QtCore import QRect
from ningbingjue.ui.gamepad_hint import HintButton

qapp = QApplication.instance() or QApplication([])

# 构造一个"被 stretch 拉宽"的 HintButton：无显式宽度约束，布局后宽 392
src = HintButton("应用")
src.setStyleSheet("QPushButton{padding:5px 12px;font-size:11px;}")
src.setFixedSize(392, 30)
print("src: width=%d minW=%d maxW=%d shW=%d" % (
    src.width(), src.minimumWidth(), src.maximumWidth(), src.sizeHint().width()))

# 模拟 _copy_size
dst = HintButton("应用")
dst.setFixedSize(src.width(), src.height())
print("dst fixed: minW=%d maxW=%d shW=%d" % (
    dst.minimumWidth(), dst.maximumWidth(), dst.sizeHint().width()))

# 直接 setGeometry 验证 clamp
w = QWidget()
w.resize(400, 200)
w.show()
dst.setParent(w)
qapp.processEvents()
dst.setGeometry(QRect(0, 0, dst.sizeHint().width(), dst.sizeHint().height()))
qapp.processEvents()
print("dst after setGeometry(60,30): actual width=%d" % dst.width())
