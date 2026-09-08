"""blocks/undervolt.py — 降压控制卡片区块。

这是一个「虚拟」区块：它不持有任何 ryzenadj / 采样器状态，真正的负载门控
降压状态机（UndervoltController）由主窗口在 `_init_undervolt` 里装配并驱动；
本区块只作为卡片在 HUD / 布局编辑器里的存在载体，面板通过
`window().undervolt_*` 系列方法读写运行时开关与状态。

与 power / freeze 等真实区块不同，它不需要任何 hook —— 所有交互都走
app_window 暴露的 undervolt_get_flags / undervolt_set_flag / undervololt_pin_static
/ undervolt_status。
"""
from __future__ import annotations

from typing import Any

from ningbingjue.core.registry import Block


class UndervoltBlock:
    """降压控制卡片的区块实例（轻量载体）。"""

    # 默认卡片色（用户可在编辑项里改成任意色）。
    DEFAULT_COLOR = "#62e0a8"

    def register(self, registry: Any) -> None:
        registry.register(Block(
            type="undervolt",
            name="降压控制",
            color=self.DEFAULT_COLOR,
            card=None,
            # 只读虚拟卡片：高度由内容决定；提供 GetCardHeight 钩子以满足区块
            # 契约（数值仅占位，运行时不消费）。
            hooks={"GetCardHeight": lambda: 220},
        ))
