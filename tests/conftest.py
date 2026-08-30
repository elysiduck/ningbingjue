"""Shared pytest fixtures and fake UI/control helpers (headless)."""

from __future__ import annotations

import pytest

from ningbingjue.core.color.state import reset_state


@pytest.fixture(scope="session", autouse=True)
def _shared_qapp():
    """会话级单例 ``QApplication``，消除 offscreen 下每测试销毁 QApplication 触发的 C 级硬崩。

    原先每个 GUI 测试各自 ``QApplication.instance() or QApplication([])``，
    测试函数返回后 pytest 的隔离清理会在 Qt 子对象仍引用 ``QApplication`` 时把它
    GC 掉，触发 C++ 层崩溃（进程直接 abort，无 traceback，独立脚本同样代码路径却不崩）。
    改为会话级单例并持引用：所有测试内部的 ``QApplication.instance() or ...`` 自动复用，
    每测试结束不再销毁 QApplication，崩溃消除。会话结束也不主动 ``quit()/destroy``，
    交给进程退出清理（与独立脚本一致，安全）。
    """
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    # 故意不在会话结束主动销毁：进程退出时 Qt 自行清理，避免再次触发 teardown 崩溃。


@pytest.fixture(autouse=True)
def _reset_color_state():
    reset_state()
    yield
    reset_state()


@pytest.fixture(autouse=True)
def _auto_dismiss_admin_warning():
    """测试环境无人点击模态框：自动接受 ``showEvent`` 里的『需要管理员权限』警告。

    该警告是 ``QMessageBox.warning``（模态），在测试进程非管理员运行时弹出，
    而测试函数不跑事件循环、也无人点击，会在 ``boot()`` 的 ``show()`` 调用里
    永久阻塞嵌套事件循环，导致所有基于 ``boot()`` 的测试挂死。
    真实交互式运行不受影响——这里仅在测试内、且仅针对该特定标题的框自动 accept。
    不依赖 ``qapp`` 夹具（部分测试文件未定义它），直接用 ``QApplication.instance()``。
    """
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication, QMessageBox

    def _poll() -> None:
        app = QApplication.instance()
        if app is None:
            return
        for w in app.topLevelWidgets():
            if isinstance(w, QMessageBox) and w.windowTitle() == "需要管理员权限":
                w.accept()

    # Parent the timer to the shared QApplication so its C++ side is torn down
    # *with* the app (correct order) instead of dangling until Python exit,
    # which otherwise causes a "Windows fatal exception: access violation"
    # when the interpreter destroys globals after Qt is already gone.
    app = QApplication.instance()
    timer = QTimer(app) if app is not None else QTimer()
    timer.setInterval(30)
    timer.timeout.connect(_poll)
    timer.start()
    try:
        yield
    finally:
        # The QApplication (and thus this parentless timer's C++ side) may
        # already be torn down by the time fixture teardown runs, raising
        # "wrapped C/C++ object ... has been deleted".  That is harmless here —
        # tolerate it so a passed test is never reported as an error.
        try:
            timer.stop()
        except Exception:
            pass


class Point:
    def __init__(self, x: int, y: int) -> None:
        self.x = x
        self.y = y


class FakeBtn:
    """Minimal stand-in for a RoundedButton-like control."""

    def __init__(self, col=None, row=None, location=None) -> None:
        self.text = ""
        self._colors = None
        self.custom_styled = False
        self.border_color = None
        self.border_thickness = None
        self.fore_color = None
        self.ctrl_col = col
        self.ctrl_row = row
        self.focused = False
        self.invalidated = 0
        self.location = location

    def set_colors(self, *args) -> None:
        self._colors = args

    def set_focus_visual(self, focused: bool) -> None:
        self.focused = focused

    def invalidate(self) -> None:
        self.invalidated += 1


class FakeLabel:
    def __init__(self) -> None:
        self.text = ""
        self.fore_color = None


class FakePanel:
    def __init__(self, x: int = 0, w: int = 240, location: Point | None = None) -> None:
        self.location = location if location is not None else Point(x, 0)
        self.width = w
        self.back_color = None
        self.visible = False


@pytest.fixture
def make_rtss_ui():
    def _make() -> dict:
        track = FakePanel(100, 240)
        thumb = FakeBtn(col=1, row=0, location=Point(0, 55))
        lock = FakeBtn(col=0, row=0)
        return {
            "card": object(),
            "btn_lock_status": lock,
            "lbl_lock_status_right": FakeLabel(),
            "pnl_slider_track": track,
            "pnl_slider_fill": FakePanel(100, 120),
            "btn_slider_thumb": thumb,
            "lbl_fps_value": FakeLabel(),
            "lbl_rtss_title": FakeLabel(),
            "toast_panel": FakePanel(),
            "toast_label": FakeLabel(),
        }

    return _make
