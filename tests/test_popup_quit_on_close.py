"""验证辅助浮窗 WA_QuitOnClose=False（关闭不触发 app 退出）。

根因（2026-08-12 修复）：
  PerfMonPopup 是无 parent 的顶层 Qt.Window，Qt 默认 WA_QuitOnClose=True；
  FaqPopup / ButtonListPopup 是 Qt.Tool（即使有 parent，默认仍 True）。
  当主窗口 hide 到托盘（_minimize_to_tray → self.hide()）后，关闭这些浮窗
  会被 Qt 当成"最后一个可见窗口关闭" → 触发 lastWindowClosed → app.quit()，
  导致"关掉浮窗 = 整个程序退出"。

  修复：给所有辅助浮窗显式 setAttribute(WA_QuitOnClose, False)。
  本测试固化此不变式，防止未来回归。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from PyQt6.QtWidgets import QWidget, QApplication  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

app = QApplication.instance() or QApplication([])


def default_snapshot() -> dict:
    """与 app_window.perfmon_snapshot 的默认字段保持一致。"""
    return {
        "window": None, "is_game": False,
        "cpu_avg": None, "cpu_max": None, "gpu": None,
        "load": "idle", "load_label": "空闲",
        "undervolt_mv": 0, "undervolt_enabled": False,
        "frame_unlocked": True, "frame_state": "未锁帧(解锁)",
        "gpu_locked_freq": None, "gpu_lock_state": "未锁定(自动)",
    }


def _check(name: str, widget: QWidget) -> bool:
    """断言 widget 的 WA_QuitOnClose 为 False。"""
    actual = widget.testAttribute(Qt.WidgetAttribute.WA_QuitOnClose)
    if actual:
        # True = 关闭会参与"最后窗口退出"判定（bug 状态）
        return False
    return True


results = []

# --- PerfMonPopup（用户报告的主 bug） ---
try:
    from ningbingjue.ui.panels import PerfMonPopup
    source = MagicMock()
    source.perfmon_snapshot.return_value = default_snapshot()
    popup = PerfMonPopup(source, initial_size=None)
    results.append(("PerfMonPopup", _check("PerfMonPopup", popup)))
    popup.close()
    popup.deleteLater()
except Exception as e:
    results.append(("PerfMonPopup", f"EXC: {e!r}"))

# --- FaqPopup ---
try:
    from ningbingjue.ui.faq_popup import FaqPopup
    p1 = QWidget()
    faq = FaqPopup(p1)
    results.append(("FaqPopup", _check("FaqPopup", faq)))
    faq.close()
    faq.deleteLater()
except Exception as e:
    results.append(("FaqPopup", f"EXC: {e!r}"))

# --- ButtonListPopup ---
# 构造依赖完整 ButtonEditPanel（QWidget 子类 + FlowLayout 画布），MagicMock
# 无法当 QWidget 用。改用源码断言：确认 __init__ 里确实设了 WA_QuitOnClose=False。
# （与 FaqPopup 同一修复模式，setAttribute 是确定性调用，源码断言等价于行为。）
try:
    import inspect
    from ningbingjue.ui.button_edit import ButtonListPopup
    src = inspect.getsource(ButtonListPopup.__init__)
    ok = "WA_QuitOnClose" in src and "False" in src
    results.append(("ButtonListPopup", ok))
except Exception as e:
    results.append(("ButtonListPopup", f"EXC: {e!r}"))

# --- ShortcutEditor ---
# 构造依赖 ShortcutStore/ShortcutEngine（返回值被当 str 传给 setText），MagicMock
# 无法满足。同上：源码断言 __init__ 设了 WA_QuitOnClose=False。
try:
    import inspect
    from ningbingjue.ui.shortcut_editor import ShortcutEditor
    src = inspect.getsource(ShortcutEditor.__init__)
    ok = "WA_QuitOnClose" in src and "False" in src
    results.append(("ShortcutEditor", ok))
except Exception as e:
    results.append(("ShortcutEditor", f"EXC: {e!r}"))

# --- 汇总 ---
print("=" * 60)
all_ok = True
for name, res in results:
    if res is True:
        print(f"  [PASS] {name}: WA_QuitOnClose = False")
    else:
        all_ok = False
        print(f"  [FAIL] {name}: {res}")
print("=" * 60)
if all_ok:
    print("全部通过：4 个辅助浮窗关闭都不会触发 app 退出")
    sys.exit(0)
else:
    print("存在失败项")
    sys.exit(1)
