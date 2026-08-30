"""页面转场动画 —— Phase 0（装饰层指示条） + Phase 1（快照位图转场）。

设计依据见项目根 ``页面转场动画-架构调查与方案设计.html``。

核心约束（来自 2026-08-29/30 的性能重构，不可违反）
------------------------------------------------------------------
本项目确立的信条是「动画只改 ``windowOpacity``、交给 DWM 合成、主线程零每帧
软件绘制」。页面转场做不到只改 windowOpacity（那是顶层窗口属性，而页面是子
widget），所以退而求其次，取**次优但同样 O(1) 的做法**：

    转场期间**不让真实 widget 动**，只把新旧两页各拍一张位图快照，
    然后在覆盖层上按进度画这两张图。

由此得到的性质：

* 每帧成本 = 2 次 ``drawPixmap``，**与卡片数量完全无关**（14 张卡和 3 张卡
  代价一样）。卡片是圆角 + 半透明的自定义 paintEvent 控件，平移时 Qt 无法走
  backing-store 位块传输、必须整页重绘——那是被 0829 重构消灭掉的卡顿来源。
* 不碰父子结构、不碰布局、不碰 ``setMask()``、不碰 ``WA_StaticContents``。
* 效果是「画两张图」，所以滑动 / 淡化 / 缩放 / 翻页共享同一份成本。
* 任何异常一律静默降级为现有硬切路径，绝不卡死。

Phase 0（``PageIndicator``）动的是页码条下方 3px 的指示条，每帧重绘面积
26×3，成本 <0.1ms，零风险。

DPR / drawPixmap 铁律（2026-08-31 修「翻页先放大再恢复」伪影）
------------------------------------------------------------------
``QWidget.grab(rect)`` 返回的快照自带 devicePixelRatio（掌机 1.5 / 2K 屏 2.0）。
``drawPixmap(target, pm, source)`` 的两个矩形坐标系**不同**：

* ``target`` → widget 的**逻辑**坐标（= 页面可视区大小）；
* ``source`` → pixmap 自身坐标系，即**设备像素全尺寸**
  ``QRectF(0, 0, pm.width(), pm.height())``。

早期实现错把 source 除以 DPR，只采样到快照左上角一小块再拉伸铺满——表现就是
「翻页瞬间页面被放大/裁切，动画结束后才恢复」。DPR=1 的屏幕上看不出差别，
所以只在掌机 / 高 DPI 屏暴露。现在统一由 ``_pixmap_source_rect()`` 提供
全设备像素 sourceRect，Qt 按 DPR 自动缩放到逻辑 target。

注意：本模块**禁止** import ``blocks/*`` 或 ``ui.panels``（跨层循环导入禁令，
见项目记忆）。它只依赖 Qt。
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import (
    QEasingCurve,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRectF,
    Qt,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import QPainter, QPixmap
from PyQt6.QtWidgets import QWidget

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 默认转场时长（ms）。180ms 与失焦暗化（180ms smoothstep）一致：
#: 「能看清但不拖沓」。被禁用的旧滑动实现用的是 100ms，实测偏快、看不清方向。
DEFAULT_TRANSITION_MS = 180

#: 合法转场模式。
TRANSITION_MODES = ("slide", "fade", "none")

#: 保底余量（ms）。在「转场意图置位」到「动画真正结束」之间还夹着一次
#: rebuild + 两次 grab()，所以余量必须给得比动画本身宽得多。
#: 定时器到点若发现动画已正常结束则 no-op；它的唯一职责是防止守卫永久置位
#: （那会让手柄彻底失灵）。
SAFETY_MARGIN_MS = 800

#: Phase 0 指示条动画时长（ms）。比页面转场略短，让指示器「先到位」。
INDICATOR_MS = 160

#: 指示条高度（px）。定位在页码按钮底部内侧，不撑高页码条。
INDICATOR_HEIGHT = 3

_INDICATOR_STYLE = "background: rgba(255,215,120,0.95); border-radius: 1px;"


# ---------------------------------------------------------------------------
# Phase 1：快照位图转场
# ---------------------------------------------------------------------------


def _pixmap_source_rect(pm: "QPixmap") -> QRectF:
    """快照位图的 sourceRect：使用**设备像素全尺寸**。

    ``QWidget.grab()`` 返回的位图自带 devicePixelRatio（掌机 1.5 / 本机 2.0），
    ``pm.width()``/``pm.height()`` 给出的是**设备**像素。``drawPixmap`` 的
    sourceRect 必须按 pixmap 自身的坐标系来——也就是设备像素；目标矩形
    (target) 才是 widget 的逻辑坐标。Qt 会根据 DPR 自动把设备像素缩放到逻辑
    目标上。

    如果错误地把 sourceRect 除以 DPR，只会采样到左上角一部分，然后被拉伸到
    整个目标矩形，导致「先放大、再恢复正常」的转场伪影。
    """
    return QRectF(0.0, 0.0, float(pm.width()), float(pm.height()))


class TransitionVeil(QWidget):
    """覆盖在页面可视区之上的转场层，只画两张页面快照。

    它是主窗口的普通子控件（不是顶层窗口），几何 = 滚动视口在主窗坐标系中的
    矩形。创建后 ``raise_()`` 置顶，转场结束即销毁。

    **不设** ``WA_TransparentForMouseEvents``：转场期间应该吸收鼠标事件。
    用户看到的是旧页快照，若点击穿透到下面真实的新页，等于「点了看不见的
    控件」——比「短暂不能点」危险得多。
    """

    def __init__(self, parent: QWidget, mode: str, old_pm: "QPixmap | None",
                 new_pm: "QPixmap | None", direction: int = 1) -> None:
        super().__init__(parent)
        self._mode = mode if mode in TRANSITION_MODES else "slide"
        self._old_pm = old_pm
        self._new_pm = new_pm
        self._direction = 1 if direction >= 0 else -1
        self._t = 0.0
        # 两张快照尺寸一致，sourceRect 建一次就够，之后每帧复用。
        # 注意：sourceRect 用设备像素全尺寸，不是逻辑尺寸（见 _pixmap_source_rect）。
        self._src_rect = _pixmap_source_rect(new_pm if new_pm is not None else old_pm) \
            if (new_pm is not None or old_pm is not None) else QRectF()
        self.hide()

    # -- 驱动 ---------------------------------------------------------------
    def set_progress(self, t: float) -> None:
        """由 :class:`PageTransition` 每帧调用；只更新进度 + 请求重绘。"""
        self._t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else float(t))
        self.update()

    def release_pixmaps(self) -> None:
        """立刻放掉两张快照（每张在 1080p@150% 下约 3MB）。"""
        self._old_pm = None
        self._new_pm = None

    def set_new_pixmap(self, pm: "QPixmap | None") -> None:
        """转场第二阶段：把新页快照补进已存在的「盖罩」。

        切页时序里，只有旧页快照能在 rebuild **之前**抓到（那时旧页还完整可见），
        而新页快照必须等 rebuild 之后才有，所以流程被拆成两步：

        1. rebuild 前：只拿旧页快照建一个「盖罩」并盖上（画面 = 旧页，用户无感），
           之后 rebuild 换上的新页真身始终藏在盖罩之下；
        2. rebuild 后：临时隐藏盖罩 → 抓新页快照 → 调本方法补上 → 起动画。

        必须复用同一个 veil 而不是新建一个：新建就得先销毁盖罩，中间只要被
        合成一帧，新页真身就会闪出来 —— 正是「先跳到第二页、再瞬回第一页」
        的成因。
        """
        self._new_pm = pm
        if pm is not None and self._src_rect.isEmpty():
            self._src_rect = _pixmap_source_rect(pm)

    # -- 绘制 ---------------------------------------------------------------
    def paintEvent(self, ev: Any) -> None:  # noqa: N802 - Qt camelCase
        old = self._old_pm
        new = self._new_pm
        if old is None and new is None:
            return
        p = QPainter(self)
        w = float(self.width())
        h = float(self.height())
        if w <= 0.0 or h <= 0.0:
            p.end()
            return
        t = self._t
        src = self._src_rect
        # ⚠️ 必须用**三参** drawPixmap(target, pm, source)。
        # 实测 PyQt6 6.11.0 / Qt 6.11.0 上两参的
        # ``drawPixmap(QRectF, QPixmap)`` 会原生崩溃（退出码 127，Python
        # try/except 完全捕获不到）。三参版 / QRect 版 / QPointF 版均正常。
        # 两张快照都是「视口尺寸」且首尾相接铺满整个 veil，几何上不会露出
        # 下面的真实页面，因此无需额外填充底色。
        if self._mode == "fade":
            # 交叉淡化：新页垫底，旧页在上面按 (1-t) 淡出。
            if new is not None:
                p.drawPixmap(QRectF(0.0, 0.0, w, h), new, src)
            if old is not None and t < 1.0:
                p.setOpacity(1.0 - t)
                p.drawPixmap(QRectF(0.0, 0.0, w, h), old, src)
                p.setOpacity(1.0)
        else:
            # 横向滑动：旧页往 -direction 移出，新页从 +direction 进入。
            d = float(self._direction)
            off = w * t
            if old is not None:
                p.drawPixmap(QRectF(-off * d, 0.0, w, h), old, src)
            if new is not None:
                p.drawPixmap(QRectF((w - off) * d, 0.0, w, h), new, src)
        p.end()


class PageTransition(QObject):
    """转场动画驱动器：持有一个 float ``progress`` 属性（0 → 1）。

    用 ``QPropertyAnimation`` 驱动该属性，每帧回调
    :meth:`TransitionVeil.set_progress`。动画对象必须保存在实例属性上
    （``self._anim``），否则会被 GC、动画静默停止——这是本项目踩过的坑。
    """

    finished = pyqtSignal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._progress = 0.0
        self._anim: Any = None
        self._veil: Any = None
        self._running = False

    # -- pyqtProperty: progress --------------------------------------------
    def _get_progress(self) -> float:
        return self._progress

    def _set_progress(self, value: float) -> None:
        self._progress = float(value)
        if self._veil is not None:
            try:
                self._veil.set_progress(self._progress)
            except Exception:
                pass

    progress = pyqtProperty(float, _get_progress, _set_progress)

    @property
    def running(self) -> bool:
        return bool(self._running)

    # -- 生命周期 -----------------------------------------------------------
    def start(self, veil: TransitionVeil, duration_ms: int) -> None:
        """开始一次转场。重复调用会先停掉上一次（快速连按翻页）。"""
        self.stop()
        self._veil = veil
        self._progress = 0.0
        try:
            veil.set_progress(0.0)
        except Exception:
            pass
        anim = QPropertyAnimation(self, b"progress")
        anim.setDuration(max(1, int(duration_ms)))
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.finished.connect(self._on_finished)
        # 必须持有引用：QPropertyAnimation 无父对象时会被 GC，动画静默停止。
        self._anim = anim
        self._running = True
        anim.start()

    def stop(self) -> None:
        """中止（不发射 finished——Qt 的 stop() 本来就不发）。"""
        anim = self._anim
        self._anim = None
        self._veil = None
        self._running = False
        if anim is not None:
            try:
                anim.stop()
            except Exception:
                pass

    def _on_finished(self) -> None:
        self._running = False
        try:
            self.finished.emit()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Phase 0：页码条滑动指示
# ---------------------------------------------------------------------------


class PageIndicator(QWidget):
    """页码条下方的滑动指示条（Phase 0）。

    绝对定位在页码条 ``bar`` 内，**不进 QHBoxLayout**，因此零布局扰动、
    不会触发 relayout。切页时用 ``QPropertyAnimation`` 动 ``pos`` 滑过去。

    每帧重绘面积 = 26×3 像素，成本可忽略（<0.1ms），且不触碰任何页面容器。
    """

    def __init__(self, bar: QWidget) -> None:
        super().__init__(bar)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFixedHeight(INDICATOR_HEIGHT)
        self.setStyleSheet(_INDICATOR_STYLE)
        self._anim: Any = None
        self._buttons: list = []
        self.hide()

    # -- 状态 ---------------------------------------------------------------
    def sync(self, buttons: list, current: int) -> None:
        """页码条（重建）后同步：无动画地落到目标按钮下方。

        父级隐藏时（编辑器模式）``show()`` 不会让本控件真的可见——QWidget
        的可见性是与父级求交的，所以这里可以无条件 show()。
        """
        self._buttons = list(buttons or [])
        if not self._buttons:
            self.hide()
            return
        target = self._buttons[max(0, min(int(current), len(self._buttons) - 1))]
        if target is None:
            self.hide()
            return
        self.setFixedWidth(max(8, target.width()))
        self.move(self._end_pos(target))
        self.raise_()
        self.show()

    def slide_to(self, current: int) -> None:
        """切页：动画滑到目标按钮下方。

        不可见（编辑器模式下页码条被隐藏）时直接跳位、不动画、不 show()，
        避免把指示条从隐藏的页码条里「单独显示」出来。
        """
        if not self._buttons:
            return
        idx = max(0, min(int(current), len(self._buttons) - 1))
        target = self._buttons[idx]
        if target is None:
            return
        end = self._end_pos(target)
        if not self.isVisible():
            self.move(end)
            return
        if self.pos() == end:
            return
        # 单 writer：先停掉上一次，杜绝两个动画抢 pos。
        if self._anim is not None:
            try:
                self._anim.stop()
            except Exception:
                pass
            self._anim = None
        anim = QPropertyAnimation(self, b"pos")
        anim.setDuration(INDICATOR_MS)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(self.pos())
        anim.setEndValue(end)
        self._anim = anim
        anim.start()

    def stop_anim(self) -> None:
        anim = self._anim
        self._anim = None
        if anim is not None:
            try:
                anim.stop()
            except Exception:
                pass

    # -- 内部 ---------------------------------------------------------------
    def _end_pos(self, target: Any) -> QPoint:
        """指示条的目标位置：贴目标按钮底边内侧。"""
        return QPoint(
            int(target.x()),
            int(target.y() + target.height() - INDICATOR_HEIGHT),
        )
