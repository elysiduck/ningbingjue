"""SMT / 核占用实时柱状图组件。

把原本位于 ``panels.py`` 的 :class:`CoreBarChart` 与 :class:`CoreSampler`
搬移到独立模块，避免与 panels.py 形成循环依赖，并新增容器类
:class:`SmtChartWidget` 供 ``SmtPanel`` 依赖。
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtWidgets import QLabel, QVBoxLayout, QHBoxLayout, QWidget
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtWidgets import QSizePolicy


class CoreBarChart(QWidget):
    """实时线程（逻辑处理器）占用柱状图。

      - 自动检测线程数：取 util 字典的键数（LP 索引），按索引升序绘制。
      - 柱色随占用率由绿(低)→黄→红(高)（HSL 色相 120°→0°）。
      - 按物理核分组：相邻物理核之间留更大间隙，SMT 兄弟线程紧挨。
      - 深色背景，自带最小高度；无数据时提示「无占用数据」。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._topo: dict | None = None
        self._util: dict[int, float] = {}
        self._order: list[int] = []
        self._core_of: dict[int, int] = {}
        # 缩短热核视图：降低最小高度，避免柱状图被布局拉伸得过高。
        self.setMinimumHeight(50)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def setData(self, topo: dict | None, util: dict[int, float] | None) -> None:
        self._topo = topo
        # util 键可能为字符串（perf_probe 监视器输出），强制转 int 后再排序，
        # 否则 sorted() 走字典序会得到 0,1,10,11,...,2 的错误顺序。
        self._util = {int(k): float(v) for k, v in (util or {}).items()}
        self._order = sorted(self._util.keys())
        self._core_of = {}
        if topo:
            lp_core = topo.get("lp_core") or {}
            for lp in self._order:
                self._core_of[lp] = lp_core.get(int(lp), -1)
        self.update()

    def sizeHint(self) -> QSize:
        # 缩短热核视图：首选高度从 120 降到 70，配合 Fixed 垂直策略与容器
        # 固定高度，使柱状图整体更矮、更紧凑。
        return QSize(200, 70)

    @staticmethod
    def _util_color(v: float) -> QColor:
        v = max(0.0, min(100.0, float(v)))
        h = 120.0 * (1.0 - v / 100.0)  # 120=绿, 0=红（经黄）
        return QColor.fromHslF(h / 360.0, 0.72, 0.50)

    def paintEvent(self, ev: Any) -> None:  # noqa: N802 - Qt camelCase
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        if w <= 2 or h <= 2:
            p.end()
            return
        p.fillRect(0, 0, w, h, QColor(13, 16, 23, 255))
        if not self._order:
            p.setPen(QColor(150, 160, 175))
            _sf = QFont("Segoe UI")
            _sf.setPixelSize(17)  # 10pt→17px 放大
            _sf.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
            p.setFont(_sf)
            p.drawText(8, h // 2, "无占用数据")
            p.end()
            return

        n = len(self._order)
        pad = 6
        label_h = 14
        axis_y = h - label_h
        top = 6
        avail = max(1, axis_y - top)
        gap = 2
        core_gap = 4
        cores = sorted({self._core_of.get(lp, -1) for lp in self._order})
        num_core_gaps = max(0, len(cores) - 1)
        avail_w = w - 2 * pad - (n - 1) * gap - num_core_gaps * core_gap
        bar_w = max(2.0, avail_w / n)

        # 基线
        p.setPen(QColor(60, 68, 82))
        p.drawLine(pad, axis_y, w - pad, axis_y)

        _sf2 = QFont("Segoe UI")
        _sf2.setPixelSize(14)  # 8pt→14px 放大
        _sf2.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
        p.setFont(_sf2)
        x = pad
        prev_core: int | None = None
        for lp in self._order:
            core = self._core_of.get(lp, -1)
            if prev_core is not None and core != prev_core:
                x += core_gap
            val = self._util.get(lp, 0.0)
            bh = avail * (max(0.0, min(100.0, val)) / 100.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._util_color(val))
            bx = int(x)
            by = int(axis_y - bh)
            p.drawRoundedRect(bx, by, int(bar_w), int(bh), 2, 2)
            # LP 索引标签（基线下方）
            p.setPen(QColor(150, 160, 175))
            p.drawText(bx, axis_y + 11, str(lp))
            x += bar_w + gap
            prev_core = core
        p.end()


class CoreSampler(QThread):
    """后台采样线程：订阅合并采样宿主的 cores 通道（core/perf_probe.
    SharedMonitor），周期性读取其 ``state["cores"]`` 并投到 GUI 线程绘制。
    最小化 / 隐藏时跳过采样。

    设计要点（修复此前「打开热核视图后卡死」）：
      * **不再 spawn 独立 PowerShell**（2026-08-19）：旧实现用
        ``perf_probe.start_core_monitor`` 各起一个长驻监视器，与主窗口的
        MetricSampler 各读各的；现统一订阅 SharedMonitor 的 cores 通道，
        全应用只保留一个合并 PowerShell 宿主。
      * 线程退出（stop / 最小化 / 视图关闭）时注销订阅即可，宿主由
        SharedMonitor 引用计数自动管理，杜绝遗留进程。
      * 订阅失败（非 Windows）时仅记录一次并退出，绝不反复 spawn。
    """

    data_ready = pyqtSignal(object, object)  # (topology, util_dict)

    def __init__(self, block: Any, parent: QWidget | None = None) -> None:
        # 故意 parentless：避免面板被 deleteLater 时连带销毁仍在运行的线程，
        # 触发 "QThread: Destroyed while thread '' is still running" 原生崩溃。
        # 线程生命周期完全由 SmtChartWidget 显式 start()/stop()/release() 管理。
        super().__init__(None)
        self._block = block
        self._running = True

    def run(self) -> None:  # noqa: N802 - Qt camelCase
        import time
        from ..core.perf_probe import SharedMonitor

        blk = self._block
        # 拓扑只读一次（_get_topology 内部原子赋值，线程安全）
        topo = None
        try:
            topo = blk._get_topology()
        except Exception:
            topo = None

        # 订阅合并宿主 cores 通道（回调在宿主 reader 线程写本地 dict）
        state: dict = {"cores": None}

        def _on_cores(d: dict) -> None:
            state["cores"] = d

        mon = SharedMonitor.instance()
        mon.subscribe("cores", _on_cores)
        try:
            while self._running:
                cores = state.get("cores")
                if cores:
                    # 拷贝一份，避免跨线程读取 dict 的竞争
                    self.data_ready.emit(topo, dict(cores))
                # 节流到 ~1Hz（宿主脚本本身 1s 输出一次）
                for _ in range(10):
                    if not self._running:
                        break
                    time.sleep(0.1)
        finally:
            # 注销订阅；若这是最后一个订阅者，宿主自动终止（引用计数归零）
            mon.unsubscribe("cores", _on_cores)

    def _log(self, msg: str) -> None:
        blk = self._block
        if hasattr(blk, "_log"):
            try:
                blk._log(msg)
            except Exception:
                pass

    def stop(self) -> None:
        self._running = False


class SmtChartWidget(QWidget):
    """SMT / 核占用实时柱状图容器。

    封装 :class:`CoreBarChart` 与 :class:`CoreSampler`，向上暴露稳定的
    构造与 API 契约（供 ``SmtPanel`` 依赖）。
    """

    def __init__(self, block, parent=None):
        super().__init__(parent)
        self._block = block
        self._paused = False
        self._sampler = None
        # 组件被销毁（面板重建 / 关闭窗口）时，若采样线程仍在跑，必须先把
        # 它停掉并等待结束，否则 Qt 会报
        # "QThread: Destroyed while thread '' is still running"（见 launch.err）。
        self.destroyed.connect(self._shutdown)

        layout = QVBoxLayout(self)
        # 缩短热核视图：收紧容器内边距与间距，整体更紧凑、不浪费垂直空间。
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)

        legend_row = QHBoxLayout()
        self.legend = QLabel("占用率 低→高：绿→黄→红（自动检测线程数）")
        self.legend.setStyleSheet("color:#8b97aa; font-size:10px;")
        legend_row.addWidget(self.legend)
        layout.addLayout(legend_row)

        self.chart = CoreBarChart()
        # 缩短热核视图：固定柱状图高度为 70px，且不再以 stretch=1 占满卡片
        # 剩余空间（旧实现会让它在高卡片里被拉得很高）。整体高度由此封顶。
        self.chart.setFixedHeight(70)
        self.chart.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.chart)

        self.info = QLabel("")
        self.info.setStyleSheet("color:#8b97aa; font-size:10px;")
        layout.addWidget(self.info)

    def start(self):
        if self._sampler is not None and self._sampler.isRunning():
            return
        if not hasattr(self._block, "current_util"):
            return
        # CoreSampler 为 parentless 线程，其删除由 stop()/_shutdown 负责，
        # 不在此连接 finished->deleteLater（避免与 stop() 重复 deleteLater）。
        sampler = CoreSampler(self._block)
        sampler.data_ready.connect(self._on_data)
        sampler.start()
        self._sampler = sampler
        self.info.setText("采样中…")

    def stop(self):
        s = self._sampler
        self._sampler = None
        if s is None:
            return
        if s.isRunning():
            s.stop()  # 置 _running=False，run() 循环随即退出并 terminate 监视器
            s.wait(2000)  # 同步等待线程真正结束，避免后续析构时仍 running
        # 线程对象为 parentless，必须显式删除（仅此处一处 deleteLater）。
        s.deleteLater()

    def release(self):
        """显式同步释放采样线程。

        在切页 / 关闭窗口 / 面板被 deleteLater 之前必须调用，确保线程已停止
        并退出，杜绝原生崩溃 'QThread: Destroyed while thread still running'。
        """
        self.stop()

    def _shutdown(self) -> None:
        """组件销毁时的最后兜底：若仍持有运行中的采样线程，停等后删除。

        正常情况下 release()/stop() 已被调用（self._sampler 为 None），此处
        不会再触发；仅用于遗漏场景。"""
        s = self._sampler
        self._sampler = None
        if s is not None and s.isRunning():
            s.stop()
            s.wait(2000)
            s.deleteLater()

    def setPaused(self, paused: bool):
        self._paused = bool(paused)
        if paused:
            self.stop()
        else:
            self.start()  # start() 内部会判重，无需担心

    def setData(self, topo, util):
        # 透传到 self.chart.setData(topo, util)（给测试用）
        self.chart.setData(topo, util)

    def _on_data(self, topo, util):
        self.chart.setData(topo, util)
        if topo:
            self.info.setText(f"拓扑：{topo.get('total_lp','?')} 线程 · 实时占用")
