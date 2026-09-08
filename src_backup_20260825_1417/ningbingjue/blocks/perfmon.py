"""blocks/perfmon.py — 性能监控区块。

这是一个「虚拟」区块：它不持有任何采样器 / 状态机状态，真正的实时数据由
主窗口的 ``perfmon_snapshot()`` 聚合（读共享 MetricSampler + 降压状态机 +
rtss/power 区块），本区块只作为卡片在 HUD / 布局编辑器里的存在载体，面板通过
``window().perfmon_snapshot()`` 只读读取各项指标与判断结果。

与 undervolt / power 等真实区块不同，它不需要任何 hook —— 所有交互都走
app_window 暴露的 ``perfmon_snapshot``（只读）。
"""
from __future__ import annotations

from typing import Any

from ..core.registry import Block


class PerfMonBlock:
    """性能监控卡片的区块实例（轻量载体）。"""

    # 默认卡片色（用户可在编辑项里改成任意色）。
    DEFAULT_COLOR = "#7fb8ff"

    def register(self, registry: Any) -> None:
        registry.register(Block(
            type="perfmon",
            name="性能监控",
            color=self.DEFAULT_COLOR,
            card=None,
            # 只读实时卡片，高度由内容（10 行 + 标题）决定；提供与真实块一致的
            # GetCardHeight 钩子以满足区块契约（数值仅用于占位，运行时不消费）。
            hooks={"GetCardHeight": lambda: 260},
        ))
