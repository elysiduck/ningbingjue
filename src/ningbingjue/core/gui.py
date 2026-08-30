"""跨线程 UI 马歇尔：保证 Qt 控件只被主线程触碰（修复后台线程直写 QWidget 的原生崩溃）。

背景：auto_lock / GPU 浮动 等由 MetricSampler 后台 reader 线程驱动，状态翻转时
会调用 show_toast()/apply_*()，进而设置控件。Qt 规定 GUI 操作只能跑在主线程，
后台线程直接写控件会损坏 Qt 内部状态 → 间歇性原生崩溃（无 Python 栈、faulthandler
也抓不到）。本模块把所有「会碰控件的调用」马歇尔到主线程：主线程调用同步执行、
后台线程调用经 Qt 跨线程信号队列到主线程。

使用前必须在主线程（QApplication 创建后）调用一次 ``ensure_gui_bridge()``
（见 ``ui/app_window.run_app``）。rtss 与 power 两块共用本模块，避免各自维护一份
永不初始化的桥——历史上 ``ensure_gui_bridge`` 从未被调用，导致 ``@_gui_method``
形同虚设，后台线程仍直接碰控件。
"""
from __future__ import annotations

import functools
from typing import Callable, Any

_GUI_BRIDGE: Any = None  # 模块级单例；必须在主线程创建（见 ensure_gui_bridge）


def ensure_gui_bridge() -> None:
    """GUI 启动早期（主线程、QApplication 已存在）调用：创建马歇尔 QObject。

    后台线程经 ``_post_to_gui`` 发出的回调由 Qt 自动队列到主线程执行。
    非 GUI 模式（headless 自检）不要调用——不会 import PyQt6，且无 QApplication；
    本函数内部也会在 QApplication 缺失时安全 no-op。幂等：重复调用只建一次。
    """
    global _GUI_BRIDGE
    if _GUI_BRIDGE is not None:
        return
    try:
        from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot
        from PyQt6.QtWidgets import QApplication
    except Exception:  # noqa: BLE001  headless 自检不 import PyQt6
        return
    if QApplication.instance() is None:
        return

    class _GuiBridge(QObject):
        _invoke = pyqtSignal(object)

        def __init__(self) -> None:
            super().__init__()
            self._invoke.connect(self._dispatch)

        @pyqtSlot(object)
        def _dispatch(self, fn) -> None:
            try:
                fn()
            except Exception:  # noqa: BLE001  后台回调异常不应拖垮主线程
                pass

        def post(self, fn) -> None:
            self._invoke.emit(fn)

    _GUI_BRIDGE = _GuiBridge()


def _post_to_gui(fn) -> None:
    """主线程→立即执行；后台线程→队列到主线程；headless/桥未就绪→同步执行。

    后台线程的 singleShot/直接调用都不可靠（worker 线程无 Qt 事件循环，
    singleShot 不会触发），故改用「主线程 QObject + 跨线程信号」这一唯一稳妥路径。
    """
    try:
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtCore import QThread
    except Exception:  # noqa: BLE001
        fn()
        return
    app = QApplication.instance()
    if app is None or _GUI_BRIDGE is None:
        fn()
        return
    if QThread.currentThread() is app.thread():
        fn()
        return
    _GUI_BRIDGE.post(fn)


def _gui_method(fn: Callable) -> Callable:
    """方法装饰器：把会碰 Qt 控件的方法体约束到主线程执行（后台线程则队列）。"""
    @functools.wraps(fn)
    def wrapper(self, *a, **k):
        _post_to_gui(lambda: fn(self, *a, **k))
    return wrapper
