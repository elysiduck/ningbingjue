"""共享调参单源（GPU 自动锁频 / 自动锁帧）。

把两块共同需要、却又极易各自漂移的参数集中到一处，避免
``power`` 的 GPU 浮动与 ``rtss`` 的自动锁帧各写各的、慢慢产生偏差：

  * ``EMA_ALPHA``      —— GPU 利用率信号的指数滑动平均系数，两块共用，
                         抑制 PDH 计数器的单点抖动。
  * ``METRIC_STALE_SECS``      —— 指标新鲜度窗口：采样器最近一次有效更新
                         距今超过此值 ⇒ 信号不可用，tick 保守 no-op / 保持
                         当前频率，绝不基于冻结数据决策。
  * ``METRIC_STALE_UI_SECS``  —— UI 层「监控不可用」窗口：超过此值仍无 GPU
                         读数 ⇒ 自动锁帧按钮置灰（避免“采样已停摆却显示监控中”）。
  * ``is_gpu_counter_corrupted`` —— 计数器损坏判定，供两块在确认该状况时
                         提示用户以管理员跑 ``lodctr /r`` 重建 PDH 计数器。

block 专属的控制参数（阈值 / 拍数 / 斜率）仍留在各自模块内，因为它们是
不同算法（连续控制器 vs 布尔锁存），不应强制统一。本模块只负责“两块都应
一致”的那部分。
"""

from __future__ import annotations

import time
from typing import Any

# EMA 平滑系数：GPU 利用率信号在两块里都用同一 α 做指数滑动平均，
# 抑制 PDH 计数器的单点抖动。0.4 ≈ 保留 ~2.5 拍历史（采样间隔 1.5s）。
EMA_ALPHA = 0.4

# 指标新鲜度窗口（秒）：采样器最近一次有效更新距今超过此值 => 信号不可用，
# tick 保守 no-op / 保持当前频率，绝不基于冻结数据决策。
# 略大于 MetricSampler 的 GPU 采样间隔（1.5s），给网络抖动留余量。
METRIC_STALE_SECS = 6.0

# UI 层面「监控不可用」窗口（秒）：超过此值仍无 GPU 读数 => 自动锁帧按钮
# 置灰（避免“采样已停摆却显示监控中”）。明显大于 METRIC_STALE_SECS。
METRIC_STALE_UI_SECS = 15.0


def is_gpu_counter_corrupted(sampler: Any) -> bool:
    """判定 GPU PDH 计数器是否已损坏（而非启动预热中）。

    签名：采样器在运行、核心/进程级信号仍健康（``snapshot.ok`` 为真且非 GPU
    字段已过期），**唯独** ``gpu_pct`` 始终为 ``None``。这正是掌机
    ``\\GPU Engine(*)`` 计数器损坏时的表现——核心/进程计数器照常工作，GPU
    计数器永远读不到。

    仅在满足上述签名时为 ``True``；采样器未启动 / 整体停摆 / 尚处预热（``ok``
    为假或 ts 过期）均返回 ``False``，避免把“开机前几秒”误判为损坏。
    """
    try:
        if not sampler.is_running:
            return False
        snap = sampler.snapshot
        if not snap.ok:
            return False
        # 核心/进程级信号有新鲜读数（说明采样线程活着、非整体停摆）
        if (time.monotonic() - snap.ts) > METRIC_STALE_SECS:
            return False
        # 唯独 GPU 永远读不到
        return snap.gpu_pct is None
    except Exception:  # noqa: BLE001
        return False
