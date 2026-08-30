# -*- coding: utf-8 -*-
"""Wiring test for the page-transition hooks on _OverlayWindow (2026-08-31).

为什么不是「真窗口端到端」测试
--------------------------------
本沙箱无法创建真 _OverlayWindow（offscreen 下 build_overlay 直接挂死 /
RC=127，与既有 test_app_window_gui 同一现象，属环境限制，项目已知）。

所以这里测的是**最危险的那部分逻辑**——守卫延期与复位：

    _arm_page_transition →（rebuild finally 分支）→ _start_page_transition
                                                  → _finish_page_transition
                                                  → _release_transition_guard

守卫一旦泄漏，手柄轮询会永久 no-op（整机输入失灵），是本次改动唯一「严重」级
风险。做法：造一个 stub self，只做 Qt C++ 层初始化就跳过 _OverlayWindow 那套
重建，然后把**真实的方法**绑上去跑。

覆盖：
1. 正常转场：守卫在动画全程保持置位，动画结束后复位 + 重启手柄轮询。
2. 保底定时器：动画回调丢失时守卫仍会被强制复位（不会永久卡死）。
3. 全量重建打断：_cancel_page_transition 清状态但不提前重启轮询。
4. 方向判定：含末页→首页的环绕「走近路」。
5. none 模式 / 单页 / 首次显示：完全不置位，零副作用。
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "src"))

from PyQt6.QtGui import QColor, QPixmap  # noqa: E402
from PyQt6.QtWidgets import QApplication, QScrollArea, QWidget  # noqa: E402

from ningbingjue.ui.app_window import _OverlayWindow  # noqa: E402

qt = QApplication.instance() or QApplication([])

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAILURES.append(name)


def spin(s=0.4):
    end = time.monotonic() + s
    while time.monotonic() < end:
        qt.processEvents()
        time.sleep(0.004)


def make_pm(c="#204080"):
    pm = QPixmap(80, 60)
    pm.fill(QColor(c))
    return pm


class FakeCfg:
    def __init__(self):
        self.page_transition = "slide"
        self.transition_ms = 60


class FakeNav:
    def __init__(self):
        self.current_page = 1


class FakeCM:
    def __init__(self):
        # ⚠️ 必须每个实例一份：写成类属性会让所有 StubWin 共享同一个 config，
        # 前一个用例改的 page_transition / transition_ms 会泄漏到后面的用例
        # （例如「none 模式」用例把模式改成 none，后续用例就全都不置位了）。
        self.config = FakeCfg()


class FakeApp:
    def __init__(self, n_pages=3):
        self._n = n_pages
        self.config_manager = FakeCM()
        self.nav = FakeNav()

    def render_pages(self):
        # 每页塞一个假 token，页数由 _n 决定
        return [["card%d" % i] for i in range(self._n)]


class FakeController:
    def __init__(self):
        self.rebuilding = False
        self.gp_starts = 0

    def set_rebuilding(self, v):
        self.rebuilding = bool(v)

    def start_gamepad(self):
        self.gp_starts += 1


#: 要挂到 stub 上的**真实**方法（来自 _OverlayWindow，一行不改地跑）。
_WIRED_METHODS = (
    "_transition_mode",
    "_transition_ms",
    "_page_viewport_rect",
    "_grab_page_snapshot",
    "_arm_page_transition",
    "_show_transition_cover",
    "_start_page_transition",
    "_teardown_veil",
    "_cancel_page_transition",
    "_finish_page_transition",
    "_on_page_transition_finished",
    "_page_transition_safety",
    "_release_transition_guard",
)


class StubWin(QWidget):
    """承接真实转场方法的宿主。

    刻意**不继承** _OverlayWindow——那会连带继承它的 paintEvent / resizeEvent
    / showEvent，而这些都依赖 __init__ 建好的一整套状态，在只做裸初始化时会
    直接把进程带崩（实测 RC=127）。这里用普通 QWidget 承接，只把被测的 12 个
    方法原样绑上去，其余 Qt 行为保持纯净。
    """

    def __init__(self, n_pages=3, cur=0):
        QWidget.__init__(self)
        self._app = FakeApp(n_pages)
        self._controller = FakeController()
        self._editor_all_pages = False
        self._current_page_idx = cur
        self._pending_old_snapshot = None
        self._pending_transition_dir = 1
        self._transition_active = False
        self._rebuild_deferred_ctrl = None
        self._rebuild_deferred_gp = False
        self._page_veil = None
        self._page_transition = None
        self._page_indicator = None
        self.inner_layout = None
        self._grab_calls = 0
        # 真滚动区：让 _page_viewport_rect 的 mapTo 走真实几何
        self._scroll = QScrollArea(self)
        self._scroll.resize(120, 100)
        self._scroll.show()
        self.resize(200, 200)
        self.show()

    # 用真 QPixmap 代替真实窗口抓图（真窗口在本沙箱建不出来）
    def grab(self, rect=None):
        self._grab_calls += 1
        return make_pm()


# 把真实方法绑上去（必须在实例化之前）
for _n in _WIRED_METHODS:
    setattr(StubWin, _n, getattr(_OverlayWindow, _n))


def simulate_rebuild_finally(w):
    """复刻 rebuild() finally 里那段「守卫延期」分支。"""
    _ctrl = w._controller
    _was_gp_active = True
    if getattr(w, "_transition_active", False):
        w._rebuild_deferred_ctrl = _ctrl
        w._rebuild_deferred_gp = _was_gp_active
    else:
        _ctrl.set_rebuilding(False)
        if _was_gp_active:
            _ctrl.start_gamepad()


# ---------------------------------------------------------------------------
print("1. 正常转场：守卫全程置位，动画结束后复位")
w = StubWin(n_pages=3, cur=0)
w._app.nav.current_page = 1
w._controller.set_rebuilding(True)

w._arm_page_transition()
check("arm 后 _transition_active 置位", w._transition_active is True)
check("arm 后抓到旧页快照", w._pending_old_snapshot is not None)
check("arm 后方向为 +1（0→1）", w._pending_transition_dir == 1,
      "dir=%d" % w._pending_transition_dir)
# 盖罩（cover veil）必须在 rebuild 之前就盖上：否则 rebuild 那一帧会把新页
# 真身合成出来，出现「先跳到第二页、再瞬回第一页」的闪烁。
check("arm 后已盖上旧页盖罩", w._page_veil is not None)
check("盖罩只有旧页快照（无新页）",
      getattr(w._page_veil, "_old_pm", None) is not None
      and getattr(w._page_veil, "_new_pm", None) is None)

simulate_rebuild_finally(w)
check("rebuild finally 把守卫延期（仍置位）",
      w._controller.rebuilding is True and w._controller.gp_starts == 0,
      "rebuilding=%r gp_starts=%d" % (w._controller.rebuilding, w._controller.gp_starts))

_cover_before = w._page_veil
w._start_page_transition()
check("start 复用了同一个盖罩（不是新建 veil）", w._page_veil is _cover_before)
check("start 后补上了新页快照", getattr(w._page_veil, "_new_pm", None) is not None)
check("start 后创建了 veil", w._page_veil is not None)
check("start 后动画在跑", w._page_transition is not None
      and w._page_transition.running)
spin(0.5)
check("动画结束后 veil 已销毁", w._page_veil is None)
check("动画结束后 _transition_active 复位", w._transition_active is False)
check("动画结束后守卫复位", w._controller.rebuilding is False)
check("动画结束后手柄轮询重启一次", w._controller.gp_starts == 1,
      "gp_starts=%d" % w._controller.gp_starts)
check("延期状态已清空", w._rebuild_deferred_ctrl is None
      and w._rebuild_deferred_gp is False)

# ---------------------------------------------------------------------------
print("2. 保底定时器：动画回调丢失时守卫仍会复位")
w2 = StubWin(n_pages=3, cur=0)
w2._app.nav.current_page = 1
w2._controller.set_rebuilding(True)
w2._arm_page_transition()
simulate_rebuild_finally(w2)
check("守卫已延期", w2._controller.rebuilding is True)
# 故意不调 _start_page_transition（模拟动画回调丢失）
# 保底 = transition_ms(60) + SAFETY_MARGIN(800)
spin(1.4)
check("保底定时器强制复位守卫", w2._controller.rebuilding is False)
check("保底定时器重启了手柄轮询", w2._controller.gp_starts == 1,
      "gp_starts=%d" % w2._controller.gp_starts)
check("保底后 _transition_active 复位", w2._transition_active is False)

# ---------------------------------------------------------------------------
print("3. 全量重建打断：清状态但不提前重启轮询")
w3 = StubWin(n_pages=3, cur=0)
w3._app.nav.current_page = 1
w3._controller.set_rebuilding(True)
w3._arm_page_transition()
simulate_rebuild_finally(w3)
w3._cancel_page_transition()   # _teardown_all_pages 会调它
check("cancel 后 _transition_active 复位", w3._transition_active is False)
check("cancel 后待用快照清空", w3._pending_old_snapshot is None)
check("cancel 后延期状态清空", w3._rebuild_deferred_ctrl is None)
check("cancel 不提前重启轮询（留给 rebuild finally）",
      w3._controller.rebuilding is True and w3._controller.gp_starts == 0,
      "rebuilding=%r gp_starts=%d" % (w3._controller.rebuilding, w3._controller.gp_starts))
simulate_rebuild_finally(w3)   # 此时 _transition_active 已 False → 正常复位
check("随后的 rebuild finally 正常复位", w3._controller.rebuilding is False
      and w3._controller.gp_starts == 1)
spin(1.2)   # 保底定时器到点应 no-op，不应重复重启
check("保底定时器到点不重复重启轮询", w3._controller.gp_starts == 1,
      "gp_starts=%d" % w3._controller.gp_starts)

# ---------------------------------------------------------------------------
print("4. 方向判定（含环绕走近路）")
w4 = StubWin(n_pages=3, cur=2)
w4._app.nav.current_page = 0     # 末页 → 首页
w4._arm_page_transition()
check("末页→首页 方向为 +1（走近路）", w4._pending_transition_dir == 1,
      "dir=%d" % w4._pending_transition_dir)
w4._app.nav.current_page = 1     # 2 → 1 后退
w4._arm_page_transition()
check("末页→中间页 方向为 -1", w4._pending_transition_dir == -1,
      "dir=%d" % w4._pending_transition_dir)

# ---------------------------------------------------------------------------
print("5. 不置位的场景：none 模式 / 单页 / 首次显示 / 编辑器全页")
w5 = StubWin(n_pages=3, cur=0)
w5._app.config_manager.config.page_transition = "none"
w5._app.nav.current_page = 1
w5._arm_page_transition()
check("none 模式不置位", w5._transition_active is False
      and w5._pending_old_snapshot is None)
check("none 模式一次都没抓图", w5._grab_calls == 0, "grabs=%d" % w5._grab_calls)

w6 = StubWin(n_pages=1, cur=0)   # 单页
w6._app.nav.current_page = 0
w6._arm_page_transition()
check("单页不置位", w6._transition_active is False)

w7 = StubWin(n_pages=3, cur=-1)  # 首次显示
w7._app.nav.current_page = 0
w7._arm_page_transition()
check("首次显示（_current_page_idx<0）不置位", w7._transition_active is False)

w8 = StubWin(n_pages=3, cur=0)
w8._app.nav.current_page = 1
w8._editor_all_pages = True
w8._arm_page_transition()
check("编辑器全页模式不置位", w8._transition_active is False)
check("编辑器全页模式一次都没抓图", w8._grab_calls == 0)

# ---------------------------------------------------------------------------
print("6. 转场时长为 0 等同关闭")
w9 = StubWin(n_pages=3, cur=0)
w9._app.config_manager.config.transition_ms = 0
w9._app.nav.current_page = 1
w9._arm_page_transition()
check("transition_ms=0 不置位", w9._transition_active is False)

# ---------------------------------------------------------------------------
print("7. 失败路径必须拆掉盖罩（否则页面被旧页快照永久盖住）")
w10 = StubWin(n_pages=3, cur=0)
w10._app.nav.current_page = 1
w10._controller.set_rebuilding(True)
w10._arm_page_transition()
check("arm 后盖罩已就位", w10._page_veil is not None)
# 让「新页」那次抓图失败（返回 None），模拟抓图失败 / 视口为 0 的降级路径
w10.grab = lambda rect=None: None
w10._start_page_transition()
check("新页抓图失败时盖罩被拆掉", w10._page_veil is None)
check("新页抓图失败时 _transition_active 复位", w10._transition_active is False)

print()
if FAILURES:
    print("FAILED (%d): %s" % (len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("ALL PASS")
